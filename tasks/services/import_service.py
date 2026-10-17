"""
Import service — accepts the bundle/CSV formats produced by export_service
and loads them into the local DB.

Conflict policy (v1): skip items whose item_key already exists on this instance.
Never overwrite — users can always delete + reimport if they want a refresh.

Multi-pass import:
  Pass 1: create items (parent_item=null for now), build source_key→new_id map.
  Pass 2: resolve parent_item FKs using the mapping + any pre-existing items.
  Pass 3: import comments (match parent comments within the bundle).
  Pass 4: create ItemLinks (both endpoints must resolve).
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import Comment, Item, ItemLink, Project


# Matches export_service.SCHEMA_VERSION — importer accepts same MODEL, any REVISION/ADDITION
CURRENT_SCHEMA_MODEL = 1


class ImportError(Exception):
    """Raised for non-recoverable import errors (bad bundle, wrong schema)."""


@dataclass
class ImportResult:
    tasks_created: int = 0
    tasks_skipped: int = 0
    tasks_failed: int = 0
    comments_created: int = 0
    comments_skipped: int = 0
    links_created: int = 0
    links_skipped: int = 0
    skipped_keys: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tasks_created": self.tasks_created,
            "tasks_skipped": self.tasks_skipped,
            "tasks_failed": self.tasks_failed,
            "comments_created": self.comments_created,
            "comments_skipped": self.comments_skipped,
            "links_created": self.links_created,
            "links_skipped": self.links_skipped,
            "skipped_keys": self.skipped_keys,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _parse_dt(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    dt = parse_datetime(str(value))
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


def _parse_decimal(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "t")


def _validate_schema_version(version: str, result: ImportResult) -> None:
    """Reject incompatible bundles (different MODEL). Warn on future minor versions."""
    try:
        parts = version.split("-")
        model = int(parts[0])
    except (ValueError, IndexError):
        raise ImportError(f"Invalid schemaVersion: {version!r}")

    if model > CURRENT_SCHEMA_MODEL:
        raise ImportError(
            f"Bundle schemaVersion {version} is newer than this instance supports "
            f"(MODEL {model} > {CURRENT_SCHEMA_MODEL}). Upgrade Pulse before importing."
        )
    if model < CURRENT_SCHEMA_MODEL:
        raise ImportError(
            f"Bundle schemaVersion {version} is older than this instance expects "
            f"(MODEL {model} < {CURRENT_SCHEMA_MODEL}). Re-export from a current instance."
        )


def _resolve_project(task_data: dict, target_project: Optional[Project], result: ImportResult) -> Optional[Project]:
    """
    Resolve which project the imported item should land in.
    Preference order: explicit target_project arg → match by source_project_key → None (personal).
    """
    if target_project:
        return target_project

    src_key = task_data.get("source_project_key")
    if not src_key:
        return None

    # Match by project key across all orgs — the importing user's queryset
    # visibility is checked separately at the API layer.
    project = Project.objects.filter(key=src_key).first()
    if project is None:
        result.warnings.append(
            f"Project '{src_key}' not found on destination — importing item as personal (no project)"
        )
    return project


def _create_item_from_dict(
    task_data: dict,
    target_project: Optional[Project],
    importer_user,
    source_instance: Optional[str],
    result: ImportResult,
) -> Optional[Item]:
    """Create a single Item from a task_data dict. Returns the new Item, or None if skipped."""
    item_key = task_data.get("item_key")

    # Dedup by item_key (if present)
    if item_key and Item.objects.filter(item_key=item_key).exists():
        result.tasks_skipped += 1
        result.skipped_keys.append(item_key)
        return None

    project = _resolve_project(task_data, target_project, result)

    # Build kwargs — defensively, only include fields that exist on the model
    kwargs = {
        "mode": task_data.get("mode", "task"),
        "item_type": task_data.get("item_type", "task.generic"),
        "title": task_data.get("title") or "(untitled)",
        "description": task_data.get("description") or "",
        "source": task_data.get("source") or "import",
        "source_id": task_data.get("source_id"),
        "source_url": task_data.get("source_url"),
        "project": project,
        "item_key": item_key,  # Preserved from source
        "assignee_email": task_data.get("assignee_email"),
        "assignment_group": task_data.get("assignment_group"),
        "workflow_name": task_data.get("workflow"),
        "workflow_status": task_data.get("workflow_status"),
        "status": task_data.get("status") or "pending",
        "priority": task_data.get("priority") or "normal",
        "severity": task_data.get("severity"),
        "created_by_id": getattr(importer_user, "id", None),
        "created_by_email": task_data.get("reporter_email") or getattr(importer_user, "email", None),
        "due_date": _parse_dt(task_data.get("due_date")),
        "start_date": _parse_dt(task_data.get("start_date")),
        "completed_at": _parse_dt(task_data.get("completed_at")),
        "story_points": task_data.get("story_points"),
        "percent_complete": int(task_data.get("percent_complete") or 0),
        "estimated_hours": _parse_decimal(task_data.get("estimated_hours")),
        "original_estimate_minutes": task_data.get("original_estimate_minutes"),
        "time_spent_minutes": task_data.get("time_spent_minutes") or 0,
        "remaining_estimate_minutes": task_data.get("remaining_estimate_minutes"),
        "is_recurring": _parse_bool(task_data.get("is_recurring")),
        "frequency": task_data.get("frequency") or "one_time",
        "recurrence_pattern": task_data.get("recurrence_pattern"),
        "recurrence_end_date": _parse_dt(task_data.get("recurrence_end_date")),
        "tags": task_data.get("tags"),
        "labels": task_data.get("labels") or {},
        "metadata": dict(task_data.get("metadata") or {}),
    }

    # Audit trail — tag the item so operators can see where it came from and undo
    imported_from = {
        "source_instance": source_instance,
        "source_item_key": item_key,
        "imported_at": timezone.now().isoformat(),
        "imported_by": getattr(importer_user, "email", None),
    }
    kwargs["metadata"]["imported_from"] = imported_from

    try:
        with transaction.atomic():
            item = Item.objects.create(**kwargs)
            # Preserve created_at if provided (after create, because auto_now_add overrides)
            created_at = _parse_dt(task_data.get("created_at"))
            if created_at:
                Item.objects.filter(pk=item.pk).update(created_at=created_at)
                item.created_at = created_at
        result.tasks_created += 1
        return item
    except Exception as e:
        result.tasks_failed += 1
        result.errors.append(f"Failed to import {item_key or 'item'}: {e}")
        return None


def import_bundle(
    bundle_bytes: bytes,
    *,
    target_project: Optional[Project] = None,
    importer_user=None,
    include_comments: bool = True,
    include_links: bool = True,
) -> ImportResult:
    """
    Import a Pulse export bundle.

    Args:
      bundle_bytes: raw bytes of the ZIP bundle
      target_project: if provided, all imported items land in this project.
                      If None, each item tries to resolve by source_project_key.
      importer_user: the user performing the import (for audit + fallback reporter)
      include_comments: import comments.ndjson if present
      include_links: import links.ndjson if present
    """
    result = ImportResult()

    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile:
        raise ImportError("File is not a valid ZIP bundle")

    # --- Parse manifest ---
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except KeyError:
        raise ImportError("Bundle is missing manifest.json")
    except json.JSONDecodeError as e:
        raise ImportError(f"Invalid manifest.json: {e}")

    schema_version = manifest.get("schemaVersion")
    if not schema_version:
        raise ImportError("manifest.json missing schemaVersion")
    _validate_schema_version(schema_version, result)

    source_instance = manifest.get("sourceInstance")

    # --- Parse tasks.ndjson ---
    try:
        tasks_raw = zf.read("tasks.ndjson").decode("utf-8")
    except KeyError:
        raise ImportError("Bundle is missing tasks.ndjson")

    tasks_data = []
    for lineno, line in enumerate(tasks_raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            tasks_data.append(json.loads(line))
        except json.JSONDecodeError as e:
            result.errors.append(f"tasks.ndjson line {lineno}: {e}")

    # --- Pass 1: create items (parent_item=null) ---
    # Mappings for pass 2+ — by key when available, by source id as fallback
    key_to_item: dict[str, Item] = {}
    src_id_to_item: dict[int, Item] = {}
    for task_data in tasks_data:
        item = _create_item_from_dict(
            task_data, target_project, importer_user, source_instance, result,
        )
        if item:
            if task_data.get("item_key"):
                key_to_item[task_data["item_key"]] = item
            if task_data.get("source_item_id") is not None:
                src_id_to_item[task_data["source_item_id"]] = item

    # --- Pass 2: resolve parent_item FKs ---
    for task_data in tasks_data:
        item_key = task_data.get("item_key")
        parent_key = task_data.get("source_parent_key")
        if not item_key or not parent_key:
            continue
        child = key_to_item.get(item_key)
        if child is None:
            continue  # was skipped (already exists on dest)
        # Look up parent: prefer bundle-imported items, fall back to pre-existing on dest
        parent = key_to_item.get(parent_key) or Item.objects.filter(item_key=parent_key).first()
        if parent is None:
            result.warnings.append(
                f"Parent {parent_key!r} not found for {item_key} — imported as root-level"
            )
            continue
        child.parent_item = parent
        child.save(update_fields=["parent_item"])

    # --- Pass 3: comments ---
    if include_comments:
        try:
            comments_raw = zf.read("comments.ndjson").decode("utf-8")
        except KeyError:
            comments_raw = ""

        # source_comment_id → new_comment_id (for threading within bundle)
        source_cid_to_new: dict[Any, Comment] = {}
        for lineno, line in enumerate(comments_raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except json.JSONDecodeError as e:
                result.errors.append(f"comments.ndjson line {lineno}: {e}")
                continue

            # Resolve item: prefer just-imported (by key, then by source id),
            # fall back to existing on dest by key.
            src_item_key = c.get("source_item_key")
            src_item_id = c.get("source_item_id")
            item = None
            if src_item_key:
                item = key_to_item.get(src_item_key) or Item.objects.filter(item_key=src_item_key).first()
            if item is None and src_item_id is not None:
                item = src_id_to_item.get(src_item_id)
            if item is None:
                result.comments_skipped += 1
                continue

            # Dedup — skip if a comment with same content + author + time already exists
            created_at = _parse_dt(c.get("created_at"))
            content = c.get("content") or ""
            author_email = c.get("author_email") or ""
            exists = Comment.objects.filter(
                item=item,
                author_email=author_email,
                content=content,
                created_at=created_at,
            ).exists() if created_at else False
            if exists:
                result.comments_skipped += 1
                continue

            parent = None
            src_parent = c.get("source_parent_id")
            if src_parent is not None:
                parent = source_cid_to_new.get(src_parent)

            try:
                comment = Comment.objects.create(
                    item=item,
                    author_id=importer_user.id if importer_user else 0,
                    author_email=author_email,
                    author_name=c.get("author_name") or "",
                    content=content,
                    parent=parent,
                )
                # Preserve created_at
                if created_at:
                    Comment.objects.filter(pk=comment.pk).update(created_at=created_at)
                src_id = c.get("source_id")
                if src_id is not None:
                    source_cid_to_new[src_id] = comment
                result.comments_created += 1
            except Exception as e:
                result.errors.append(f"Comment create failed: {e}")

    # --- Pass 4: links ---
    if include_links:
        try:
            links_raw = zf.read("links.ndjson").decode("utf-8")
        except KeyError:
            links_raw = ""

        for lineno, line in enumerate(links_raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                l = json.loads(line)
            except json.JSONDecodeError as e:
                result.errors.append(f"links.ndjson line {lineno}: {e}")
                continue

            src_key = l.get("source_item_key")
            tgt_key = l.get("target_item_key")
            src_id = l.get("source_item_id")
            tgt_id = l.get("target_item_id")
            link_type = l.get("link_type") or "relates_to"

            source_item = None
            if src_key:
                source_item = key_to_item.get(src_key) or Item.objects.filter(item_key=src_key).first()
            if source_item is None and src_id is not None:
                source_item = src_id_to_item.get(src_id)

            target_item = None
            if tgt_key:
                target_item = key_to_item.get(tgt_key) or Item.objects.filter(item_key=tgt_key).first()
            if target_item is None and tgt_id is not None:
                target_item = src_id_to_item.get(tgt_id)

            if source_item is None or target_item is None:
                result.links_skipped += 1
                continue

            if ItemLink.objects.filter(
                source_item=source_item, target_item=target_item, link_type=link_type
            ).exists():
                result.links_skipped += 1
                continue

            try:
                ItemLink.objects.create(
                    source_item=source_item,
                    target_item=target_item,
                    link_type=link_type,
                    created_by_id=getattr(importer_user, "id", 0) or 0,
                    created_by_email=l.get("created_by_email") or getattr(importer_user, "email", ""),
                )
                result.links_created += 1
            except Exception as e:
                result.errors.append(f"Link create failed: {e}")

    return result


# === CSV import ============================================================

def import_csv(
    csv_text: str,
    *,
    target_project: Optional[Project] = None,
    importer_user=None,
) -> ImportResult:
    """
    Import a CSV produced by export_csv (or hand-edited by a user).
    Simpler than bundle — no comments, no links, no schema version.
    """
    result = ImportResult()

    reader = csv.DictReader(io.StringIO(csv_text))

    rows = list(reader)

    # Pass 1: create items
    key_to_item: dict[str, Item] = {}
    for row in rows:
        # Normalize CSV row to the bundle dict shape so we share the creator function
        task_data = {
            "item_key": (row.get("item_key") or "").strip() or None,
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "mode": (row.get("mode") or "task").strip(),
            "item_type": (row.get("item_type") or "task.generic").strip(),
            "status": (row.get("status") or "pending").strip(),
            "priority": (row.get("priority") or "normal").strip(),
            "severity": (row.get("severity") or None) or None,
            "workflow": (row.get("workflow") or None) or None,
            "workflow_status": (row.get("workflow_status") or None) or None,
            "assignee_email": (row.get("assignee_email") or None) or None,
            "reporter_email": (row.get("reporter_email") or None) or None,
            "source_project_key": (row.get("project_key") or None) or None,
            "source_parent_key": (row.get("parent_item_key") or None) or None,
            "due_date": row.get("due_date"),
            "start_date": row.get("start_date"),
            "completed_at": row.get("completed_at"),
            "created_at": row.get("created_at"),
            "story_points": int(row["story_points"]) if row.get("story_points") else None,
            "percent_complete": int(row["percent_complete"]) if row.get("percent_complete") else 0,
            "estimated_hours": row.get("estimated_hours"),
            "is_recurring": row.get("is_recurring"),
            "frequency": (row.get("frequency") or "one_time").strip(),
            "tags": row.get("tags"),
            "labels": json.loads(row["labels_json"]) if row.get("labels_json") else {},
            "metadata": json.loads(row["metadata_json"]) if row.get("metadata_json") else {},
        }

        item = _create_item_from_dict(
            task_data, target_project, importer_user, source_instance="csv", result=result,
        )
        if item and task_data.get("item_key"):
            key_to_item[task_data["item_key"]] = item

    # Pass 2: parent links
    for row in rows:
        item_key = (row.get("item_key") or "").strip()
        parent_key = (row.get("parent_item_key") or "").strip()
        if not item_key or not parent_key:
            continue
        child = key_to_item.get(item_key)
        if child is None:
            continue
        parent = key_to_item.get(parent_key) or Item.objects.filter(item_key=parent_key).first()
        if parent is None:
            result.warnings.append(f"Parent {parent_key!r} not found for {item_key}")
            continue
        child.parent_item = parent
        child.save(update_fields=["parent_item"])

    return result

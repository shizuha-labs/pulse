"""
Export service — produces two formats:
  1. Bundle (ZIP with manifest.json + ndjson files) — full fidelity, machine-readable,
     schema-versioned for safe upgrades. Use for cross-instance migrations.
  2. CSV — one row per task, Excel-openable. Use for human editing.

See README-EXPORT.md for the schema spec.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone as _tz
from typing import Iterable, Optional

from django.conf import settings
from django.utils import timezone

from ..models import Comment, Item, ItemLink

# SchemaVer: MODEL-REVISION-ADDITION.
# - MODEL bump → breaking change; older importers must refuse the bundle
# - REVISION bump → backward-compatible semantic change
# - ADDITION bump → pure additive (new optional fields); older importers can still read
SCHEMA_VERSION = "1-0-0"
GENERATOR_NAME = "shizuha-pulse"
GENERATOR_VERSION = "1.0.0"


def _iso(dt) -> Optional[str]:
    """Serialize a datetime (or None) to ISO 8601 UTC string."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, _tz.utc)
    return dt.astimezone(_tz.utc).isoformat().replace("+00:00", "Z")


def _item_to_dict(item: Item) -> dict:
    """Serialize an Item to the bundle's tasks.ndjson format.
    Fields are named to be stable across schema revisions — rename triggers a MODEL bump."""
    return {
        # Identity — primary key for dedup on import
        "item_key": item.item_key,
        # Fallback identity for items without item_key (personal tasks).
        # Used to match comments/links to their parent item within the same bundle.
        "source_item_id": item.id,
        # Basic info
        "mode": item.mode,
        "item_type": item.item_type,
        "title": item.title,
        "description": item.description or "",
        # Source tracking (origin system)
        "source": item.source,
        "source_id": item.source_id,
        "source_url": item.source_url,
        # Project reference (by key, not numeric ID)
        "source_project_key": item.project.key if item.project else None,
        "source_project_name": item.project.name if item.project else None,
        # Assignment (by email — resolves to user on import)
        "assignee_email": item.assignee_email,
        "assignee_name": item.assignee_name if hasattr(item, "assignee_name") else None,
        "assignment_group": item.assignment_group,
        "reporter_email": item.created_by_email,
        "reporter_name": item.created_by_name if hasattr(item, "created_by_name") else None,
        # Workflow (by slug, not numeric ID)
        "workflow": item.workflow_name,
        "workflow_status": item.workflow_status,
        # Status & priority
        "status": item.status,
        "priority": item.priority,
        "severity": item.severity,
        # Timing
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "due_date": _iso(item.due_date),
        "start_date": _iso(item.start_date),
        "completed_at": _iso(item.completed_at),
        # Task-specific
        "story_points": item.story_points,
        "percent_complete": item.percent_complete,
        "estimated_hours": str(item.estimated_hours) if item.estimated_hours is not None else None,
        "original_estimate_minutes": item.original_estimate_minutes,
        "time_spent_minutes": item.time_spent_minutes,
        "remaining_estimate_minutes": item.remaining_estimate_minutes,
        # Recurrence
        "is_recurring": item.is_recurring,
        "frequency": item.frequency,
        "recurrence_pattern": item.recurrence_pattern,
        "recurrence_end_date": _iso(item.recurrence_end_date),
        # Parent (by item_key — resolved in pass 2 during import)
        "source_parent_key": (
            item.parent_item.item_key if item.parent_item_id and item.parent_item else None
        ),
        # Tags / labels / metadata blobs (preserved as-is)
        "tags": item.tags,
        "labels": item.labels or {},
        "metadata": item.metadata or {},
    }


def _comment_to_dict(comment: Comment) -> dict:
    return {
        "source_item_key": comment.item.item_key,
        "source_item_id": comment.item_id,  # Fallback for items without item_key
        "author_email": comment.author_email,
        "author_name": comment.author_name,
        "content": comment.content,
        "created_at": _iso(comment.created_at),
        "updated_at": _iso(comment.updated_at) if hasattr(comment, "updated_at") else None,
        # Threading: source_parent_comment_id used only if parents are also in the bundle
        "source_parent_id": comment.parent_id if comment.parent_id else None,
        "source_id": comment.id,  # Preserved for threading within bundle only
    }


def _link_to_dict(link: ItemLink) -> dict:
    return {
        "source_item_key": link.source_item.item_key,
        "source_item_id": link.source_item_id,   # Fallback for items without item_key
        "target_item_key": link.target_item.item_key,
        "target_item_id": link.target_item_id,
        "link_type": link.link_type,
        "created_by_email": link.created_by_email,
        "created_at": _iso(link.created_at),
    }


# Minimal JSON Schema for tasks.ndjson — shipped inside the bundle so an
# importer can validate locally without network access.
_TASKS_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"https://shizuha.com/pulse/schema/tasks/{SCHEMA_VERSION}",
    "title": "PulseTaskExport",
    "type": "object",
    "required": ["item_key", "title", "mode", "status"],
    "properties": {
        "item_key": {"type": ["string", "null"]},
        "mode": {"type": "string", "enum": ["task", "action", "alert"]},
        "item_type": {"type": "string"},
        "title": {"type": "string", "maxLength": 400},
        "description": {"type": "string"},
        "status": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
        "severity": {"type": ["string", "null"], "enum": ["info", "warning", "error", "critical", None]},
        "source_project_key": {"type": ["string", "null"]},
        "assignee_email": {"type": ["string", "null"]},
        "reporter_email": {"type": ["string", "null"]},
        "workflow": {"type": ["string", "null"]},
        "workflow_status": {"type": ["string", "null"]},
        "source_parent_key": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"], "format": "date-time"},
        "created_at": {"type": ["string", "null"], "format": "date-time"},
        "updated_at": {"type": ["string", "null"], "format": "date-time"},
        "completed_at": {"type": ["string", "null"], "format": "date-time"},
        "is_recurring": {"type": "boolean"},
        "frequency": {"type": "string"},
        "story_points": {"type": ["integer", "null"]},
        "percent_complete": {"type": "integer", "minimum": 0, "maximum": 100},
        "tags": {"type": ["string", "null"]},
        "labels": {"type": "object"},
        "metadata": {"type": "object"},
    },
    "additionalProperties": True,  # Allow future fields without MODEL bump
}


def export_bundle(
    queryset,
    *,
    include_comments: bool = True,
    include_links: bool = True,
    source_instance: Optional[str] = None,
    user=None,
) -> bytes:
    """
    Export items to a ZIP bundle. Returns raw bytes.

    Args:
      queryset: Item QuerySet (already filtered to what the user can see).
      include_comments: if True, include each item's comments in comments.ndjson.
      include_links: if True, include ItemLinks where both endpoints are in the
                     export set.
      source_instance: human-readable identifier of the source Pulse instance
                       (e.g. "https://shizuha.com/pulse"). Written to manifest.
      user: the requesting user (written to manifest for audit).
    """
    items = list(queryset.select_related("project", "parent_item"))
    item_ids = [i.id for i in items]
    item_keys = {i.id: i.item_key for i in items if i.item_key}

    # Comments for these items
    comments = []
    if include_comments:
        comments = list(
            Comment.objects.filter(item_id__in=item_ids).select_related("item").order_by("created_at")
        )

    # Links — include only when BOTH endpoints are being exported. Cross-bundle
    # links are unreliable (target might not exist on dest), so we skip them.
    links = []
    if include_links:
        links = list(
            ItemLink.objects.filter(
                source_item_id__in=item_ids,
                target_item_id__in=item_ids,
            ).select_related("source_item", "target_item")
        )

    now = timezone.now()
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generator": GENERATOR_NAME,
        "generatorVersion": GENERATOR_VERSION,
        "exportedAt": _iso(now),
        "sourceInstance": source_instance or getattr(settings, "PULSE_INSTANCE_URL", "unknown"),
        "exportedBy": getattr(user, "email", None) if user else None,
        "counts": {
            "tasks": len(items),
            "comments": len(comments),
            "links": len(links),
            "attachments": 0,  # v1: attachments not included
        },
        "includes": {
            "tasks": True,
            "comments": include_comments,
            "links": include_links,
            "attachments": False,
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("schema.json", json.dumps(_TASKS_JSON_SCHEMA, indent=2))

        # tasks.ndjson — one line per item
        tasks_buf = io.StringIO()
        for item in items:
            tasks_buf.write(json.dumps(_item_to_dict(item), ensure_ascii=False))
            tasks_buf.write("\n")
        zf.writestr("tasks.ndjson", tasks_buf.getvalue())

        if include_comments:
            comments_buf = io.StringIO()
            for c in comments:
                comments_buf.write(json.dumps(_comment_to_dict(c), ensure_ascii=False))
                comments_buf.write("\n")
            zf.writestr("comments.ndjson", comments_buf.getvalue())

        if include_links:
            links_buf = io.StringIO()
            for l in links:
                links_buf.write(json.dumps(_link_to_dict(l), ensure_ascii=False))
                links_buf.write("\n")
            zf.writestr("links.ndjson", links_buf.getvalue())

        # README — human-readable explainer so someone unpacking the zip knows what it is
        zf.writestr("README.txt", _BUNDLE_README.format(
            schema_version=SCHEMA_VERSION,
            generator=GENERATOR_NAME,
            generator_version=GENERATOR_VERSION,
            exported_at=_iso(now),
            task_count=len(items),
            comment_count=len(comments),
            link_count=len(links),
        ))

    return buf.getvalue()


_BUNDLE_README = """Shizuha Pulse Export Bundle
===========================

schemaVersion: {schema_version}
generator:     {generator} v{generator_version}
exportedAt:    {exported_at}

Contents
--------
  manifest.json       Export metadata + schema version (read this first)
  schema.json         JSON Schema for tasks.ndjson
  tasks.ndjson        {task_count} tasks (one JSON object per line)
  comments.ndjson     {comment_count} comments
  links.ndjson        {link_count} issue links

SchemaVer
---------
The schemaVersion field uses MODEL-REVISION-ADDITION semantics:
  - MODEL bump    = breaking change (old importers MUST refuse)
  - REVISION bump = semantic change, backward-compatible
  - ADDITION bump = pure additive (new optional fields)

Import instructions
-------------------
  1. UI:  Open the Pulse Tasks page, click "Import", upload this ZIP.
  2. CLI: docker compose exec shizuha-pulse python manage.py import_bundle /path/to/bundle.zip

By default, existing items (matched by item_key) are skipped.
"""


# === CSV export ============================================================

# Column order is stable — changing it is a schema break (MODEL bump).
CSV_COLUMNS = [
    "item_key",
    "title",
    "description",
    "mode",
    "item_type",
    "status",
    "priority",
    "severity",
    "workflow",
    "workflow_status",
    "assignee_email",
    "reporter_email",
    "project_key",
    "parent_item_key",
    "due_date",
    "start_date",
    "completed_at",
    "created_at",
    "updated_at",
    "story_points",
    "percent_complete",
    "estimated_hours",
    "is_recurring",
    "frequency",
    "tags",
    "labels_json",
    "metadata_json",
]


def export_csv(queryset) -> str:
    """
    Export items to CSV. Returns CSV as a string.
    Columns are the union of user-editable fields; comments/links are NOT included
    (CSV fidelity is lossy — use the bundle for full export).
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()

    for item in queryset.select_related("project", "parent_item"):
        writer.writerow({
            "item_key": item.item_key or "",
            "title": item.title,
            "description": item.description or "",
            "mode": item.mode,
            "item_type": item.item_type,
            "status": item.status,
            "priority": item.priority,
            "severity": item.severity or "",
            "workflow": item.workflow_name or "",
            "workflow_status": item.workflow_status or "",
            "assignee_email": item.assignee_email or "",
            "reporter_email": item.created_by_email or "",
            "project_key": item.project.key if item.project else "",
            "parent_item_key": item.parent_item.item_key if item.parent_item_id and item.parent_item else "",
            "due_date": _iso(item.due_date) or "",
            "start_date": _iso(item.start_date) or "",
            "completed_at": _iso(item.completed_at) or "",
            "created_at": _iso(item.created_at) or "",
            "updated_at": _iso(item.updated_at) or "",
            "story_points": item.story_points if item.story_points is not None else "",
            "percent_complete": item.percent_complete,
            "estimated_hours": str(item.estimated_hours) if item.estimated_hours is not None else "",
            "is_recurring": "true" if item.is_recurring else "false",
            "frequency": item.frequency,
            "tags": item.tags or "",
            "labels_json": json.dumps(item.labels) if item.labels else "",
            "metadata_json": json.dumps(item.metadata) if item.metadata else "",
        })

    return buf.getvalue()

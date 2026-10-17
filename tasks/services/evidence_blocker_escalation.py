"""Event-driven deadline escalation for evidence-request blockers (PLS-463).

The blocker Item is the durable source of truth.  A mutation hook reconciles a
single generation in ``metadata.evidence_escalation`` and places that generation
on the existing Redis timer scheduler.  Delivery re-checks the live generation,
so callbacks popped before a due-date/evidence/link change are harmless.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

EVIDENCE_REQUEST_KEY = "evidence_request"
ESCALATION_STATE_KEY = "evidence_escalation"
EVIDENCE_TAG = "evidence-request"
COS_EMAIL = os.environ.get('PULSE_COS_EMAIL', '').strip().lower()
OPERATOR_EMAIL = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower()
ACTIVE_PRIORITIES = frozenset({"urgent", "high"})


def _hours(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using %.1fh", name, default)
        return default


def _tagged(item) -> bool:
    tags = {
        token.strip().lower()
        for token in (getattr(item, "tags", "") or "").split(",")
        if token.strip()
    }
    labels = getattr(item, "labels", {}) or {}
    label_value = labels.get(EVIDENCE_TAG) if isinstance(labels, dict) else None
    return EVIDENCE_TAG in tags or str(label_value or "").lower() in {
        "1", "true", "yes", EVIDENCE_TAG,
    }


def has_evidence_marker(item) -> bool:
    """Cheap signal guard; avoids a reconciliation query for ordinary items."""
    metadata = getattr(item, "metadata", {}) or {}
    return _tagged(item) or bool(metadata.get(ESCALATION_STATE_KEY))


def _active_unblocked_parents(item):
    """Return current items that this blocker prevents from progressing."""
    from tasks.models import Item, ItemLink

    parent_ids = set(ItemLink.objects.filter(
        source_item=item, link_type="blocks",
    ).values_list("target_item_id", flat=True))
    parent_ids.update(ItemLink.objects.filter(
        target_item=item, link_type="is_blocked_by",
    ).values_list("source_item_id", flat=True))
    if not parent_ids:
        return []
    parents = list(
        Item.objects.filter(pk__in=parent_ids, is_archived=False)
        .order_by("pk")
    )
    from tasks.services.workflow_service import WorkflowService
    return [
        parent for parent in parents
        if not WorkflowService._status_releases_blocks(parent.status)
    ]


def _request(item):
    metadata = getattr(item, "metadata", {}) or {}
    request = metadata.get(EVIDENCE_REQUEST_KEY) or {}
    return request if isinstance(request, dict) else {}


def escalation_snapshot(item):
    """Return the current eligible generation, or ``None`` when ineligible."""
    from tasks.services.workflow_service import WorkflowService

    if (
        getattr(item, "mode", None) != "task"
        or getattr(item, "is_archived", False)
        or WorkflowService._status_releases_blocks(getattr(item, "status", None))
        or getattr(item, "priority", None) not in ACTIVE_PRIORITIES
        or not getattr(item, "due_date", None)
        or not _tagged(item)
    ):
        return None

    request = _request(item)
    owner = str(request.get("owner_email") or "").strip().lower()
    required = str(request.get("required_evidence") or "").strip()
    # This lane is for a named evidence owner, not a generic team-doable or
    # operator/CEO escalation.  CoS is the escalation observer, never the
    # fallback evidence owner.
    if (
        not owner or not required or owner == OPERATOR_EMAIL
        or owner != str(getattr(item, "assignee_email", "") or "").strip().lower()
    ):
        return None

    parents = _active_unblocked_parents(item)
    if not parents:
        return None

    age_at = item.created_at + timedelta(
        hours=_hours("PULSE_EVIDENCE_BLOCKER_MAX_AGE_HOURS", 24.0)
    )
    due_at = item.due_date - timedelta(
        hours=_hours("PULSE_EVIDENCE_BLOCKER_DUE_LEAD_HOURS", 24.0)
    )
    fire_at = max(timezone.now(), min(age_at, due_at))
    parent_rows = [
        {"id": parent.pk, "key": parent.item_key or str(parent.pk), "title": parent.title}
        for parent in parents
    ]
    generation_input = {
        "item_id": item.pk,
        "status": item.status,
        "priority": item.priority,
        "due_date": item.due_date.isoformat(),
        "owner_email": owner,
        "required_evidence": required,
        "parents": [row["id"] for row in parent_rows],
    }
    generation = hashlib.sha256(
        json.dumps(generation_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "generation": generation,
        "fire_at": fire_at.isoformat(),
        "owner_email": owner,
        "required_evidence": required,
        "parents": parent_rows,
    }


def _schedule(item_id: int, fire_at: str) -> None:
    from tasks.scheduler import get_timer_manager

    get_timer_manager().schedule_evidence(str(item_id), datetime.fromisoformat(fire_at))


def _cancel(item_id: int) -> None:
    from tasks.scheduler import get_timer_manager

    get_timer_manager().cancel_evidence(str(item_id))


def reconcile_evidence_blocker(item_id: int) -> str:
    """Reconcile one blocker after a committed Item or ItemLink mutation."""
    from tasks.models import Item

    with transaction.atomic():
        try:
            item = Item.objects.select_for_update().get(pk=item_id)
        except Item.DoesNotExist:
            transaction.on_commit(lambda: _cancel(item_id))
            return "missing"

        metadata = dict(item.metadata or {})
        current = metadata.get(ESCALATION_STATE_KEY)
        current = current if isinstance(current, dict) else {}
        snapshot = escalation_snapshot(item)

        if snapshot is None:
            if current:
                metadata.pop(ESCALATION_STATE_KEY, None)
                item.metadata = metadata
                item.save(update_fields=["metadata", "updated_at"])
            transaction.on_commit(lambda: _cancel(item_id))
            return "cleared"

        if current.get("generation") == snapshot["generation"]:
            if current.get("state") == "scheduled":
                transaction.on_commit(
                    lambda: _schedule(item_id, current["fire_at"])
                )
            return current.get("state") or "scheduled"

        metadata[ESCALATION_STATE_KEY] = {
            **snapshot,
            "state": "scheduled",
            "delivered_recipient_emails": [],
            "scheduled_at": timezone.now().isoformat(),
        }
        item.metadata = metadata
        item.save(update_fields=["metadata", "updated_at"])
        transaction.on_commit(lambda: _schedule(item_id, snapshot["fire_at"]))
        return "scheduled"


def reconcile_link(link) -> None:
    """Reconcile the blocker side of a blocks/is_blocked_by link."""
    if getattr(link, "link_type", None) == "blocks":
        blocker_id = link.source_item_id
    elif getattr(link, "link_type", None) == "is_blocked_by":
        blocker_id = link.target_item_id
    else:
        return
    transaction.on_commit(lambda: reconcile_evidence_blocker(blocker_id))

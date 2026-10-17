"""PLS-1041: merge-lane stall-escalation sweep.

An ``awaiting_merge`` item with a mergeable PR that has waited past the stall
threshold (env-configurable, default N hours) is re-resolved against the merge
lane: if a distinct available merger is now selectable (a seat freed up, or the
capacity accounting recovered), the item is assigned to it and the
``merge_sod_hold`` is cleared; otherwise a merge-lane stall alert fires so a
capacity-saturated lane never silently strands approved PRs (PLS-411/PLS-856).

This is the stall-escalation post-function half of PLS-1041. The capacity-
accounting half (waiting states no longer count as WIP) is gated on the
broker-only cutover (PLAT-5700/ORIG-240) and tracked there.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from tasks.models import Item

# Stall threshold before an awaiting_merge item is re-resolved/escalated.
MERGE_STALL_HOURS = float(getattr(settings, 'PULSE_MERGE_STALL_HOURS', 6))

_ALERT_SOURCE = 'pulse-merge-stall'


def _mergeable_pr(item) -> bool:
    """Does the item have a live, mergeable linked PR (not conflicting/dirty)?"""
    from tasks.services.workflow_service import WorkflowService
    try:
        entries = WorkflowService._linked_pr_entries(item)
    except Exception:
        return False
    for entry in entries or []:
        status = str(entry.get('status') or 'open').strip().lower()
        if status in {'closed', 'merged'}:
            continue
        if WorkflowService._is_retired_mirror_pr(entry):
            continue
        # A conflicting/dirty PR is not mergeable — it must go back to the
        # implementer to rebase, not stall in the merge lane.
        if str(entry.get('mergeable') or '').lower() in {'false', 'conflict', 'dirty'}:
            continue
        return True
    return False


def _stalled_since(item):
    """When the item entered the merge stall (held_at or last update)."""
    meta = getattr(item, 'metadata', None) or {}
    hold = meta.get('merge_sod_hold') if isinstance(meta, dict) else None
    if isinstance(hold, dict) and hold.get('held_at'):
        held_at = hold.get('held_at')
        if hasattr(held_at, 'tzinfo'):
            return held_at
    return item.updated_at or item.created_at


def _re_resolve_merger(item):
    """Re-attempt merge-lane resolution; returns (uid, email) or (None, None)."""
    from tasks.services.workflow_service import WorkflowService
    try:
        return WorkflowService._resolve_team_assignee(
            'merge',
            exclude_email=item.created_by_email,
            project_key=WorkflowService._item_project_key(item),
            item=item,
        )
    except Exception:
        return (None, None)


def _fire_stall_alert(item) -> None:
    from tasks.services.deduplication import AlertDeduplicationService
    now = timezone.now()
    data = {
        'item_type': 'alert.merge_stall',
        'title': (
            f'Merge-lane stall: {getattr(item, "item_key", "") or item.pk} '
            'awaiting_merge past threshold with no distinct merger'
        ),
        'source': _ALERT_SOURCE,
        'source_id': f'merge-stall:{item.pk}',
        'severity': 'warning',
        'labels': {
            'item': getattr(item, 'item_key', '') or str(item.pk),
            'alertname': 'PulseMergeStall',
        },
        'payload': {
            'item_id': item.pk,
            'item_key': getattr(item, 'item_key', '') or '',
            'stall_hours': MERGE_STALL_HOURS,
            'generated_at': now.isoformat(),
            'lane_health_view': '/shizuha-pulse/api/lane-health/?team=merge',
        },
        'description': (
            f"Item `{getattr(item, 'item_key', '') or item.pk}` has been in "
            f"`awaiting_merge` past the {MERGE_STALL_HOURS}h stall threshold "
            f"with a mergeable PR but no distinct available merger "
            f"(PLS-411/PLS-856). See the merge-lane health view."
        ),
    }
    AlertDeduplicationService.submit_alert(data)


def check_merge_stalls(*, threshold_hours: float | None = None) -> dict:
    """Run the merge-lane stall-escalation sweep.

    Returns ``{"checked": int, "reassigned": int, "escalated": int,
    "stalled": list[dict]}``.
    """
    threshold = threshold_hours if threshold_hours is not None else MERGE_STALL_HOURS
    cutoff = timezone.now() - timedelta(hours=threshold)

    qs = Item.objects.filter(
        mode='task',
        status='awaiting_merge',
        resolution__isnull=True,
    ).order_by('updated_at')

    checked = 0
    reassigned = 0
    escalated = 0
    stalled = []

    for item in qs.iterator():
        checked += 1
        if not _mergeable_pr(item):
            continue
        since = _stalled_since(item)
        if since is None or since > cutoff:
            continue
        stalled.append({
            'item_key': getattr(item, 'item_key', '') or '',
            'item_id': item.pk,
            'held_at': since.isoformat() if hasattr(since, 'isoformat') else str(since),
        })

        uid, email = _re_resolve_merger(item)
        if uid:
            from tasks.services.workflow_service import WorkflowService
            update_fields = []
            WorkflowService._apply_resolved_assignee(
                item, uid, email, update_fields,
            )
            WorkflowService._clear_merge_sod_hold(item, update_fields)
            item.save(update_fields=update_fields + ['updated_at'])
            reassigned += 1
        else:
            _fire_stall_alert(item)
            escalated += 1

    return {
        'checked': checked,
        'reassigned': reassigned,
        'escalated': escalated,
        'stalled': stalled,
    }

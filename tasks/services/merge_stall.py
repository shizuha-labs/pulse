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

PLAT-10108 (merge-click auto-failover): when the resolved merger rung is
offline, the sweep re-resolves excluding the offline rung so the consolidated
merge-ready queue lands on the next merge-capable seat that ALREADY holds
merge rights per the BUILD-13 map (aoi, then ryo) — never a seat that would
need a new grant. The no-distinct-merger escalation alert is the fail-loud
zero-click counter and routes to devops.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from tasks.models import Item

# Stall threshold before an awaiting_merge item is re-resolved/escalated.
# PLAT-10108: fail-loud zero-click age counter — default 4h, not 6h.
MERGE_STALL_HOURS = float(getattr(settings, 'PULSE_MERGE_STALL_HOURS', 4))

_ALERT_SOURCE = 'pulse-merge-stall'

# BUILD-13 failover order (PLAT-10108): seats that ALREADY hold merge rights,
# in BUILD-13 map order. _failover_merger CONSUMES this as the ordered
# candidate pool: earlier passes exclude the later candidates so the resolver
# must land on the highest-priority available BUILD-13 seat first (aoi), and
# only a subsequent pass can reach ryo. Never widened ad hoc — a seat not on
# this list must get merge rights via a reviewed grant first, not via this
# sweep.
DEFAULT_MERGE_FAILOVER_CANDIDATES = ['aoi@shizuha.com', 'ryo@shizuha.com']


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


def _re_resolve_merger(item, extra_exclude_emails=None):
    """Re-attempt merge-lane resolution; returns (uid, email) or (None, None)."""
    from tasks.services.workflow_service import WorkflowService
    try:
        return WorkflowService._resolve_team_assignee(
            'merge',
            exclude_email=item.created_by_email,
            hard_exclude_emails=extra_exclude_emails or None,
            project_key=WorkflowService._item_project_key(item),
            item=item,
        )
    except Exception:
        return (None, None)


def _failover_merger(item):
    """PLAT-10108: re-resolve excluding the offline merger rung.

    Excludes the item's current assignee (the offline rung) and the author so
    the resolver lands on the next merge-capable seat. The BUILD-13 order
    (DEFAULT_MERGE_FAILOVER_CANDIDATES) is enforced by pass: each pass excludes
    the LATER candidates, so the first pass can only land on aoi (or another
    merge-lane member that already holds rights), and ryo is reachable only
    when aoi's pass found nothing — seats that ALREADY hold merge rights, never
    a seat that would need a new grant. Returns (uid, email) or (None, None).
    """
    excludes = []
    for email in (item.assignee_email, item.created_by_email):
        if email and email not in excludes:
            excludes.append(email)
    if not excludes:
        return (None, None)
    candidates = list(DEFAULT_MERGE_FAILOVER_CANDIDATES)
    for i in range(len(candidates)):
        later = candidates[i + 1:]
        uid, email = _re_resolve_merger(
            item, extra_exclude_emails=excludes + later,
        )
        if uid:
            return (uid, email)
    return (None, None)


def _fire_stall_alert(item, ready_queue=None) -> None:
    from tasks.services.deduplication import AlertDeduplicationService
    ready_queue = list(ready_queue or [])
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
            'owner_group': 'devops',
        },
        'payload': {
            'item_id': item.pk,
            'item_key': getattr(item, 'item_key', '') or '',
            'stall_hours': MERGE_STALL_HOURS,
            'ready_queue': ready_queue,
            'generated_at': now.isoformat(),
            'lane_health_view': '/shizuha-pulse/api/lane-health/?team=merge',
        },
        'description': (
            f"Item `{getattr(item, 'item_key', '') or item.pk}` has been in "
            f"`awaiting_merge` past the {MERGE_STALL_HOURS}h stall threshold "
            f"with a mergeable PR but no distinct available merger "
            f"(PLS-411/PLS-856; PLAT-10108 zero-click counter). "
            f"Ready queue: {', '.join(ready_queue) if ready_queue else '(empty)'}. "
            f"See the merge-lane health view."
        ),
    }
    AlertDeduplicationService.submit_alert(data)


def check_merge_stalls(*, threshold_hours: float | None = None) -> dict:
    """Run the merge-lane stall-escalation sweep.

    Two phases (PLAT-10108): phase 1 gathers every awaiting_merge item with a
    mergeable PR past the stall threshold; phase 2 processes each with the
    consolidated ready-queue context. When the resolved merger rung is offline,
    the item is re-resolved excluding that rung so the queue lands on the next
    merge-capable BUILD-13 seat instead of stalling.

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
    stalled_pairs = []

    # Phase 1: gather stalled rows with mergeable PRs.
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
        stalled_pairs.append((item, since))

    # Consolidated ready-queue: every other stalled row from this run.
    ready_queue = [
        entry['item_key'] for entry in stalled if entry['item_key']
    ]

    # Phase 2: re-resolve each stalled row; fail over past an offline rung.
    for item, since in stalled_pairs:
        uid, email = _re_resolve_merger(item)
        via = 'normal'
        if not uid:
            uid, email = _failover_merger(item)
            via = 'failover'
        if uid:
            from tasks.services.workflow_service import WorkflowService
            update_fields = []
            previous_assignee = item.assignee_email
            WorkflowService._apply_resolved_assignee(
                item, uid, email, update_fields,
            )
            WorkflowService._clear_merge_sod_hold(item, update_fields)
            meta = item.metadata if isinstance(item.metadata, dict) else {}
            meta['merge_failover'] = {
                # PLAT-10108 P3 (reika review 6879): via discriminates which
                # path resolved the seat — a normal phase-1 re-resolution is
                # recorded as via=normal, not as a failover.
                'via': via,
                'from_email': previous_assignee or '',
                'to_email': email or '',
                'ready_queue': [
                    key for key in ready_queue
                    if key != (getattr(item, 'item_key', '') or '')
                ],
                'held_at': since.isoformat() if hasattr(since, 'isoformat') else str(since),
                'at': timezone.now().isoformat(),
            }
            item.metadata = meta
            if 'metadata' not in update_fields:
                update_fields.append('metadata')
            item.save(update_fields=update_fields + ['updated_at'])
            reassigned += 1
        else:
            item_queue = [
                key for key in ready_queue
                if key != (getattr(item, 'item_key', '') or '')
            ]
            _fire_stall_alert(item, ready_queue=item_queue)
            escalated += 1

    return {
        'checked': checked,
        'reassigned': reassigned,
        'escalated': escalated,
        'stalled': stalled,
    }

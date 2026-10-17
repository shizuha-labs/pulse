"""PLS-1045: critical-path stall probe (prevention-mechanism for PLS-1044).

Critical-path work strands when the owning seat is saturated/down and no
under-cap seat exists (PLAT-6969 sat `todo` ~2.5h on kai with 20 actionable /
4 urgent in_progress; sora's seat dropped mid-coordination). The seat-
availability chain (enabled-agents ↔ availability ↔ resolver) desyncs, so no
alert fires early.

This probe:
1. Detects a critical-path item stranded (urgent/high, `todo`/`in_progress`
   with no movement, or owned by a down/saturated seat) past a stall threshold
   (default 1h).
2. Escalates to a capacity decision (CoS/flow-owner) — not just a nudge to the
   same seat — so routing/capacity is reconciled before hours are lost.
3. Cross-checks the seat-availability chain (enabled-agents ↔ availability ↔
   resolver) to surface desyncs.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

from tasks.models import Item

# Stall threshold before a critical-path item is escalated to a capacity owner.
STALL_HOURS = float(getattr(settings, 'CRITICAL_PATH_STALL_HOURS', 1))

_ALERT_SOURCE = 'pulse-critical-path-stall'

# Critical-path priorities.
_CRITICAL_PRIORITIES = ('urgent', 'high')
# Statuses that indicate the item is owned-but-not-moving (stranded).
_STRANDED_STATUSES = ('todo', 'in_progress')


def _seat_available(email: str) -> Optional[bool]:
    """Is the owning seat available? None = unknown (chain desync surface)."""
    if not email:
        return None
    from tasks.services.workflow_service import WorkflowService
    try:
        return bool(WorkflowService._is_agent_available_for_assignment(email))
    except Exception:
        return None


def _stranded_since(item):
    return item.updated_at or item.created_at


def _fire_stall_escalation(item, *, seat_available: Optional[bool], reason: str) -> None:
    from tasks.services.deduplication import AlertDeduplicationService
    now = timezone.now()
    data = {
        'item_type': 'alert.critical_path_stall',
        'title': (
            f'Critical-path stall: {getattr(item, "item_key", "") or item.pk} '
            f'({item.priority}) stranded past {STALL_HOURS}h'
        ),
        'source': _ALERT_SOURCE,
        'source_id': f'critical-stall:{item.pk}',
        'severity': 'warning',
        'labels': {
            'item': getattr(item, 'item_key', '') or str(item.pk),
            'alertname': 'CriticalPathStall',
            'priority': item.priority or '',
        },
        'payload': {
            'item_id': item.pk,
            'item_key': getattr(item, 'item_key', '') or '',
            'priority': item.priority or '',
            'assignee': item.assignee_email or '',
            'assignment_group': item.assignment_group or '',
            'seat_available': seat_available,
            'stall_hours': STALL_HOURS,
            'reason': reason,
            'generated_at': now.isoformat(),
        },
        'description': (
            f"Critical-path item `{getattr(item, 'item_key', '') or item.pk}` "
            f"({item.priority}) has been stranded in `{item.status}` past the "
            f"{STALL_HOURS}h stall threshold. Seat availability: "
            f"{'AVAILABLE' if seat_available else 'DOWN/SATURATED' if seat_available is False else 'UNKNOWN (chain desync)'}. "
            f"ESCALATE TO CAPACITY OWNER (CoS/flow-owner): reconcile routing — "
            f"defer a non-critical item, wake a seat, or reroute — do NOT just "
            f"nudge the same seat."
        ),
    }
    AlertDeduplicationService.submit_alert(data)


def check_critical_path_stalls(*, stall_hours: float | None = None) -> dict:
    """Run the critical-path stall probe.

    Returns ``{"checked": int, "stalled": list[dict], "escalated": int}``.
    """
    threshold = stall_hours if stall_hours is not None else STALL_HOURS
    cutoff = timezone.now() - timedelta(hours=threshold)

    qs = Item.objects.filter(
        mode='task',
        priority__in=_CRITICAL_PRIORITIES,
        status__in=_STRANDED_STATUSES,
        resolution__isnull=True,
    ).order_by('updated_at')

    checked = 0
    escalated = 0
    stalled = []
    for item in qs.iterator():
        checked += 1
        since = _stranded_since(item)
        if since is None or since > cutoff:
            continue
        seat_available = _seat_available(item.assignee_email or '')
        reason = 'no_movement'
        if seat_available is False:
            reason = 'seat_down_or_saturated'
        elif seat_available is None:
            reason = 'seat_availability_unknown'
        stalled.append({
            'item_key': getattr(item, 'item_key', '') or '',
            'item_id': item.pk,
            'priority': item.priority or '',
            'assignee': item.assignee_email or '',
            'seat_available': seat_available,
            'reason': reason,
        })
        _fire_stall_escalation(item, seat_available=seat_available, reason=reason)
        escalated += 1
    return {'checked': checked, 'stalled': stalled, 'escalated': escalated}

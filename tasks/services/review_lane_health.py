"""PLAT-7062: review-lane health probe (prevention-mechanism for PLAT-7059).

Retro 27 Aug 2026: review-lane capacity starvation stranded deploy#763 ~6h
while live idle review seats were online but not routed. The review-lane
routing desyncs when a critical-path PR sits in ``in_review`` past a stall
threshold while live, under-capacity review seats exist — no alert fires early.

# PLAT-7062: re-trigger premerge/test gate at head (run was lost/queued).

This probe:
1. Detects a critical-path review item (urgent/high, ``in_review``,
   ``assignment_group='review'``) stranded past a stall threshold (default 1h).
2. Computes the review lane's live seat state (liveness via the resolver's
   ``_seat_routability_verdict``, capacity via WIP accounting) and classifies
   seats as live-idle / live-busy / down.
3. Fires:
   - **routing desync** — a critical-path review item is waiting while live
     idle review seats exist (the item should have been routed to one of them);
   - **review-lane starvation** — a critical-path review item is waiting and
     NO live idle review seat exists (all live seats busy or down), which
     escalates to a capacity decision (CoS/flow-owner) so routing/capacity is
     reconciled before hours are lost.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

from tasks.models import Item

# Stall threshold before a critical-path review item is escalated.
STALL_HOURS = float(getattr(settings, 'REVIEW_LANE_STALL_HOURS', 1))

_ALERT_SOURCE = 'pulse-review-lane-health'

# The review lane's canonical team name.
REVIEW_TEAM = 'review'

# Critical-path priorities.
_CRITICAL_PRIORITIES = ('urgent', 'high')
# Statuses that indicate the item is waiting for review.
_REVIEW_STATUSES = ('in_review',)


def _review_team_roster() -> list[str]:
    """Flattened review-team roster (all tiers)."""
    try:
        from tasks.services.lane_health import _flat_roster
        return _flat_roster(REVIEW_TEAM)
    except Exception:
        return []


def _seat_state(email: str, probe_item) -> dict:
    """Live + capacity state for one review seat.

    Liveness uses the resolver's existing ``_seat_routability_verdict`` reason
    classes (no parallel vocabulary); capacity is WIP accounting independent of
    availability (the lane-health anti-conflation rule). An "idle" seat is live
    AND under capacity.
    """
    from tasks.services.lane_health import _seat_capacity
    from tasks.services.workflow_service import WorkflowService
    live = False
    reasons = []
    try:
        verdict = WorkflowService._seat_routability_verdict(
            probe_item, REVIEW_TEAM, email, enforce_cap=False,
        )
        live = bool(verdict.ok)
        reasons = list(verdict.reasons)
    except Exception:
        reasons.append('verdict_error')
    capacity = _seat_capacity(email, REVIEW_TEAM, probe_item) or {}
    over_capacity = bool(capacity.get('over_capacity'))
    return {
        'email': email,
        'live': live,
        'reasons': sorted(set(reasons)),
        'over_capacity': over_capacity,
    }


def _stranded_since(item):
    return item.updated_at or item.created_at


def _fire_review_lane_alert(item, *, kind: str, live_idle: int, live_busy: int,
                            down: int, seat_count: int, reason: str) -> None:
    from tasks.services.deduplication import AlertDeduplicationService
    now = timezone.now()
    if kind == 'desync':
        title = (
            f'Review-lane routing desync: {getattr(item, "item_key", "") or item.pk} '
            f'({item.priority}) waiting for review while {live_idle} live idle '
            f'review seat(s) exist'
        )
        description = (
            f"Critical-path review item `{getattr(item, 'item_key', '') or item.pk}` "
            f"({item.priority}) has been in `in_review` past the {STALL_HOURS}h "
            f"threshold while {live_idle} live idle review seat(s) are online and "
            f"under capacity. ROUTING DESYNC — route this PR to a live idle seat "
            f"now; do not let it sit."
        )
    else:
        title = (
            f'Review-lane starvation: {getattr(item, "item_key", "") or item.pk} '
            f'({item.priority}) waiting for review with no live idle seat'
        )
        description = (
            f"Critical-path review item `{getattr(item, 'item_key', '') or item.pk}` "
            f"({item.priority}) has been in `in_review` past the {STALL_HOURS}h "
            f"threshold and NO live idle review seat exists (live-busy={live_busy}, "
            f"down={down} of {seat_count}). REVIEW-LANE STARVATION — ESCALATE TO "
            f"CAPACITY OWNER (CoS/flow-owner): wake a seat, deploy a not-deployed "
            f"seat, or reroute — do NOT just nudge the same seat."
        )
    data = {
        'item_type': 'alert.review_lane_health',
        'title': title,
        'source': _ALERT_SOURCE,
        'source_id': f'review-lane:{item.pk}:{kind}',
        'severity': 'warning',
        'labels': {
            'item': getattr(item, 'item_key', '') or str(item.pk),
            'alertname': 'ReviewLaneStarvation' if kind == 'starvation' else 'ReviewLaneRoutingDesync',
            'priority': item.priority or '',
        },
        'payload': {
            'item_id': item.pk,
            'item_key': getattr(item, 'item_key', '') or '',
            'priority': item.priority or '',
            'assignment_group': item.assignment_group or '',
            'kind': kind,
            'live_idle_seats': live_idle,
            'live_busy_seats': live_busy,
            'down_seats': down,
            'seat_count': seat_count,
            'stall_hours': STALL_HOURS,
            'reason': reason,
            'generated_at': now.isoformat(),
        },
        'description': description,
    }
    AlertDeduplicationService.submit_alert(data)


def check_review_lane_health(*, stall_hours: float | None = None) -> dict:
    """Run the review-lane health probe.

    Returns ``{"checked": int, "waiting": list[dict], "desync": list[dict],
    "starvation": list[dict], "escalated": int}``.
    """
    threshold = stall_hours if stall_hours is not None else STALL_HOURS
    cutoff = timezone.now() - timedelta(hours=threshold)

    qs = Item.objects.filter(
        mode='task',
        priority__in=_CRITICAL_PRIORITIES,
        status__in=_REVIEW_STATUSES,
        assignment_group=REVIEW_TEAM,
        resolution__isnull=True,
    ).order_by('updated_at')

    checked = 0
    waiting = []
    for item in qs.iterator():
        checked += 1
        since = _stranded_since(item)
        if since is None or since > cutoff:
            continue
        waiting.append(item)

    if not waiting:
        return {
            'checked': checked, 'waiting': [], 'desync': [], 'starvation': [],
            'escalated': 0,
        }

    # Compute the review lane's live seat state once (probe = oldest waiting item).
    probe = waiting[0]
    seats = [_seat_state(email, probe) for email in _review_team_roster()]
    live_idle = [s for s in seats if s['live'] and not s['over_capacity']]
    live_busy = [s for s in seats if s['live'] and s['over_capacity']]
    down = [s for s in seats if not s['live']]

    desync = []
    starvation = []
    escalated = 0
    for item in waiting:
        entry = {
            'item_key': getattr(item, 'item_key', '') or '',
            'item_id': item.pk,
            'priority': item.priority or '',
            'assignee': item.assignee_email or '',
            'live_idle_seats': len(live_idle),
            'live_busy_seats': len(live_busy),
            'down_seats': len(down),
            'seat_count': len(seats),
        }
        if live_idle:
            desync.append(entry)
            _fire_review_lane_alert(
                item, kind='desync', live_idle=len(live_idle),
                live_busy=len(live_busy), down=len(down), seat_count=len(seats),
                reason='live_idle_seats_exist',
            )
        else:
            starvation.append(entry)
            _fire_review_lane_alert(
                item, kind='starvation', live_idle=0,
                live_busy=len(live_busy), down=len(down), seat_count=len(seats),
                reason='no_live_idle_seat',
            )
        escalated += 1

    return {
        'checked': checked,
        'waiting': waiting,
        'desync': desync,
        'starvation': starvation,
        'escalated': escalated,
    }

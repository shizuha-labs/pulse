"""PLS-1037 (S3): lane-stall detection + alert.

A lane is **stalled** when it has zero routable seats AND queued demand for
longer than the debounce window (default N=10m, env-configurable). This module
reuses the read-only lane-health aggregation (``lane_health``) — including the
PLS-1033 §4.2 anti-conflation failure-mode classification (``classify_stall``,
which lives in S1 so S2/S3/S4 share one shape) — and submits a lane-level alert
(distinct from per-seat alerts) with the failure mode pre-computed:

- ``authority_drift`` — roster changed since the last healthy probe (checked
  FIRST).
- ``rollout_window`` — a fleet-wide agent-runtime digest change is in flight
  (PLS-1061); zero routable seats are expected while pods boot, so the alert is
  annotated "rollout in progress" rather than a raw lane-stall.
- ``seat_not_live`` — zero routable seats; every non-routable verdict is
  liveness-family (runtime/provider fault).
- ``over_capacity`` — zero routable seats; every non-routable verdict is
  capacity-family (WIP accounting, never availability).
- ``mixed`` — multiple families contribute.
- ``none`` — not stalled (routable seat exists or demand empty).
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from tasks.services.lane_health import classify_stall, lane_health

# Debounce window before a lane-stall alert fires (env-configurable).
STALL_DEBOUNCE_SECONDS = getattr(
    settings, 'PULSE_LANE_STALL_DEBOUNCE_SECONDS', 600,
)

_ALERT_SOURCE = 'pulse-lane-stall'


def _stall_since(team: str):
    """Return when the lane entered the stalled state (None if not stalled)."""
    from django.core.cache import cache
    key = f'pulse:lane_stall:since:{team}'
    ts = cache.get(key)
    if ts is None:
        return None
    return timezone.datetime.fromisoformat(ts)


def _set_stall_since(team: str, now) -> None:
    from django.core.cache import cache
    cache.set(f'pulse:lane_stall:since:{team}', now.isoformat(), timeout=86400)


def _clear_stall_since(team: str) -> None:
    from django.core.cache import cache
    cache.delete(f'pulse:lane_stall:since:{team}')


def _submit_stall_alert(team: str, verdict: dict) -> None:
    """Submit (or dedup) a lane-stall alert for a stalled lane."""
    from tasks.services.deduplication import AlertDeduplicationService

    now = timezone.now()
    # PLS-1061: a rollout-window verdict is a normal fleet bump, not a lane
    # failure — annotate "rollout in progress" instead of firing a raw stall.
    rollout = verdict['failure_mode'] == 'rollout_window'
    data = {
        'item_type': 'alert.lane_stall',
        'title': (
            f'Lane stalled: {team} — zero routable seats with queued demand'
            if not rollout
            else f'Lane {team}: rollout in progress — zero routable seats during fleet bump'
        ),
        'source': _ALERT_SOURCE,
        'source_id': f'lane-stall:{team}',
        'severity': 'warning' if not rollout else 'info',
        'labels': {
            'lane': team,
            'failure_mode': verdict['failure_mode'],
            'alertname': 'PulseLaneStall',
            'rollout_in_progress': 'true' if rollout else 'false',
        },
        'payload': {
            'lane': team,
            'failure_mode': verdict['failure_mode'],
            'classification_evidence': verdict['evidence'],
            'routable_seats': verdict['routable_seats'],
            'demand': verdict['demand'],
            'rollout_in_progress': rollout,
            'generated_at': now.isoformat(),
            'lane_health_view': '/shizuha-pulse/api/lane-health/?team=' + team,
        },
        'description': (
            (
                f"Lane `{team}`: a fleet-wide agent-runtime rollout is in "
                f"progress — zero routable seats expected while pods boot. "
                f"{verdict['evidence']}. See the lane-health view for per-seat "
                f"detail. This is NOT a lane failure."
            )
            if rollout
            else (
                f"Lane `{team}` is stalled: {verdict['routable_seats']} routable "
                f"seats with {verdict['demand']} queued items. Failure mode: "
                f"**{verdict['failure_mode']}**. {verdict['evidence']}. "
                f"See the lane-health view for per-seat detail."
            )
        ),
    }
    AlertDeduplicationService.submit_alert(data)


def check_lane_stall(team: str, *, org_slug: str = 'shizuha') -> dict:
    """Run one lane-stall check: classify and fire/resolve the alert."""
    snapshot = lane_health(team, org_slug=org_slug)
    verdict = classify_stall(snapshot)
    now = timezone.now()

    assigned = (snapshot.get('stall') or {}).get('assigned_not_pulled') or {}
    if assigned.get('detected') and not verdict['is_stalled']:
        # Distinct signature: live seat, item not pulled. Fire without
        # conflating failure_mode.
        _submit_stall_alert(team, {
            **verdict,
            'failure_mode': 'assigned_not_pulled',
            'evidence': (
                f"item {assigned.get('item_key')} assigned to "
                f"{assigned.get('assigned_to')} not pulled for "
                f"{assigned.get('age_seconds')}s after {assigned.get('last_assignment_kind')}"
            ),
        })
        return {**verdict, 'team': team, 'action': 'fired', 'assigned_not_pulled': assigned}
    hold = (snapshot.get('stall') or {}).get('hold_override') or {}
    if hold.get('flagged') and not verdict['is_stalled']:
        latest = (hold.get('events') or [{}])[0]
        _submit_stall_alert(team, {
            **verdict,
            'failure_mode': 'hold_override',
            'evidence': (
                f"PR {latest.get('pr')} merged by {latest.get('merger')} "
                f"overriding hold by {latest.get('held_by')} "
                f"(head {latest.get('head_sha')} vs approved {latest.get('approved_sha')})"
            ),
        })
        return {**verdict, 'team': team, 'action': 'fired', 'hold_override': hold}

    if not verdict['is_stalled']:
        _clear_stall_since(team)
        return {**verdict, 'team': team, 'action': 'none'}

    # Debounce: only fire after the lane has been stalled for the window.
    since = _stall_since(team)
    if since is None:
        _set_stall_since(team, now)
        return {**verdict, 'team': team, 'action': 'debouncing'}
    elapsed = (now - since).total_seconds()
    if elapsed < STALL_DEBOUNCE_SECONDS:
        return {**verdict, 'team': team, 'action': 'debouncing'}

    _submit_stall_alert(team, verdict)
    return {**verdict, 'team': team, 'action': 'fired'}


def check_all_lanes(*, org_slug: str = 'shizuha') -> list[dict]:
    """Run the lane-stall check across every active lane."""
    from tasks.models import TeamRoster

    results = []
    teams = list(
        TeamRoster.objects.filter(
            organization_slug=org_slug, is_active=True,
        ).values_list('team_name', flat=True).distinct()
    )
    for team in teams:
        try:
            results.append(check_lane_stall(team, org_slug=org_slug))
        except Exception:
            continue
    return results

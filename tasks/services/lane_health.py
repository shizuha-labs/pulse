"""PLS-1035: lane-health aggregation for the Pulse lane-health API.

Read-only, reporter-accessible health surface for a routing lane (team). It
aggregates the existing source-of-truth helpers into one per-lane snapshot so a
Team-Outage responder can confirm (not diagnose) lane state from one view:

- **roster_version** — the TeamRoster optimistic version + active/cap config.
- **seats** — per-member routability verdict using the existing
  ``_seat_routability_verdict`` reason classes (no parallel vocabulary), plus a
  WIP-accounting capacity verdict (PLS-843/737) independent of availability.
- **resolver** — staffed/routable summary (flat roster, routable-seat count).
- **queue_depth** — active Item counts by status for the lane.
- **last_assignment** — most recent assignment event for the lane.
- **stall** — the PLS-1033 §4.2 anti-conflation failure-mode classification
  (exactly one of authority_drift | seat_not_live | over_capacity | mixed |
  none; authority-drift FIRST; capacity from WIP accounting, never
  availability). S2/S3/S4 build on this shape.
- **merge_sod** — distinct ``no_distinct_available_merger`` merge-lane holds.

This module is deliberately read-only: it never writes rosters, availability,
items, or routing state. It composes ``WorkflowService`` helpers and the
``TeamRoster`` / ``AgentAvailability`` / ``ItemActivity`` models.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from tasks.models import AgentAvailability, Item, ItemActivity, TeamRoster

# Statuses that count as "active work" for a lane's queue depth.
_ACTIVE_QUEUE_STATUSES = {
    'open', 'todo', 'in_progress', 'in_review', 'applying', 'awaiting_merge',
    'awaiting_deploy', 'scheduled', 'found', 'accepted', 'needs_triage',
}

# Reason families per the PLS-1033 contract §4.2 (runbook table). These are the
# existing ``_seat_routability_verdict`` reason classes — never a parallel set.
# Exception (PLS-721): ``no_candidate_evidence`` is a hold-EMISSION class, not a
# verdict class — emitted when a routing pool produced zero per-seat verdicts
# (empty candidates / all hard-excluded / legacy-filtered post-verdict). It is
# operationally a liveness condition (no scoreable seat), so it classifies here.
_LIVENESS_REASONS = {'seat_not_live', 'no_candidate_evidence'}
_CAPACITY_REASONS = {'over_capacity'}
_AUTHORITY_REASONS = {
    'team_unstaffed', 'team_unknown', 'org_roster_mismatch', 'seat_not_team_member',
}
_MERGE_SOD_REASONS = {
    'merge_sod_author_identity', 'merge_sod_reviewer_identity',
    'merge_sod_identity_unavailable', 'merge_sod_evidence_unavailable',
    'no_distinct_available_merger',
}
_REVIEW_SOD_REASONS = {
    'review_sod_author_identity', 'review_sod_author_evidence_unavailable',
}
_SOD_REASONS = _MERGE_SOD_REASONS | _REVIEW_SOD_REASONS

# Lane demand that SoD applies to (author≠reviewer / author≠merger).
_SOD_DEMAND_STATUSES = {
    'review': {'in_review'},
    'architecture': {'in_review'},
    'merge': {'awaiting_merge'},
}

# Cache key prefix for the last-healthy roster version (authority-drift baseline).
_HEALTHY_ROSTER_CACHE_KEY = 'pulse:lane_stall:healthy_roster:{team}'
_HOLD_OVERRIDE_CACHE_KEY = 'pulse:lane_health:hold_override:{team}'
_HOLD_OVERRIDE_NOTE = (
    'merge-without-exact-head / hold-override event — a discipline signal, '
    'NOT a failure mode'
)
_DEFAULT_ASSIGNED_NOT_PULLED_SECONDS = 7200
_READY_OLDEST_STATUSES = {
    'open', 'todo', 'needs_triage', 'found', 'accepted',
}

# PLS-1061: rollout-window classification (4th failure mode). A fleet-wide
# agent-runtime bump can transiently leave a lane with zero routable seats while
# pods boot (ichi's 2026-08-28 fleet verification: digest 8878023b, ready=0 on
# merge-lane seats, self-recovered). That is a NORMAL rollout, not a lane failure.
# The fleet digest + when it changed is written by the deploy/Hive reconciler to
# this cache key ({"digest": str, "changed_at": ISO8601}); a lane whose seats are
# down while that digest changed within the window is classified rollout_window.
_FLEET_RUNTIME_DIGEST_CACHE_KEY = 'pulse:lane_health:fleet_runtime_digest'
# Window (seconds) after a fleet digest change during which zero-routable seats
# are attributed to the rollout rather than a lane failure (env-configurable).
_ROLLOUT_WINDOW_SECONDS = getattr(
    settings, 'PULSE_LANE_ROLLOUT_WINDOW_SECONDS', 600,
)

# PLS-1187: per-lane snapshot cache (stale-while-revalidate). The no-param
# endpoint serves ``all_lanes_health()`` = a serial ``lane_health()`` rebuild
# for every active lane, per request. Measured in production
# (shizuha-tasks-e9504fb, 2026-09-12): ~3.7s per lane x ~23 lanes = ~85s, so
# the authenticated probe timed out at every client ceiling (15-30s) even with
# the pulse#452/#475 degraded-cache fixes live and the cache Redis healthy —
# the healthy-system aggregation cost itself was never SLA-capable at
# production lane count. Shape: each lane's snapshot is cached under a fresh
# key (short TTL) and a stale key (long horizon). A read serves fresh
# immediately; on fresh-expiry it serves the stale snapshot and refreshes
# out-of-band (mutex-guarded daemon thread, one refresher per lane); a cold
# cache builds synchronously once. Fail-open throughout: any cache error
# falls through to a live build (bounded by the PLS-1187 2s cache-client
# OPTIONS in settings.CACHES). Keys are team-scoped, matching this module's
# existing per-team cache keys (single-org deployment reality).
_LANE_SNAPSHOT_FRESH_KEY = 'pulse:lane_health:snapshot:{team}'
_LANE_SNAPSHOT_STALE_KEY = 'pulse:lane_health:snapshot_stale:{team}'
_LANE_SNAPSHOT_REFRESH_LOCK_KEY = 'pulse:lane_health:snapshot_refresh_lock:{team}'
_LANE_SNAPSHOT_FRESH_TTL = getattr(
    settings, 'PULSE_LANE_HEALTH_SNAPSHOT_FRESH_TTL', 30,
)
_LANE_SNAPSHOT_STALE_TTL = getattr(
    settings, 'PULSE_LANE_HEALTH_SNAPSHOT_STALE_TTL', 900,
)
_LANE_SNAPSHOT_REFRESH_LOCK_TTL = 30
# PLS-1244: a cold lane (no fresh AND no stale snapshot — post-deploy,
# post-flush, or after `_invalidate_all_lane_snapshots` with an empty stale
# horizon) must never block the request path on the full build (~3.7s/lane
# measured, ~85-120s across 23 lanes). The cold path spawns the background
# build and serves this bounded placeholder immediately; the view turns it
# into 503 + Retry-After so clients fast-fail instead of timing out.
_LANE_SNAPSHOT_BUILD_RETRY_AFTER = getattr(
    settings, 'PULSE_LANE_HEALTH_BUILD_RETRY_AFTER_SECONDS', 5,
)


def _canonical_team(team_name: str) -> str:
    from tasks.services.workflow_service import WorkflowService
    return WorkflowService._canonical_team_name(team_name or '')


def _flat_roster(team_name: str, item=None) -> list[str]:
    """Flattened member emails for a team's effective roster (all tiers)."""
    from tasks.services.workflow_service import WorkflowService
    try:
        # _team_roster_entry_for_item requires an item's project org; for the
        # lane-health view (no specific item) read the effective roster directly.
        pod = WorkflowService._pod_roster()
        entry = pod.get(team_name)
        if entry is None:
            return []
        return WorkflowService._flat_roster(entry)
    except Exception:
        return []


def _probe_item(team_name: str) -> Optional[Item]:
    """Stalest active item for a lane — the probe for item-relative verdicts.

    Mirrors the org-health sweep's probe (the oldest queued item) so the
    per-seat routability verdict is item-relative exactly like the live
    resolver, not a synthetic read.
    """
    try:
        return (
            Item.objects.filter(
                mode='task',
                assignment_group=team_name,
                status__in=_ACTIVE_QUEUE_STATUSES,
            )
            .order_by('created_at')
            .first()
        )
    except Exception:
        return None


def _seat_capacity(email: str, team_name: str, probe_item) -> dict:
    """WIP-accounting capacity verdict (PLS-843/737), independent of availability.

    Capacity is computed from the seat's active/ready slot load vs the lane's
    WIP caps — never from resolver/availability verdicts (the contract's
    binding anti-conflation rule).
    """
    from tasks.services.workflow_service import WorkflowService
    base = {
        'enforced': False,
        'active_load': None,
        'ready_load': None,
        'active_wip_cap': None,
        'ready_wip_cap': None,
        'over_capacity': False,
    }
    try:
        roster_config = WorkflowService._wip_roster_config(team_name, probe_item)
        if roster_config is None:
            return base
        active_cap = roster_config.active_wip_cap
        ready_cap = roster_config.ready_wip_cap
        active_load = WorkflowService._agent_slot_load(probe_item, email, 'active')
        ready_load = WorkflowService._agent_slot_load(probe_item, email, 'ready')
        over = (
            (active_cap is not None and active_cap > 0 and active_load >= active_cap)
            or (ready_cap is not None and ready_cap > 0 and ready_load >= ready_cap)
        )
        return {
            'enforced': True,
            'active_load': active_load,
            'ready_load': ready_load,
            'active_wip_cap': active_cap,
            'ready_wip_cap': ready_cap,
            'over_capacity': over,
        }
    except Exception:
        return base


def _seat_verdict(email: str, team_name: str, probe_item, runtime=None) -> dict:
    """Per-seat routability verdict using the resolver's reason classes.

    Calls ``_seat_routability_verdict`` with ``enforce_cap=False`` so the
    liveness/staffing/capability/status-role/SoD reason classes are the existing
    vocabulary (no parallel set). Capacity is attached separately from WIP
    accounting (``_seat_capacity``), never from availability.
    """
    from tasks.services.workflow_service import WorkflowService
    reasons = []
    live = False
    try:
        verdict = WorkflowService._seat_routability_verdict(
            probe_item, team_name, email, enforce_cap=False,
        )
        reasons = list(verdict.reasons)
        live = verdict.ok
    except Exception:
        reasons.append('verdict_error')
    # Surface any durable operator availability override (informational).
    try:
        row = AgentAvailability.objects.filter(email__iexact=email).first()
        if row is not None:
            if row.status == 'disabled':
                reasons.append('operator_disabled')
            elif row.status == 'on_leave':
                reasons.append('on_leave')
            elif not row.active:
                reasons.append('operator_inactive')
    except Exception:
        pass
    capacity = _seat_capacity(email, team_name, probe_item)
    # PLS-1164 / PLS-1033 contract line: surface roster members that fail
    # principal resolution as a per-seat phantom-identity flag instead of
    # letting routing bounce them silently. ``resolution_error`` (authority
    # unreachable) is deliberately NOT a phantom — an outage must never render
    # as an identity defect.
    phantom_identity = False
    try:
        from .canonical_identity import classify_roster_email
        status, _canonical = classify_roster_email(email)
        phantom_identity = status == 'phantom'
        if phantom_identity:
            reasons.append('phantom_roster_identity')
    except Exception:
        pass
    return {
        'email': email,
        'live': live,
        'reasons': sorted(set(reasons)),
        'phantom_identity': phantom_identity,
        'capacity': capacity,
        # PLS-1061: per-seat agent-runtime digest/version signal. Per-seat
        # digest is not independently tracked; the fleet-wide runtime digest +
        # last-change timestamp (the rollout-window signal) is surfaced so a
        # responder can tell rollout_window from seat_not_live at a glance.
        # PLS-1187: the fleet-wide digest is identical for every seat — the
        # caller computes it once per lane and passes it in. Calling it here
        # paid one cache round-trip PER SEAT (seats × lanes cache.gets per
        # request) and multiplied the degraded-cache stall surface.
        'runtime': runtime,
    }


def _queue_depth(team_name: str) -> dict:
    """Active Item counts by status for a lane (assignment_group)."""
    qs = Item.objects.filter(mode='task', assignment_group=team_name)
    counts = dict(Counter(qs.values_list('status', flat=True)))
    active = {
        s: counts.get(s, 0)
        for s in sorted(_ACTIVE_QUEUE_STATUSES)
        if counts.get(s, 0)
    }
    oldest = (
        qs.filter(status__in=_READY_OLDEST_STATUSES)
        .exclude(assignee_email='')
        .order_by('created_at')
        .first()
    )
    oldest_payload = {
        'oldest_item_key': None,
        'oldest_assigned_to': None,
        'oldest_state': None,
        'oldest_assigned_at': None,
    }
    if oldest is not None:
        oldest_payload = {
            'oldest_item_key': getattr(oldest, 'item_key', None) or '',
            'oldest_assigned_to': (oldest.assignee_email or '').strip().lower() or None,
            'oldest_state': oldest.status,
            # Heuristic: Item.updated_at, not a dedicated assignment timestamp.
            # Unrelated comments/status writes can understate not-pulled age.
            'oldest_assigned_at': (
                oldest.updated_at.isoformat() if oldest.updated_at else None
            ),
        }
    return {
        'by_status': active,
        'total_active': sum(counts.get(s, 0) for s in _ACTIVE_QUEUE_STATUSES),
        'total_all': qs.count(),
        **oldest_payload,
    }


def previous_assignee_from_comment(comment: str) -> str:
    """Extract the prior owner from a failover/reassign activity comment."""
    text = comment or ''
    m = re.search(r'primary\s+\**([\w.@+-]+)\**', text, re.I)
    if not m:
        m = re.search(r'from\s+\**([\w.@+-]+)\**', text, re.I)
    return (m.group(1) if m else '').strip()


def _last_assignment(team_name: str) -> Optional[dict]:
    """Most recent assignment event for a lane (ItemActivity action='assigned')."""
    try:
        ev = (
            ItemActivity.objects.filter(
                action='assigned',
                item__assignment_group=team_name,
                item__mode='task',
            )
            .select_related('item')
            .order_by('-created_at')
            .first()
        )
    except Exception:
        return None
    if ev is None:
        return None
    comment = (getattr(ev, 'comment', None) or getattr(ev, 'details', None) or '')
    if not isinstance(comment, str):
        comment = str(comment or '')
    kind = 'assigned'
    lowered = comment.lower()
    if 'auto-failover' in lowered or 'failover' in lowered:
        kind = 'failover'
    elif 'reassign' in lowered or 'rebalance' in lowered:
        kind = 'reassigned'
    previous = previous_assignee_from_comment(comment)
    return {
        'at': ev.created_at.isoformat() if ev.created_at else None,
        'assignee': ev.user_email or '',
        'item_key': getattr(ev.item, 'item_key', None) or '',
        'kind': kind,
        'previous_assignee': previous,
    }


def _merge_lane_sod(team_name: str) -> dict:
    """Surface ``no_distinct_available_merger`` merge-lane holds as a distinct signal."""
    holds = []
    try:
        # The stored status is workflow-normalized (awaiting_merge → in_progress),
        # so filter on the JSON hold itself, not the display status.
        qs = (
            Item.objects.filter(
                mode='task',
                assignment_group=team_name,
                metadata__merge_sod_hold__reason='no_distinct_available_merger',
            )
            .order_by('-updated_at')[:20]
        )
        for item in qs:
            meta = getattr(item, 'metadata', None) or {}
            hold = meta.get('merge_sod_hold') if isinstance(meta, dict) else None
            if isinstance(hold, dict) and hold.get('reason') == 'no_distinct_available_merger':
                held_at = hold.get('held_at')
                if held_at is not None and hasattr(held_at, 'isoformat'):
                    held_at = held_at.isoformat()
                holds.append({
                    'item_key': getattr(item, 'item_key', None) or '',
                    'reason': 'no_distinct_available_merger',
                    'held_at': held_at,
                })
    except Exception:
        holds = []
    return {'count': len(holds), 'holds': holds}


def _pending_sod_demand(team_name: str):
    """Active items whose SoD (author≠reviewer/merger) binds this lane."""
    statuses = _SOD_DEMAND_STATUSES.get(team_name, {'in_review', 'awaiting_merge'})
    try:
        return list(
            Item.objects.filter(
                mode='task',
                assignment_group=team_name,
                status__in=statuses,
            ).only('id', 'item_key', 'created_by_email', 'status')[:50]
        )
    except Exception:
        return []


def _annotate_sod(seats: list[dict], team_name: str, pending: list) -> None:
    """Mark seats SoD-disqualified for every pending demand item (PLS-1133)."""
    if not pending:
        for seat in seats:
            seat['sod_disqualified'] = False
            seat['sod_items'] = []
        return
    from tasks.services.workflow_service import WorkflowService
    for seat in seats:
        email = (seat.get('email') or '').lower()
        blocked = []
        for item in pending:
            author = (getattr(item, 'created_by_email', None) or '').lower()
            reasons = set(seat.get('reasons') or ())
            try:
                verdict = WorkflowService._seat_routability_verdict(
                    item, team_name, seat.get('email') or '', enforce_cap=False,
                )
                reasons = set(verdict.reasons)
            except Exception:
                if author and email and author == email:
                    reasons = reasons | {'review_sod_author_identity'}
            sod = sorted(reasons & _SOD_REASONS)
            if sod:
                blocked.append({
                    'item_key': getattr(item, 'item_key', None) or '',
                    'reasons': sod,
                })
        # Disqualified only when they cannot legally act on ANY pending item.
        seat['sod_disqualified'] = bool(pending) and len(blocked) == len(pending)
        seat['sod_items'] = blocked if seat['sod_disqualified'] else []


def _seat_family(reasons, capacity: dict, live: bool) -> str:
    """Map a seat's reason set + WIP capacity to its dominant failure family.

    Capacity (from WIP accounting) only dominates for a **live** seat that is
    over its WIP cap. A seat that is not live keeps its reason family (e.g.
    ``seat_not_live`` → liveness) even if it also reads over-capacity — the
    anti-conflation rule: capacity never masks a liveness verdict.
    """
    reasons = set(reasons or ())
    if live and capacity and capacity.get('over_capacity'):
        return 'capacity'
    if reasons & _LIVENESS_REASONS:
        return 'liveness'
    if reasons & _CAPACITY_REASONS:
        return 'capacity'
    if reasons & _AUTHORITY_REASONS:
        return 'authority'
    if reasons & _MERGE_SOD_REASONS:
        return 'sod'
    return 'other'


def _last_healthy_roster_version(team: str) -> Optional[str]:
    try:
        return cache.get(_HEALTHY_ROSTER_CACHE_KEY.format(team=team))
    except Exception:
        # PLS-1187: degraded cache Redis fails open — authority-drift
        # detection degrades to "no baseline", never raises into the lane.
        return None


def _remember_healthy_roster(team: str, version) -> None:
    if version is not None:
        try:
            cache.set(_HEALTHY_ROSTER_CACHE_KEY.format(team=team), str(version), timeout=86400)
        except Exception:
            # PLS-1187: best-effort remember — a degraded cache Redis must not
            # fail the lane snapshot that just classified healthy.
            pass


def _fleet_runtime_digest() -> Optional[dict]:
    """Fleet-wide agent-runtime digest signal (PLS-1061).

    Returns ``{"digest": str, "changed_at": str}`` (or None) as written by the
    deploy/Hive reconciler to ``_FLEET_RUNTIME_DIGEST_CACHE_KEY`` when a
    fleet-wide agent-runtime bump lands. ``changed_at`` is the ISO8601 moment
    the digest changed — the start of the rollout window.
    """
    try:
        value = cache.get(_FLEET_RUNTIME_DIGEST_CACHE_KEY)
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    digest = value.get('digest')
    changed_at = value.get('changed_at')
    if not digest or not changed_at:
        return None
    return {'digest': str(digest), 'changed_at': str(changed_at)}


def record_fleet_runtime_digest(digest: str) -> Optional[dict]:
    """Producer for the fleet-wide agent-runtime digest signal (PLS-1061).

    Called by the deploy/Hive reconciler (or ``record_fleet_runtime_digest``
    management command) when a fleet-wide agent-runtime bump lands. Writes
    ``{"digest": str, "changed_at": ISO8601}`` to the cache key — ``changed_at``
    is only refreshed when the digest actually changes, so the rollout window
    starts at the bump, not on every reconcile tick.

    Returns the written signal dict (or None on failure).
    """
    digest = (digest or '').strip()
    if not digest:
        return None
    try:
        previous = _fleet_runtime_digest()
        now = timezone.now()
        if previous is not None and previous['digest'] == digest:
            # Same digest — keep the original changed_at (rollout already counted).
            return previous
        signal = {'digest': digest, 'changed_at': now.isoformat()}
        cache.set(_FLEET_RUNTIME_DIGEST_CACHE_KEY, signal, timeout=86400)
        # PLS-1187: the digest change re-classes every lane's stall verdict
        # (rollout_window). Drop the fresh snapshot keys so the next read
        # rebuilds with the new signal; stale snapshots keep serving while the
        # rebuild runs.
        _invalidate_all_lane_snapshots()
        return signal
    except Exception:
        return None


def _rollout_in_progress() -> bool:
    """True when a fleet-wide agent-runtime digest change is in flight.

    A digest change recorded within ``_ROLLOUT_WINDOW_SECONDS`` means pods are
    still booting on the new runtime — zero-routable seats in that window are
    expected rollout behaviour, not a lane failure.
    """
    signal = _fleet_runtime_digest()
    if signal is None:
        return False
    try:
        changed_at = timezone.datetime.fromisoformat(signal['changed_at'])
    except (TypeError, ValueError):
        return False
    if timezone.is_naive(changed_at):
        changed_at = timezone.make_aware(changed_at, timezone.utc)
    return (timezone.now() - changed_at).total_seconds() <= _ROLLOUT_WINDOW_SECONDS


def classify_stall(snapshot: dict) -> dict:
    """PLS-1033 §4.2: anti-conflation failure-mode classification.

    Returns exactly one of ``authority_drift | rollout_window | seat_not_live |
    over_capacity | sod_exhausted | mixed | none``. ``sod_exhausted`` is the
    early distinct case: raw ``routable_seats > 0`` but every live seat is
    SoD-disqualified for the lane's pending demand (PLS-1133). Then
    **authority-drift is checked FIRST** among the roster/family modes (roster
    version advanced vs the last healthy probe), then **rollout-window** (a
    fleet-wide agent-runtime digest change is in flight — PLS-1061). Capacity is
    decided from WIP accounting (per-seat ``capacity.over_capacity``), never
    from availability verdicts. A lane is stalled when it has zero routable
    seats AND queued demand.

    Returns ``{"is_stalled": bool, "failure_mode": str, "evidence": str,
    "routable_seats": int, "effective_routable": int, "demand": int}``.
    """
    team = snapshot.get('team', '')
    resolver = snapshot.get('resolver', {}) or {}
    queue = snapshot.get('queue_depth', {}) or {}
    seats = snapshot.get('seats', []) or []
    roster = snapshot.get('roster', {}) or {}

    routable = int(resolver.get('routable_seats', 0) or 0)
    effective = int(resolver.get('effective_routable', routable) or 0)
    demand = int(queue.get('total_active', 0) or 0)

    if demand == 0:
        return {
            'is_stalled': False,
            'failure_mode': 'none',
            'evidence': 'demand empty',
            'routable_seats': routable,
            'effective_routable': effective,
            'demand': demand,
        }

    if routable > 0 and effective == 0:
        return {
            'is_stalled': True,
            'failure_mode': 'sod_exhausted',
            'evidence': (
                f'{routable} raw routable seat(s) but 0 effective; every live '
                'seat is SoD-disqualified for pending demand'
            ),
            'routable_seats': routable,
            'effective_routable': 0,
            'demand': demand,
        }

    if routable > 0:
        return {
            'is_stalled': False,
            'failure_mode': 'none',
            'evidence': 'routable seat exists or demand empty',
            'routable_seats': routable,
            'effective_routable': effective,
            'demand': demand,
        }

    # authority-drift FIRST: roster version advanced vs the last healthy probe.
    current_version = roster.get('version')
    last_healthy = _last_healthy_roster_version(team)
    if (
        last_healthy is not None
        and current_version is not None
        and str(current_version) != last_healthy
    ):
        return {
            'is_stalled': True,
            'failure_mode': 'authority_drift',
            'evidence': (
                f'roster version advanced {last_healthy} → {current_version}; '
                'routing authority moved while demand is queued'
            ),
            'routable_seats': routable,
            'effective_routable': effective,
            'demand': demand,
        }

    # rollout-window SECOND (PLS-1061): a fleet-wide agent-runtime digest change
    # is in flight — zero routable seats during pod boot is expected rollout
    # behaviour, not a lane failure. Checked before the per-seat family scan so
    # a bump never surfaces as seat_not_live / over_capacity / mixed.
    if _rollout_in_progress():
        signal = _fleet_runtime_digest() or {}
        return {
            'is_stalled': True,
            'failure_mode': 'rollout_window',
            'evidence': (
                f'fleet agent-runtime digest {signal.get("digest", "?")} changed '
                f'at {signal.get("changed_at", "?")}; rollout in progress — '
                'zero routable seats expected while pods boot'
            ),
            'routable_seats': routable,
            'demand': demand,
        }

    # Dominant non-routable family across seats (capacity from WIP accounting).
    family_counts = Counter()
    family_seats = {}
    for seat in seats:
        live = bool(seat.get('live'))
        if live and not (seat.get('capacity') or {}).get('over_capacity'):
            continue
        fam = _seat_family(seat.get('reasons'), seat.get('capacity'), live)
        family_counts[fam] += 1
        family_seats.setdefault(fam, []).append(seat.get('email', ''))

    if not family_counts:
        mode = 'seat_not_live'
        evidence = 'zero routable seats; no per-seat reason detail available'
    elif len(family_counts) == 1:
        (only_fam, count) = next(iter(family_counts.items()))
        if only_fam == 'liveness':
            mode = 'seat_not_live'
        elif only_fam == 'capacity':
            mode = 'over_capacity'
        elif only_fam == 'authority':
            mode = 'authority_drift'
        else:
            mode = 'seat_not_live'
        evidence = (
            f'zero routable seats; all {count} non-routable verdicts '
            f'{only_fam}-family ({", ".join(family_seats[only_fam])})'
        )
    else:
        mode = 'mixed'
        parts = [
            f'{fam}-family x{count} ({", ".join(family_seats[fam])})'
            for fam, count in family_counts.items()
        ]
        evidence = 'zero routable seats; multiple families: ' + '; '.join(parts)

    return {
        'is_stalled': True,
        'failure_mode': mode,
        'evidence': evidence,
        'routable_seats': routable,
        'effective_routable': effective,
        'demand': demand,
    }


def record_hold_override_event(team: str, event: dict) -> None:
    """Append a merge-audit hold-override event for a lane (PLS-1062)."""
    key = _HOLD_OVERRIDE_CACHE_KEY.format(team=_canonical_team(team))
    events = list(cache.get(key) or [])
    events.append(event)
    cache.set(key, events[-50:], timeout=86400 * 14)
    # PLS-1187: the lane's cached snapshot now predates this event — drop the
    # fresh key so the next read rebuilds with it (stale keeps serving meanwhile).
    _invalidate_lane_snapshot(_canonical_team(team))


def maybe_record_hold_override_from_merge(
    item,
    *,
    head_sha: str,
    repo: str = '',
    number: int | None = None,
    merger: str = '',
    reviews: list | None = None,
    merged_at=None,
) -> dict | None:
    """Producer: record a hold-override when latest APPROVED commit_id != head.

    Called from the merge-attestation path (provider-verified merge). Exact-head
    merges return None and do not write the cache.
    """
    head = (head_sha or '').strip().lower()
    if not head:
        return None
    rows = reviews
    if rows is None:
        meta = getattr(item, 'metadata', None) or {}
        prs = meta.get('pull_requests') if isinstance(meta, dict) else None
        rows = []
        if isinstance(prs, list):
            for pr in prs:
                if not isinstance(pr, dict):
                    continue
                if number is not None and int(pr.get('number') or 0) != int(number):
                    continue
                rows.extend(pr.get('reviews') or pr.get('origin_reviews') or [])
    approved_sha = ''
    held_by = ''
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        state = (row.get('state') or row.get('verdict') or '').upper()
        cid = (row.get('commit_id') or row.get('commitId') or '').strip().lower()
        login = ''
        user = row.get('user') or {}
        if isinstance(user, dict):
            login = user.get('login') or user.get('email') or ''
        login = login or row.get('author') or row.get('email') or ''
        if state in ('CHANGES_REQUESTED', 'REQUEST_CHANGES', 'HOLD'):
            held_by = login or held_by
        if state == 'APPROVED' and cid:
            approved_sha = cid
            if not held_by:
                held_by = login
    if not approved_sha or approved_sha == head:
        return None
    team = (getattr(item, 'assignment_group', None) or 'merge')
    event = {
        'lane': team,
        'pr': f'{repo}#{number}' if repo and number else (getattr(item, 'item_key', '') or ''),
        'merger': merger or '',
        'held_by': held_by,
        'head_sha': head,
        'approved_sha': approved_sha,
        'at': merged_at.isoformat() if hasattr(merged_at, 'isoformat') else merged_at,
    }
    record_hold_override_event(team, event)
    return event


def hold_override_events(snapshot: dict) -> dict:
    """PLS-1062: stall *signature* (not a failure_mode).

    Exact-head / hold-override merges. Lane may still have routable seats.
    TERM: empty events → flagged=False. Exact-head (head_sha == approved_sha)
    events are dropped (no false positive).
    """
    team = snapshot.get('team') or ''
    injected = list(snapshot.get('hold_override_events') or [])
    try:
        cached = list(cache.get(_HOLD_OVERRIDE_CACHE_KEY.format(team=team)) or [])
    except Exception:
        # PLS-1187: degraded cache Redis fails open to no cached events —
        # injected events still evaluate, the lane still returns.
        cached = []
    events = []
    for raw in injected + cached:
        if not isinstance(raw, dict):
            continue
        head = (raw.get('head_sha') or '').strip().lower()
        approved = (raw.get('approved_sha') or '').strip().lower()
        if head and approved and head == approved:
            continue
        events.append({
            'lane': raw.get('lane') or team,
            'pr': raw.get('pr') or '',
            'merger': raw.get('merger') or '',
            'held_by': raw.get('held_by') or '',
            'head_sha': raw.get('head_sha') or '',
            'approved_sha': raw.get('approved_sha') or '',
            'at': raw.get('at'),
        })
    return {
        'flagged': bool(events),
        'events': events,
        'note': _HOLD_OVERRIDE_NOTE if events else '',
    }


def assigned_not_pulled(snapshot: dict) -> dict:
    """PLS-1055: stall *signature* (not a failure_mode).

    Item assigned to a live seat, not pulled past threshold, with a recent
    reassignment/failover. The lane may still have routable seats.
    """
    empty = {
        'detected': False,
        'item_key': None,
        'assigned_to': None,
        'age_seconds': None,
        'threshold_seconds': int(getattr(
            settings, 'PULSE_ASSIGNED_NOT_PULLED_SECONDS',
            _DEFAULT_ASSIGNED_NOT_PULLED_SECONDS,
        ) or _DEFAULT_ASSIGNED_NOT_PULLED_SECONDS),
        'last_assignment_kind': None,
    }
    queue = snapshot.get('queue_depth') or snapshot.get('queue') or {}
    last = snapshot.get('last_assignment') or {}
    seats = snapshot.get('seats') or []
    email = (queue.get('oldest_assigned_to') or '').strip().lower()
    item_key = queue.get('oldest_item_key')
    assigned_at = queue.get('oldest_assigned_at')
    kind = (last.get('kind') or 'assigned')
    if not email or not item_key or not assigned_at:
        return empty
    live = any(
        (s.get('email') or '').strip().lower() == email and s.get('live')
        for s in seats
    )
    if not live:
        return empty
    if kind not in ('failover', 'reassigned'):
        return empty
    try:
        when = assigned_at
        if isinstance(when, str):
            when = timezone.datetime.fromisoformat(when.replace('Z', '+00:00'))
        if timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.get_current_timezone())
        age = (timezone.now() - when).total_seconds()
    except (TypeError, ValueError):
        return empty
    threshold = empty['threshold_seconds']
    if age < threshold:
        return empty
    return {
        'detected': True,
        'item_key': item_key,
        'assigned_to': email,
        'age_seconds': int(age),
        'threshold_seconds': threshold,
        'last_assignment_kind': kind,
    }


def _snapshot_cache_enabled() -> bool:
    """Snapshot cache kill-switch (hermetic test settings disable it so the
    existing suite keeps exercising the live builder path)."""
    return bool(getattr(settings, 'PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED', True))


def lane_health(team_name: str, *, org_slug: str = 'shizuha') -> dict:
    """One per-lane health snapshot (read-only), served through the
    PLS-1187 snapshot cache (stale-while-revalidate)."""
    if not _snapshot_cache_enabled():
        return _lane_health_uncached(team_name, org_slug=org_slug)
    team = _canonical_team(team_name)
    try:
        cached = cache.get(_LANE_SNAPSHOT_FRESH_KEY.format(team=team))
    except Exception:
        cached = None
    if cached is not None:
        return cached
    try:
        stale = cache.get(_LANE_SNAPSHOT_STALE_KEY.format(team=team))
    except Exception:
        stale = None
    if stale is not None:
        # Fresh-expired: serve the last-known snapshot immediately and refresh
        # out-of-band so an expired lane never blocks the request path (the
        # all-lanes aggregation must stay sub-second at production lane count).
        _spawn_lane_snapshot_refresh(team, org_slug=org_slug)
        return stale
    # Cold cache (post-deploy / post-flush): PLS-1244 — never block the request
    # path on the full build. Spawn the background build and serve a bounded
    # building placeholder (the view escalates it to 503 + Retry-After). If the
    # refresh cannot be scheduled at all (degraded cache backend), fail open to
    # the legacy synchronous build rather than serve placeholders forever.
    if _spawn_lane_snapshot_refresh(team, org_slug=org_slug):
        return _building_placeholder(team, org_slug=org_slug)
    return _build_and_store_lane_snapshot(team, org_slug=org_slug)


def _building_placeholder(team: str, *, org_slug: str) -> dict:
    """Bounded cold-window placeholder for a lane whose snapshot is building.

    Shape-compatible with a real snapshot at the top-level keys consumers read
    unconditionally (team/organization/generated_at), with explicit
    ``build_status='building'`` + ``degraded`` markers so no consumer can
    mistake it for authoritative liveness data. ``stall`` is deliberately NOT
    classified here — an empty roster must never read as a healthy or stalled
    verdict; consumers gate on ``build_status`` first.
    """
    return {
        'team': team,
        'organization': org_slug,
        'build_status': 'building',
        'degraded': True,
        'retry_after_seconds': _LANE_SNAPSHOT_BUILD_RETRY_AFTER,
        'roster': {'version': None, 'active': None, 'member_count': 0},
        'seats': [],
        'resolver': {
            'staffed': None,
            'routable_seats': None,
            'effective_routable': None,
            'seat_count': 0,
            'unavailable_seats': 0,
        },
        'queue_depth': {},
        'last_assignment': None,
        'merge_sod': {},
        'stall': {'is_stalled': False, 'reason': 'snapshot_building'},
        'generated_at': timezone.now().isoformat(),
    }


def _build_and_store_lane_snapshot(team: str, *, org_slug: str) -> dict:
    """Build one lane snapshot live and publish it to fresh+stale keys."""
    snapshot = _lane_health_uncached(team, org_slug=org_slug)
    try:
        cache.set(
            _LANE_SNAPSHOT_FRESH_KEY.format(team=team),
            snapshot, timeout=_LANE_SNAPSHOT_FRESH_TTL,
        )
        cache.set(
            _LANE_SNAPSHOT_STALE_KEY.format(team=team),
            snapshot, timeout=_LANE_SNAPSHOT_STALE_TTL,
        )
    except Exception:
        pass  # fail-open: a degraded cache must never break the read path
    return snapshot


def _spawn_lane_snapshot_refresh(team: str, *, org_slug: str) -> bool:
    """Refresh one lane's snapshot in a daemon thread, at most one refresher
    per lane at a time (cache.add mutex). Never raises.

    Returns True when a build is schedulable/already in flight (a caller on
    the request path may serve a building placeholder); False only when the
    cache backend is too degraded to schedule one (the caller then fails open
    to a synchronous build).
    """
    lock_key = _LANE_SNAPSHOT_REFRESH_LOCK_KEY.format(team=team)
    try:
        if not cache.add(lock_key, '1', timeout=_LANE_SNAPSHOT_REFRESH_LOCK_TTL):
            return True  # another worker is already refreshing this lane
    except Exception:
        return False  # degraded cache: cannot schedule a background refresh
    try:
        threading.Thread(
            target=_refresh_lane_snapshot,
            args=(team,),
            kwargs={'org_slug': org_slug, 'lock_key': lock_key},
            daemon=True,
            name=f'lane-health-refresh:{team}',
        ).start()
    except Exception:
        # Thread spawn failed: release the mutex so a later request can retry,
        # and let the caller fail open to a synchronous build.
        try:
            cache.delete(lock_key)
        except Exception:
            pass
        return False
    return True


def _refresh_lane_snapshot(team: str, *, org_slug: str, lock_key: str) -> None:
    try:
        close_old_connections()
        _build_and_store_lane_snapshot(team, org_slug=org_slug)
    except Exception:
        pass  # stale snapshot stays served; the next expired read retries
    finally:
        try:
            close_old_connections()
        except Exception:
            pass
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def _invalidate_lane_snapshot(team: str) -> None:
    """Best-effort drop of a lane's FRESH snapshot key (stale keeps serving
    while the next read rebuilds)."""
    try:
        cache.delete(_LANE_SNAPSHOT_FRESH_KEY.format(team=team))
    except Exception:
        pass


def _invalidate_all_lane_snapshots() -> None:
    """Best-effort drop of every active lane's FRESH snapshot key."""
    try:
        teams = set(
            TeamRoster.objects.filter(is_active=True)
            .values_list('team_name', flat=True)
        )
    except Exception:
        return
    # Snapshot keys are keyed by CANONICALIZED team name (lane_health
    # canonicalizes before formatting); the roster rows store raw labels that
    # may be aliases ('admin_ops' -> 'admin-ops'), so canonicalize here too or
    # the delete misses the key.
    for team in teams:
        try:
            _invalidate_lane_snapshot(_canonical_team(team))
        except Exception:
            continue


def _lane_health_uncached(team_name: str, *, org_slug: str = 'shizuha') -> dict:
    """One per-lane health snapshot, built live (no snapshot cache)."""
    team = _canonical_team(team_name)
    roster = None
    try:
        roster = (
            TeamRoster.objects.filter(
                team_name=team,
                organization_slug=org_slug,
            )
            .order_by('-version')
            .first()
        )
    except Exception:
        roster = None

    members = _flat_roster(team)
    probe = _probe_item(team)
    # PLS-1187: one fleet-wide digest read per lane (was one per seat).
    runtime_digest = _fleet_runtime_digest()
    seats = [
        _seat_verdict(email, team, probe, runtime=runtime_digest)
        for email in members
    ]
    pending = _pending_sod_demand(team)
    _annotate_sod(seats, team, pending)
    routable = sum(
        1 for s in seats
        if s['live'] and not (s.get('capacity') or {}).get('over_capacity')
    )
    effective = sum(
        1 for s in seats
        if s['live']
        and not (s.get('capacity') or {}).get('over_capacity')
        and not s.get('sod_disqualified')
    )

    snapshot = {
        'team': team,
        'organization': org_slug,
        'roster': {
            'version': roster.version if roster else None,
            'active': bool(roster and roster.is_active),
            'wip_enforced': bool(roster and roster.wip_enforced),
            'ready_wip_cap': roster.ready_wip_cap if roster else None,
            'active_wip_cap': roster.active_wip_cap if roster else None,
            'member_count': len(members),
        },
        'seats': seats,
        'resolver': {
            'staffed': bool(members),
            'routable_seats': routable,
            'effective_routable': effective,
            'seat_count': len(members),
            'unavailable_seats': len(members) - routable,
        },
        'queue_depth': _queue_depth(team),
        'last_assignment': _last_assignment(team),
        'merge_sod': _merge_lane_sod(team),
        'generated_at': timezone.now().isoformat(),
    }
    snapshot['stall'] = classify_stall(snapshot)
    snapshot['stall']['hold_override'] = hold_override_events(snapshot)
    snapshot['stall']['assigned_not_pulled'] = assigned_not_pulled(snapshot)

    # Remember the healthy roster baseline for authority-drift detection.
    if not snapshot['stall']['is_stalled']:
        _remember_healthy_roster(team, snapshot['roster']['version'])

    return snapshot


def _all_lanes_serial(rows, *, org_slug: str) -> list[dict]:
    lanes = []
    for row in rows:
        try:
            lanes.append(lane_health(row.team_name, org_slug=org_slug))
        except Exception:
            continue
    return lanes


def all_lanes_health(*, org_slug: str = 'shizuha') -> list[dict]:
    """Health snapshots for every active TeamRoster lane in an organization.

    Warm and stale-while-revalidate reads batch the per-lane cache keys with
    ``cache.get_many`` (two round-trips, not one ``cache.get`` per lane). A
    serial Redis RTT across ~25 lanes was the remaining 5–7s tail after the
    snapshot cache landed (PLAT-8386). Cold lanes still spawn a background
    refresh and return a building placeholder; a degraded cache falls open to
    the serial ``lane_health`` path.
    """
    try:
        rows = list(
            TeamRoster.objects.filter(
                organization_slug=org_slug,
                is_active=True,
            ).order_by('team_name')
        )
    except Exception:
        return []
    if not rows or not _snapshot_cache_enabled():
        return _all_lanes_serial(rows, org_slug=org_slug)

    teams = []
    for row in rows:
        try:
            teams.append(_canonical_team(row.team_name))
        except Exception:
            continue
    fresh_keys = [_LANE_SNAPSHOT_FRESH_KEY.format(team=team) for team in teams]
    try:
        fresh = cache.get_many(fresh_keys)
    except Exception:
        return _all_lanes_serial(rows, org_slug=org_slug)

    missing = [
        team for team, key in zip(teams, fresh_keys)
        if fresh.get(key) is None
    ]
    stale = {}
    if missing:
        stale_keys = [_LANE_SNAPSHOT_STALE_KEY.format(team=team) for team in missing]
        try:
            stale = cache.get_many(stale_keys)
        except Exception:
            stale = {}

    lanes = []
    for team in teams:
        hit = fresh.get(_LANE_SNAPSHOT_FRESH_KEY.format(team=team))
        if hit is not None:
            lanes.append(hit)
            continue
        stale_hit = stale.get(_LANE_SNAPSHOT_STALE_KEY.format(team=team))
        if stale_hit is not None:
            _spawn_lane_snapshot_refresh(team, org_slug=org_slug)
            lanes.append(stale_hit)
            continue
        if _spawn_lane_snapshot_refresh(team, org_slug=org_slug):
            lanes.append(_building_placeholder(team, org_slug=org_slug))
            continue
        try:
            lanes.append(_build_and_store_lane_snapshot(team, org_slug=org_slug))
        except Exception:
            continue
    return lanes

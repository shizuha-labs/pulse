"""Capability-class lane detector (PLAT-8980, ADR-0086 v4).

Prevention for PLAT-8960 (single-point capability seats absorb carriers when
dark): fire — **alert, not page** — when a critical capability lane scores
exactly **1** live-capable seat, debounced 15m, with the provisioning ask
bundled (PLAT-8263 one-window pointer). `count == 0` with genuinely queued
demand belongs to the existing zero-live family (ordered Team Outage
checklist) — this detector deliberately does NOT own that condition.

Signal (ADR-0086 "Detector implementation spec (CoS leg)"): per critical lane,
count seats that are BOTH (a) **live** (resolver-level seat-live scoring,
disposition `a1225ba3`) AND (b) **capability-qualified** for the lane — the
same typed scoring the assignee write-guard (PLS-454) consumes via
`WorkflowService._seat_routability_verdict`. Risk-label/trigger-name heuristics
are never used; the predicate is demand-independent (a lane at 1 with zero
queued demand still degrades — topology fact, not demand-dependent).

Episode identity: `(organization_id, canonical_team_slug, capability_class)`
— carried by the `org_health_signal` metadata key
`single-capability-seat:<organization_id>:<lane>`, deduped idempotently by the
sweep's existing carrier machinery. Deterministic removal: the signal clears
when `count >= 2` holds for `PULSE_CAP_LANE_CLEAR_SECONDS` (default 1800).
"""
from __future__ import annotations

import logging
import os
import time

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Rollout order (load-bearing): merge + JIT-approval lanes first — the two
# that bit three times the week of 2026-09-15 (jun write, aoi merge, JIT sole
# approver). Repo-write rides the same substrate sweep later.
DEFAULT_CRITICAL_CAPABILITY_LANES = ('merge', 'jit-approval')


def critical_capability_lanes():
    raw = os.environ.get('PULSE_CAPABILITY_LANES', '')
    lanes = [s.strip().lower() for s in raw.split(',') if s.strip()]
    return tuple(lanes) or DEFAULT_CRITICAL_CAPABILITY_LANES


def single_seat_debounce_seconds() -> int:
    """ADR-0086: fire at count == 1 only after a 15m debounced hold."""
    return int(os.environ.get('PULSE_CAP_LANE_DEBOUNCE_SECONDS', '900'))


def clear_hold_seconds() -> int:
    """Deterministic removal: count >= 2 must hold this long to clear."""
    return int(os.environ.get('PULSE_CAP_LANE_CLEAR_SECONDS', '1800'))


# Reasons that disqualify a seat from counting as live-capable. Typed verdict
# vocabulary from `WorkflowService._seat_routability_verdict` (PLS-278) — the
# same object the PLS-454 assignee write-guard enforces on. SoD-disqualified
# and phantom-identity seats always surface here as a disqualifying reason
# (incapability / non-membership / unknown team), so they never count.
_DISQUALIFYING_REASONS = frozenset({
    'seat_not_live',
    'seat_not_team_member',
    'team_unstaffed',
    'team_unknown',
    'org_roster_mismatch',
    'seat_incapable',
})

_DEBOUNCE_KEY = 'pulse:cap_lane:single_seat_first_seen:{org}:{lane}'
_CLEAR_KEY = 'pulse:cap_lane:clear_first_seen:{org}:{lane}'


def _env_int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default


def live_capable_seat_count(W, team_name, *, organization_id=1, probe_item=None):
    """Count roster seats that are BOTH live AND capability-qualified.

    Returns `(count, detail)` where detail maps email -> disqualifying reasons
    ([] for counted seats) — the alert body's evidence rows.
    """
    team = W._canonical_team_name(team_name)
    try:
        roster_entry, _org = W._team_roster_entry_for_item(team, probe_item)
    except Exception:
        roster_entry, _org = None, None
    if roster_entry is None:
        try:
            assignable = W.assignable_teams(include_active_task_groups=False)
            roster_entry = assignable.get(team)
        except Exception:
            roster_entry = None
    if not roster_entry:
        return 0, {'__lane__': ['team_unstaffed']}
    emails = [
        str(e or '').strip().lower()
        for e in W._flat_roster(roster_entry)
        if str(e or '').strip()
    ]
    if not emails:
        return 0, {'__lane__': ['team_unstaffed']}

    detail = {}
    count = 0
    for email in emails:
        try:
            verdict = W._seat_routability_verdict(
                probe_item, team, email,
                intent='new_assignment',
                enforce_cap=False,
            )
            reasons = [str(r) for r in (getattr(verdict, 'reasons', None) or [])]
        except Exception as e:
            logger.warning(
                'capability_lane_health: verdict failed for %s/%s: %r',
                team, email, e)
            reasons = ['seat_not_live']
        bad = [r for r in reasons if r in _DISQUALIFYING_REASONS]
        detail[email] = bad
        if not bad:
            count += 1
    return count, detail


def _cache_get_int(key):
    try:
        val = cache.get(key)
    except Exception:
        return None
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_single_seat_episode(lane, count, *, now_ts=None):
    """Debounce/dedup state machine for one `(org, lane)` episode.

    Returns `'fire'`, `'hold'`, or `'clear'`:
    - `count == 1`: record first-seen; `'fire'` once the hold has persisted
      `single_seat_debounce_seconds()` (subsequent evaluations keep returning
      `'fire'` only until a clear — the sweep's metadata dedup makes repeat
      fires idempotent).
    - `count >= 2`: record clear-first-seen; `'clear'` once the clear has held
      `clear_hold_seconds()` (deterministic removal), else `'hold'`.
    - `count == 0`: NOT owned here (zero-live family) — treated as `'hold'`
      with the debounce window preserved so a flapping seat doesn't re-arm
      instantly.
    """
    now_ts = float(now_ts if now_ts is not None else time.time())
    org = '1'  # single-org deployment reality; sweep passes scoped keys
    debounce_key = _DEBOUNCE_KEY.format(org=org, lane=lane)
    clear_key = _CLEAR_KEY.format(org=org, lane=lane)
    if count == 1:
        cache.delete(clear_key)
        first = _cache_get_int(debounce_key)
        if first is None:
            cache.set(debounce_key, now_ts, timeout=3600)
            return 'hold'
        if now_ts - first >= single_seat_debounce_seconds():
            return 'fire'
        return 'hold'
    if count >= 2:
        cache.delete(debounce_key)
        first_clear = _cache_get_int(clear_key)
        if first_clear is None:
            cache.set(clear_key, now_ts, timeout=7200)
            return 'hold'
        if now_ts - first_clear >= clear_hold_seconds():
            cache.delete(clear_key)
            return 'clear'
        return 'hold'
    # count == 0 — zero-live family owns this; keep windows as-is.
    return 'hold'


def prometheus_lines(counts):
    """Expose `pulse_capability_lane_live_capable_seats{lane}` gauges."""
    lines = []
    for lane, count in sorted(counts.items()):
        lines.append(
            f'pulse_capability_lane_live_capable_seats{{lane="{lane}"}} {int(count)}'
        )
    return lines

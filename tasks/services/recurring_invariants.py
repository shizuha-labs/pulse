"""PLAT-1255: recurring-engine invariant metrics for Prometheus.

These checks are deliberately read-only. They expose invariant drift so the
existing Alertmanager/critical-alert-notifier path can DM the on-call lead; they
must not file Pulse tasks or mutate recurring items.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

import redis
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from tasks.scheduler import KEY_RECURRING, RECURRING_STOP_STATUSES
from tasks.services.recurring import parse_interval_seconds
# The fire handler (fire_due_cycle) treats these as permanent stops and will
# never re-fire them; an item in this set cannot keep a recurring timer armed
# (it pops, is stopped, and is not re-armed). Aligning the invariant's "active"
# set with the fire handler's stop set prevents two defects: (1) the
# missing_timer metric counting intentionally-stopped items (rejected /
# duplicate / wont_fix / failed) as "missing timer", and (2) the 60s sweep
# re-arming them into a pop→stop→re-arm loop (PLAT-6289 / PLS-980).
from tasks.services.recurring_cycle_state import STOP_STATUSES as FIRE_STOP_STATUSES

LEADER_NAMES = ("beat", "scheduler")


@dataclass(frozen=True)
class OverdueRecurringItemRef:
    """PLS-1345: per-item identity behind one ``pulse_recurring_overdue_item`` sample.

    The scalar ``pulse_recurring_overdue`` names no culprits, and the
    permission-scoped items API hides cross-org/terminal rows by design, so a
    firing alert could not be actioned from any seat. The labeled series
    carries the identity the runbook documents (item_key, organization_id,
    assignment_group). ``organization_id``/``assignment_group`` are ``""`` when
    the row has no project/org or no group routing.
    """

    item_key: str
    organization_id: str
    assignment_group: str


@dataclass(frozen=True)
class RecurringInvariantMetrics:
    overdue: int
    unschedulable: int
    missing_timer: int
    group_queued_missing_timer: int
    assignment_andon: int
    leader_counts: dict[str, int]
    # PLAT-9452: 1 when any Redis read in this collection raised (the
    # invariant's Redis view is BLIND for this sample). The missing-timer
    # gauges then report 0 — what the sample actually verified — instead of
    # the old fail-closed len(items), which masqueraded a Redis blip as
    # "every timer missing" through the same gauge the
    # PulseRecurringGroupQueueMissingTimer alert fires on (observed 09-20:
    # per-replica counts oscillating 5→4→1→5 with max == total population).
    # Blindness is carried by pulse_recurring_redis_error so it alerts to the
    # Redis/infra owner, not the recurring engine.
    redis_error: int
    # Epoch seconds of the last successful sweep_missed_notifications run, as
    # recorded by the sweep itself in Redis (PLAT-9452 defect 3). ``None`` means
    # the liveness key could not be read (Redis probe failure) — the renderer
    # omits the family rather than publishing a synthetic zero. A missing key
    # renders as 0.0 (the sweep has never completed — maximally stale).
    sweep_last_success_timestamp: float | None = None
    # PLS-1345: identity of each overdue item, rendered as the labeled
    # ``pulse_recurring_overdue_item`` series (one sample per entry, value 1).
    # Empty tuple when nothing is overdue — the scalar carries the count.
    overdue_items: tuple[OverdueRecurringItemRef, ...] = ()


# PLAT-9452 defect 3: the sweep stamps this key on every successful run so the
# "beat firing but healer no-op" state is distinguishable from "beat dead".
SWEEP_LIVENESS_KEY = "pulse:sweep:last_success_ts"


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, double quote, newline)."""
    return (
        str(value)
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
    )


def _redis_client(redis_client=None):
    if redis_client is not None:
        return redis_client
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)


def _active_recurring_items():
    from tasks.models import Item

    return (
        Item.objects.filter(mode="task", is_recurring=True)
        .select_related('project')
        .filter(
            Q(assignee_id__isnull=False)
            | (Q(assignment_group__isnull=False) & ~Q(assignment_group=""))
            | (
                Q(metadata__recurring_owner_team__isnull=False)
                & ~Q(metadata__recurring_owner_team="")
            )
        )
        .exclude(frequency__in=("", "one_time"))
        .exclude(status__in=RECURRING_STOP_STATUSES | FIRE_STOP_STATUSES)
    )


def _is_unschedulable(item) -> bool:
    interval = parse_interval_seconds(item.frequency, item.recurrence_pattern)
    return interval is None and item.frequency not in ("monthly", "yearly")


def _is_overdue(item, now) -> bool:
    if not item.due_date:
        return False
    interval = parse_interval_seconds(item.frequency, item.recurrence_pattern)
    if interval is None:
        # Calendar recurrences are valid but do not have a fixed seconds period;
        # the 1.25*period invariant applies only to fixed-interval schedules.
        return False
    threshold = item.due_date + timedelta(seconds=interval * 1.25)
    return now > threshold


def _missing_timer_count(items: Iterable, redis_client) -> tuple[int, bool]:
    """Return (confirmed_missing, redis_blind).

    On a RedisError the sample cannot verify ANY timer, so it reports
    0 confirmed-missing and sets the blind flag — the caller surfaces
    blindness via ``pulse_recurring_redis_error`` instead of folding it into
    the missing-timer gauges (PLAT-9452; the old fail-closed ``len(items)``
    made a Redis blip indistinguishable from total timer loss).
    """
    missing = 0
    for item in items:
        try:
            if redis_client.zscore(KEY_RECURRING, str(item.pk)) is None:
                missing += 1
        except redis.RedisError:
            return 0, True
    return missing, False


def _is_routing_held(item) -> bool:
    """PLS-528 routing hold (PLAT-9452): the occurrence is intentionally
    un-routed (``no_live_member`` et al.) and the DB-driven due sweep covers
    it with ``delivery_retry_at`` — a missing Redis timer is the designed
    state for a held occurrence, not invariant drift. The missing-timer
    signal must exclude held items or the gauge pages permanently on the
    hold population (live 09-20: detector 4, healer population diverged).

    Marker shape (recurring_cycle_state): metadata['recurring_routing']
    ['discrepancy'] = {active: True, outcome: 'hold', reason: ...}.
    """
    metadata = getattr(item, 'metadata', None)
    if not isinstance(metadata, dict):
        return False
    routing = metadata.get('recurring_routing')
    if not isinstance(routing, dict):
        return False
    discrepancy = routing.get('discrepancy')
    return (
        isinstance(discrepancy, dict)
        and bool(discrepancy.get('active'))
        and discrepancy.get('outcome') == 'hold'
    )


def collect_recurring_invariant_metrics(redis_client=None, now=None) -> RecurringInvariantMetrics:
    """Collect recurring-engine self-check metrics, including team queues."""
    now = now or timezone.now()
    r = _redis_client(redis_client)
    items = list(_active_recurring_items())

    unschedulable = sum(1 for item in items if _is_unschedulable(item))
    schedulable_items = [item for item in items if not _is_unschedulable(item)]
    overdue_items = [
        item for item in schedulable_items if _is_overdue(item, now)
    ]
    overdue = len(overdue_items)
    # PLS-1345: keep the identity of each overdue row so the exposition can
    # name culprits (the scalar and the permission-scoped API both cannot).
    overdue_refs = tuple(
        OverdueRecurringItemRef(
            item_key=str(item.item_key or item.pk),
            # Org rides the project FK (nullable) — "" when unassigned.
            organization_id=(
                str(item.project.org_id)
                if item.project is not None and item.project.org_id is not None
                else ""
            ),
            assignment_group=item.assignment_group or "",
        )
        for item in overdue_items
    )
    # PLAT-9452: the missing-timer signal covers only genuinely-routed
    # occurrences. Routing-held items (PLS-528) are intentionally un-armed —
    # the DB sweep owns their fire cadence — so a missing Redis timer for a
    # held item is the designed state and must not page.
    routed_items = [item for item in schedulable_items if not _is_routing_held(item)]
    missing_timer, redis_blind = _missing_timer_count(routed_items, r)
    group_queued = [item for item in routed_items if not item.assignee_id]
    group_queued_missing_timer, group_redis_blind = _missing_timer_count(group_queued, r)
    redis_error = 1 if (redis_blind or group_redis_blind) else 0
    assignment_andon = sum(
        1 for item in items
        if isinstance(item.metadata, dict)
        and isinstance(item.metadata.get('recurring_cycle'), dict)
        and item.metadata['recurring_cycle'].get('assignment_andon_at')
    )

    leader_counts = {}
    for name in LEADER_NAMES:
        try:
            leader_counts[name] = 1 if r.get(f"pulse:leader:{name}") else 0
        except redis.RedisError:
            leader_counts[name] = 0
            redis_error = 1

    # PLAT-9452 defect 3: sweep-liveness. Key absent → the sweep has never
    # completed → render 0.0 (maximally stale, truth-bearing). Redis read
    # failure → None → the renderer omits the family (never synthetic zero).
    try:
        raw = r.get(SWEEP_LIVENESS_KEY)
        sweep_last_success_timestamp = float(raw) if raw else 0.0
    except redis.RedisError:
        sweep_last_success_timestamp = None

    return RecurringInvariantMetrics(
        overdue=overdue,
        overdue_items=overdue_refs,
        unschedulable=unschedulable,
        missing_timer=missing_timer,
        group_queued_missing_timer=group_queued_missing_timer,
        assignment_andon=assignment_andon,
        leader_counts=leader_counts,
        redis_error=redis_error,
        sweep_last_success_timestamp=sweep_last_success_timestamp,
    )


def render_prometheus_metrics(metrics: RecurringInvariantMetrics) -> str:
    """Render Prometheus text exposition without adding a runtime dependency."""
    lines = [
        "# HELP pulse_recurring_overdue Active recurring items overdue by >1.25x their fixed period.",
        "# TYPE pulse_recurring_overdue gauge",
        f"pulse_recurring_overdue {metrics.overdue}",
    ]
    # PLS-1345: labeled per-item series so an alert firing can name its
    # culprits from any seat (the scalar cannot, and the permission-scoped
    # items API hides cross-org rows by design). One sample per overdue item;
    # the family is header-only when nothing is overdue (the scalar is the
    # count of record). The runbook cites exactly these label names.
    lines.append(
        "# HELP pulse_recurring_overdue_item Per-item identity of each overdue recurring item (value is always 1); one sample per item_key."
    )
    lines.append("# TYPE pulse_recurring_overdue_item gauge")
    for ref in metrics.overdue_items:
        lines.append(
            f'pulse_recurring_overdue_item{{item_key="{_escape_label(ref.item_key)}"'
            f',organization_id="{_escape_label(ref.organization_id)}"'
            f',assignment_group="{_escape_label(ref.assignment_group)}"}} 1'
        )
    lines.extend([
        "# HELP pulse_recurring_unschedulable Recurring items whose frequency/pattern cannot be interpreted.",
        "# TYPE pulse_recurring_unschedulable gauge",
        f"pulse_recurring_unschedulable {metrics.unschedulable}",
        "# HELP pulse_recurring_missing_timer Active routed recurring items absent from Redis pulse:timers:recurring.",
        "# TYPE pulse_recurring_missing_timer gauge",
        f"pulse_recurring_missing_timer {metrics.missing_timer}",
        "# HELP pulse_recurring_group_queued_missing_timer Team-owned unassigned recurring items absent from Redis pulse:timers:recurring.",
        "# TYPE pulse_recurring_group_queued_missing_timer gauge",
        f"pulse_recurring_group_queued_missing_timer {metrics.group_queued_missing_timer}",
        "# HELP pulse_recurring_assignment_andon Team-owned recurring items whose bounded assignment retries exhausted.",
        "# TYPE pulse_recurring_assignment_andon gauge",
        f"pulse_recurring_assignment_andon {metrics.assignment_andon}",
        "# HELP pulse_recurring_redis_error 1 when this sample's Redis reads failed (invariant view blind); missing-timer gauges are 0 in that state, not fail-closed totals.",
        "# TYPE pulse_recurring_redis_error gauge",
        f"pulse_recurring_redis_error {metrics.redis_error}",
        "# HELP pulse_leader_count Pulse singleton leader lease presence by process name; expected value is 1.",
        "# TYPE pulse_leader_count gauge",
    ])
    for name in sorted(metrics.leader_counts):
        lines.append(f'pulse_leader_count{{name="{name}"}} {metrics.leader_counts[name]}')
    # PLAT-9452 defect 3: sweep-liveness gauge. Omitted entirely on a Redis
    # probe failure (None) — an absent family is "unknown", never synthetic
    # zero; the staleness alert treats a missing series as fail-loud via its
    # own absent() handling.
    if metrics.sweep_last_success_timestamp is not None:
        lines.append(
            "# HELP pulse_sweep_last_success_timestamp_seconds Epoch seconds of the last successful sweep_missed_notifications run."
        )
        lines.append("# TYPE pulse_sweep_last_success_timestamp_seconds gauge")
        lines.append(
            f"pulse_sweep_last_success_timestamp_seconds {metrics.sweep_last_success_timestamp}"
        )
    return "\n".join(lines) + "\n"

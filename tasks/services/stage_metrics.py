"""PLS-13: stage-transition dwell metrics.

True stage dwell (time spent IN each workflow status) derived from the existing
`ItemActivity` log — NOT `updated_at` proxies, and with no schema change. Every
status transition is already recorded as an ItemActivity row whose `field_name`
is the status field's tracked label (`TRACKED_FIELDS['status']`) — this captures
ALL status changes regardless of the action label (`status_changed` / `completed`
/ `reopened` / any `_activity_status_action` override). `old_value`/`new_value`
are the rendered status values and pair consistently into intervals.

Read-only: this module derives + aggregates; it never writes and does not touch
the transition path.
"""
from __future__ import annotations

from datetime import timedelta
from django.utils import timezone

from ..models import Item, ItemActivity
from ..signals import TRACKED_FIELDS
from .actionable_items import TERMINAL_STATUS_SLUGS

STATUS_FIELD_LABEL = TRACKED_FIELDS.get('status', 'Status')

# Statuses that are intentional parking / calendar holds, not flow bottlenecks.
# "Slowest stages" on org-progress must not rank "scheduled" (recurring annual
# filings, monthly meetings) or "blocked" (waiting on upstream) as if they were
# stuck in-progress work — dwell there is expected until the next due event or
# unblock. Mirrors WAITING_QUEUE_STATUS_SLUGS + backlog parking.
PARKING_STATUS_SLUGS = frozenset({
    'scheduled',
    'blocked',
    'waiting_external',
    'backlog',
    'deferred',  # terminal-ish parking if still present in history
})


def is_parking_status(status) -> bool:
    """True when dwell in this status is not a flow-stage bottleneck signal."""
    return _norm_status(status) in PARKING_STATUS_SLUGS


def derive_intervals(current_status, created_at, transitions, now=None):
    """Pure: turn an ordered transition list into per-status intervals.

    transitions: list of {'old': str, 'new': str, 'at': datetime} sorted by `at`
    ascending. Returns a list of
        {status, entered_at, exited_at|None, duration_seconds, open: bool}
    The first interval starts at `created_at` in the item's initial status
    (the first transition's `old`, or `current_status` if there were none); the
    last interval is OPEN (exited_at=None) and measured to `now`.
    """
    now = now or timezone.now()
    intervals = []
    status = transitions[0]['old'] if transitions else current_status
    start = created_at
    for t in transitions:
        intervals.append({
            'status': status,
            'entered_at': start,
            'exited_at': t['at'],
            'duration_seconds': max(0.0, (t['at'] - start).total_seconds()),
            'open': False,
        })
        status = t['new']
        start = t['at']
    intervals.append({
        'status': status,
        'entered_at': start,
        'exited_at': None,
        'duration_seconds': max(0.0, (now - start).total_seconds()),
        'open': True,
    })
    return intervals


def _status_transitions(item):
    """Ordered status transitions for an item from the ItemActivity log."""
    rows = (ItemActivity.objects
            .filter(item=item, field_name=STATUS_FIELD_LABEL)
            .order_by('created_at', 'pk')
            .values('old_value', 'new_value', 'created_at'))
    return [{'old': r['old_value'], 'new': r['new_value'], 'at': r['created_at']} for r in rows]


def item_stage_history(item, now=None):
    """Per-item stage history: the ordered list of status intervals + the team."""
    transitions = _status_transitions(item)
    intervals = derive_intervals(
        current_status=getattr(item, 'status', None),
        created_at=item.created_at,
        transitions=transitions,
        now=now,
    )
    return {
        'item_key': getattr(item, 'item_key', None) or str(item.pk),
        'team': item.assignment_group or '',
        'current_status': getattr(item, 'status', None),
        'intervals': intervals,
    }


def _percentile(values, pct):
    """Nearest-rank percentile (pct in 0..100) of a list of numbers."""
    if not values:
        return None
    s = sorted(values)
    import math
    k = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[k - 1]


def team_throughput(days=14, project_key=None, now=None, items=None,
                    include_parking=False):
    """Aggregate per (team, status): sample count, avg/p90 dwell (seconds) over
    CLOSED intervals that exited within the window, plus throughput
    (transitions-out per day). Window = the last `days` days.

    ``items`` optionally supplies a pre-scoped Item queryset (e.g. an
    org-scoped one — PLAT-1197 review: the company-state snapshot must not
    aggregate stage dwell across orgs). Defaults to all items.

    By default, **parking statuses** (scheduled, blocked, backlog, …) are
    omitted: long dwell there is expected (calendar holds, upstream waits),
    not a "slowest stage" bottleneck. Pass ``include_parking=True`` for
    diagnostics that intentionally want those rows.
    """
    now = now or timezone.now()
    window_start = now - timedelta(days=max(1, days))

    items = Item.objects.all() if items is None else items
    # Flow/stage-dwell metrics represent work-item movement. Alert-mode rows are
    # an incident ledger fed by Alertmanager and can remain active until external
    # resolution events arrive; including them pollutes team-throughput/stuck-stage
    # signals with monitoring state rather than actionable work (PLS-219).
    items = items.exclude(mode='alert')
    if project_key:
        items = items.filter(project__key=str(project_key).upper())
    # Only items that saw a status transition in the window need scanning, but
    # for correctness (open intervals + small datasets) we scan items touched
    # since the window start.
    items = items.filter(updated_at__gte=window_start) if hasattr(Item, 'updated_at') else items

    # (team, status) -> list of closed-interval durations that EXITED in-window
    buckets: dict[tuple[str, str], list[float]] = {}
    # (team, status) -> count of transitions OUT (interval exits) in-window
    exits: dict[tuple[str, str], int] = {}

    # Read only in-window exits plus one carry-in transition per item. A
    # correlated predecessor lookup per exit still rescans/sorts old histories
    # thousands of times on busy organizations, even though it is one SQL call.
    # PostgreSQL DISTINCT ON bounds the carry-in result, never hydrating history.
    rows = list(ItemActivity.objects.filter(
        item__in=items.values('pk'), field_name=STATUS_FIELD_LABEL,
        created_at__gte=window_start,
    ).order_by('item_id', 'created_at', 'pk').values(
        'item_id', 'item__assignment_group', 'item__created_at',
        'old_value', 'new_value', 'created_at',
    ))
    item_ids = {row['item_id'] for row in rows}
    previous = {
        row['item_id']: (row['new_value'], row['created_at'])
        for row in ItemActivity.objects.filter(
            item_id__in=item_ids, field_name=STATUS_FIELD_LABEL,
            created_at__lt=window_start,
        ).order_by('item_id', '-created_at', '-pk').distinct('item_id').values(
            'item_id', 'new_value', 'created_at',
        )
    }
    for row in rows:
        status, entered_at = previous.get(
            row['item_id'], (row['old_value'], row['item__created_at']))
        previous[row['item_id']] = (row['new_value'], row['created_at'])
        if not include_parking and is_parking_status(status):
            continue
        duration = max(0.0, (row['created_at'] - entered_at).total_seconds())
        key = (row['item__assignment_group'] or '', status)
        buckets.setdefault(key, []).append(duration)
        exits[key] = exits.get(key, 0) + 1

    out = []
    day_span = max(1.0, (now - window_start).total_seconds() / 86400.0)
    for (team, status), durations in sorted(buckets.items()):
        avg = sum(durations) / len(durations) if durations else None
        out.append({
            'team': team,
            'status': status,
            'samples': len(durations),
            'avg_dwell_seconds': round(avg, 1) if avg is not None else None,
            'p90_dwell_seconds': round(_percentile(durations, 90), 1),
            'throughput_out_per_day': round(exits.get((team, status), 0) / day_span, 2),
            'window_days': days,
            'is_parking': is_parking_status(status),
        })
    return out


def current_stage_dwell(items=None, now=None, include_parking=False):
    """Per (team, status) dwell of items currently SITTING in that status.

    ``team_throughput`` only counts CLOSED intervals that already exited —
    useful for cycle-time, but the home "Slowest stages" tile then ranks a
    single 20-day ``accepted`` that already left as if it were a live
    bottleneck, and the 24h/3d/7d range toggle does not apply to it.

    This helper uses the OPEN tail of each item's stage history (now − entered).
    Parking statuses are omitted by default, matching ``team_throughput``.
    """
    now = now or timezone.now()
    items = Item.objects.all() if items is None else items
    items = items.exclude(mode='alert').exclude(status__in=TERMINAL_STATUS_SLUGS)

    buckets: dict[tuple[str, str], list[float]] = {}
    rows = list(items.order_by().values('pk', 'assignment_group', 'status', 'created_at'))
    latest = {
        row['item_id']: (row['new_value'], row['created_at'])
        for row in ItemActivity.objects.filter(
            item_id__in=[row['pk'] for row in rows], field_name=STATUS_FIELD_LABEL,
        ).order_by('item_id', '-created_at', '-pk').distinct('item_id').values(
            'item_id', 'new_value', 'created_at',
        )
    }
    # Open dwell needs only the latest entry per currently active item.
    for row in rows:
        status, entered_at = latest.get(row['pk'], (row['status'], row['created_at']))
        if not include_parking and is_parking_status(status):
            continue
        key = (row['assignment_group'] or '', status)
        buckets.setdefault(key, []).append(max(0.0, (now - entered_at).total_seconds()))

    out = []
    for (team, status), durations in sorted(buckets.items()):
        avg = sum(durations) / len(durations)
        out.append({
            'team': team,
            'status': status,
            'samples': len(durations),
            'avg_dwell_seconds': round(avg, 1),
            'p90_dwell_seconds': round(_percentile(durations, 90), 1),
            'max_dwell_seconds': round(max(durations), 1),
            'is_parking': is_parking_status(status),
            'kind': 'open',
        })
    return out


# Terminal statuses that represent PRODUCTIVE completion (a subset of the full
# terminal set below) — used to separate "we shipped it" from "we dropped/parked
# it" (cancelled/rejected/deferred) on the org-progress resolution-rate chart.
_COMPLETED_TERMINAL = {'completed', 'done', 'resolved', 'closed', 'merged', 'applied'}


def _norm_status(value):
    """Normalize a rendered status value to its slug form for terminal-set
    membership (mirrors cumulative_flow's `.strip().lower()`, plus space→_ so
    multi-word rendered labels like 'In Review' match the underscore slugs)."""
    return str(value or '').strip().lower().replace(' ', '_')


def progress_timeseries(hours=24, buckets=24, project_key=None, now=None, items=None):
    """Time-bucketed task flow for the org-progress trend charts.

    Splits the last ``hours`` hours into ``buckets`` equal buckets and, per
    bucket, counts:
      - ``created``:   items whose ``created_at`` falls in the bucket (intake).
      - ``completed``: status transitions INTO a productive-terminal status
                       (done/completed/resolved/closed/merged/applied).
      - ``terminal``:  transitions INTO ANY terminal status, including
                       cancelled/rejected/deferred (the full resolution rate).

    ``completed`` <= ``terminal`` always. Read-only over ``Item`` +
    ``ItemActivity`` (the same transition log as ``team_throughput`` /
    ``cumulative_flow``). Same scoping rules: alert-mode rows excluded, optional
    project filter, and an optional pre-scoped ``items`` queryset — pass the
    caller-scoped ``ItemViewSet.get_queryset()`` for correct per-org isolation.
    """
    now = now or timezone.now()
    hours = max(1, min(int(hours), 24 * 30))        # 1h .. 30d
    buckets = max(1, min(int(buckets), 500))
    window_start = now - timedelta(hours=hours)
    width = (now - window_start).total_seconds() / buckets

    items = Item.objects.all() if items is None else items
    items = items.exclude(mode='alert')
    if project_key:
        items = items.filter(project__key=str(project_key).upper())

    def bucket_index(ts):
        i = int((ts - window_start).total_seconds() // width)
        return min(max(i, 0), buckets - 1)

    created = [0] * buckets
    completed = [0] * buckets
    terminal = [0] * buckets

    for ts in (items.filter(created_at__gte=window_start, created_at__lte=now)
               .values_list('created_at', flat=True)):
        created[bucket_index(ts)] += 1

    rows = (ItemActivity.objects
            .filter(item__in=items, field_name=STATUS_FIELD_LABEL,
                    created_at__gte=window_start, created_at__lte=now)
            .values_list('new_value', 'created_at'))
    for new_value, ts in rows.iterator():
        slug = _norm_status(new_value)
        if slug in _CFD_TERMINAL:
            bi = bucket_index(ts)
            terminal[bi] += 1
            if slug in _COMPLETED_TERMINAL:
                completed[bi] += 1

    points = [{
        'ts': (window_start + timedelta(seconds=width * (i + 1))).isoformat(),
        'created': created[i],
        'completed': completed[i],
        'terminal': terminal[i],
    } for i in range(buckets)]

    return {
        'hours': hours,
        'buckets': buckets,
        'bucket_minutes': round(width / 60.0, 2),
        'points': points,
        'totals': {
            'created': sum(created),
            'completed': sum(completed),
            'terminal': sum(terminal),
        },
    }


def derive_cycle(created_at, completed_at, transitions):
    """PLS-362, pure: (cycle_seconds, lead_seconds) for one completed item.

    lead  = created -> completed (whole life).
    cycle = first movement out of the initial status -> completed (work time);
            an item completed with no recorded transitions falls back to lead.
    """
    lead = max(0.0, (completed_at - created_at).total_seconds())
    started_at = transitions[0]['at'] if transitions else created_at
    if started_at > completed_at:  # clock skew / backdated completion guard
        started_at = completed_at
    cycle = max(0.0, (completed_at - started_at).total_seconds())
    return cycle, lead


def cycle_times(days=30, project_key=None, now=None, items=None):
    """PLS-362: per-item cycle/lead time for items COMPLETED in the window,
    plus a percentile summary. Read-only; same scoping rules as
    team_throughput (alert-mode rows excluded, optional project filter,
    optional pre-scoped queryset for org scoping)."""
    now = now or timezone.now()
    window_start = now - timedelta(days=max(1, days))

    items = Item.objects.all() if items is None else items
    items = (items.exclude(mode='alert')
             .filter(completed_at__gte=window_start, completed_at__lte=now))
    if project_key:
        items = items.filter(project__key=str(project_key).upper())

    rows = []
    for item in items.iterator():
        cycle, lead = derive_cycle(
            item.created_at, item.completed_at, _status_transitions(item))
        rows.append({
            'item_key': getattr(item, 'item_key', None) or str(item.pk),
            'title': getattr(item, 'title', '') or '',
            'team': item.assignment_group or '',
            'completed_at': item.completed_at.isoformat(),
            'cycle_seconds': round(cycle, 1),
            'lead_seconds': round(lead, 1),
        })
    rows.sort(key=lambda r: r['completed_at'])

    cycles = [r['cycle_seconds'] for r in rows]
    summary = {
        'count': len(cycles),
        'avg_seconds': round(sum(cycles) / len(cycles), 1) if cycles else None,
        'p50_seconds': _percentile(cycles, 50),
        'p85_seconds': _percentile(cycles, 85),
        # PLAT-9077 M-additive: p90 joins additively; p85/p95 unchanged
        # (binding note 3 — no breaking change to the live contract).
        'p90_seconds': _percentile(cycles, 90),
        'p95_seconds': _percentile(cycles, 95),
    }
    return {'rows': rows, 'summary': summary, 'window_days': days}


# Statuses considered terminal for the cumulative-flow "done band" and the
# M5/M6 open-work scoping. Mirrors views.TERMINAL_STATUS_SLUGS (kept local
# to avoid a circular import).
_CFD_TERMINAL = {
    'completed', 'cancelled', 'canceled', 'closed', 'done',
    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
    'failed', 'expired', 'deferred',
}


def _first_assignment_at(item_ids):
    """Earliest `action='assigned'` ItemActivity timestamp per item id.

    Served by the PLS-1244 composite index (action, created_at). Binding
    note 1 (PLAT-9077): first-assignment-only from the durable indexed
    record — never comment breadcrumbs; any future v1.1 hold enrichment
    sources RoutingAssignmentReceipt, not comments (PLS-1287).
    """
    if not item_ids:
        return {}
    first = {}
    rows = (ItemActivity.objects
            .filter(action='assigned', item_id__in=list(item_ids))
            .order_by('item_id', 'created_at')
            .values('item_id', 'created_at'))
    for r in rows:
        first.setdefault(r['item_id'], r['created_at'])
    return first


def _parked_seconds_before(intervals, until=None):
    """Pure: seconds of parking-status dwell inside [interval start, until].

    `until=None` measures each parking interval in full (open intervals run
    to their recorded duration, i.e. `now`). Used for the M3 parked
    decomposition: dwell in blocked/scheduled-style statuses before the
    first assignment is queue-wait context, not flow time.
    """
    total = 0.0
    for iv in intervals:
        if not is_parking_status(iv['status']):
            continue
        start = iv['entered_at']
        end = iv['exited_at']
        duration = iv['duration_seconds']
        if end is None:
            end = start + timedelta(seconds=duration)
        if until is not None:
            if start >= until:
                continue
            end = min(end, until)
            if end <= start:
                continue
        total += max(0.0, (end - start).total_seconds())
    return total


def queue_waits(days=30, project_key=None, now=None, items=None):
    """PLAT-9077 M3: per-item queue-wait + parked decomposition.

    queue_wait_seconds = first_assigned_at − created_at, from the durable
    indexed ItemActivity record (first assignment only — binding note 1).
    parked_seconds = dwell in parking statuses (blocked/scheduled/…, see
    PARKING_STATUS_SLUGS) BEFORE the first assignment, decomposed separately
    via the existing derive_intervals lineage; for never-assigned items it is
    the parking dwell to date and queue_wait_seconds is None (honest).

    Read-only; alert-mode rows excluded; optional project filter; optional
    pre-scoped queryset for org scoping. Default scope: items CREATED in the
    window (queue-wait is a birth-to-assignment measure, so completion state
    is irrelevant).
    """
    now = now or timezone.now()
    window_start = now - timedelta(days=max(1, days))

    items = Item.objects.all() if items is None else items
    items = (items.exclude(mode='alert')
             .filter(created_at__gte=window_start, created_at__lte=now))
    if project_key:
        items = items.filter(project__key=str(project_key).upper())

    item_list = list(items)
    first_assigned = _first_assignment_at([i.pk for i in item_list])

    rows = []
    for item in item_list:
        assigned_at = first_assigned.get(item.pk)
        intervals = derive_intervals(
            current_status=getattr(item, 'status', None),
            created_at=item.created_at,
            transitions=_status_transitions(item),
            now=now,
        )
        parked = _parked_seconds_before(intervals, until=assigned_at)
        rows.append({
            'item_key': getattr(item, 'item_key', None) or str(item.pk),
            'title': getattr(item, 'title', '') or '',
            'team': item.assignment_group or '',
            'created_at': item.created_at.isoformat(),
            'assigned': assigned_at is not None,
            'queue_wait_seconds': (
                round(max(0.0, (assigned_at - item.created_at).total_seconds()), 1)
                if assigned_at is not None else None
            ),
            'parked_seconds': round(parked, 1),
        })
    rows.sort(key=lambda r: r['created_at'])

    waits = [r['queue_wait_seconds'] for r in rows if r['queue_wait_seconds'] is not None]
    return {
        'rows': rows,
        'summary': {
            'count': len(rows),
            'assigned_count': len(waits),
            'unassigned_count': len(rows) - len(waits),
            'avg_queue_wait_seconds': (
                round(sum(waits) / len(waits), 1) if waits else None
            ),
            'p50_queue_wait_seconds': _percentile(waits, 50),
            'p90_queue_wait_seconds': _percentile(waits, 90),
            'avg_parked_seconds': (
                round(sum(r['parked_seconds'] for r in rows) / len(rows), 1)
                if rows else None
            ),
        },
        'window_days': days,
    }


def age_distribution(project_key=None, now=None, items=None):
    """PLAT-9077 M5: current-state age buckets for open work.

    Buckets of `now − created_at` for non-terminal, non-alert items — a
    current-state snapshot (binding note 4: no window param). Fixed edges
    ≤1d / ≤3d / ≤7d / ≤14d / ≤30d / >30d.
    """
    now = now or timezone.now()

    items = Item.objects.all() if items is None else items
    items = items.exclude(mode='alert').exclude(status__in=_CFD_TERMINAL)
    if project_key:
        items = items.filter(project__key=str(project_key).upper())

    edges = [
        ('le_1d', timedelta(days=1)),
        ('le_3d', timedelta(days=3)),
        ('le_7d', timedelta(days=7)),
        ('le_14d', timedelta(days=14)),
        ('le_30d', timedelta(days=30)),
    ]
    buckets = {name: 0 for name, _ in edges}
    buckets['gt_30d'] = 0
    oldest = None

    for item in items.iterator():
        age = now - item.created_at
        if age.total_seconds() < 0:  # backdated/clock-skew guard: count as newest
            age = timedelta(0)
        for name, edge in edges:
            if age <= edge:
                buckets[name] += 1
                break
        else:
            buckets['gt_30d'] += 1
        if oldest is None or item.created_at < oldest:
            oldest = item.created_at

    return {
        'buckets': buckets,
        'total_open': sum(buckets.values()),
        'oldest_created_at': oldest.isoformat() if oldest else None,
        'as_of': now.isoformat(),
    }


def wip_vs_cap(project_key=None, now=None, items=None):
    """PLAT-9077 M6: per-seat open assigned count vs the seat's WIP cap.

    Cap authority is the PLS-509 roster configuration (TeamRoster
    `active_wip_cap` on the seat's team, `wip_enforced=True`) — NOT the
    AgentWipCapacity row, which is only the admission row-lock target and
    carries no cap value. `cap=None` is honest when the seat's team has no
    enforced roster row (cap not configured for that seat). `over_cap` is
    computed only against a concrete cap (None → False).

    Open = assigned, non-terminal, non-alert. Read-only; optional project
    filter; optional pre-scoped queryset for org scoping.
    """
    now = now or timezone.now()

    items = Item.objects.all() if items is None else items
    items = (items.exclude(mode='alert')
             .exclude(status__in=_CFD_TERMINAL)
             .exclude(assignee_email=''))
    if project_key:
        items = items.filter(project__key=str(project_key).upper())

    seats = {}
    for item in items.iterator():
        email = (item.assignee_email or '').strip().lower()
        if not email:
            continue
        seat = seats.setdefault(email, {
            'assignee_email': email,
            'team': item.assignment_group or '',
            'open_count': 0,
        })
        seat['open_count'] += 1
        if not seat['team'] and item.assignment_group:
            seat['team'] = item.assignment_group

    from .workflow_service import WorkflowService  # lazy: avoid import cycles
    rows = []
    for seat in seats.values():
        cap = None
        team = (seat['team'] or '').strip()
        if team:
            config = WorkflowService._wip_roster_config(team)
            if config is not None:
                cap = int(config.active_wip_cap or 0) or None
        rows.append({
            'assignee_email': seat['assignee_email'],
            'team': seat['team'],
            'open_count': seat['open_count'],
            'cap': cap,
            'over_cap': bool(cap is not None and seat['open_count'] > cap),
        })
    rows.sort(key=lambda r: (-r['open_count'], r['assignee_email']))

    capped = [r for r in rows if r['cap'] is not None]
    return {
        'rows': rows,
        'summary': {
            'seats': len(rows),
            'seats_with_cap': len(capped),
            'over_cap_count': sum(1 for r in rows if r['over_cap']),
            'total_open_assigned': sum(r['open_count'] for r in rows),
        },
        'as_of': now.isoformat(),
    }


def _status_at(intervals, at):
    """Status an item held at instant `at`, or None if not yet created."""
    for iv in intervals:
        entered = iv['entered_at']
        exited = iv['exited_at']
        if entered <= at and (exited is None or at < exited):
            return iv['status']
    return None


def cumulative_flow(days=14, project_key=None, now=None, items=None):
    """PLS-361: end-of-day status counts per day for the CFD stacked areas.

    Read-only over the ItemActivity log (same derivation as team_throughput).
    Scope bound: items updated within the window OR currently non-terminal —
    items that reached a terminal status BEFORE the window and were never
    touched again are excluded, so the terminal band shows completions
    entering during the window rather than the project's full history (the
    endpoint documents this as `done_baseline_excluded`).
    """
    now = now or timezone.now()
    days = max(1, min(int(days), 90))  # bound long ranges (acceptance: edge cases)
    window_start = now - timedelta(days=days)

    items = Item.objects.all() if items is None else items
    items = items.exclude(mode='alert')
    if project_key:
        items = items.filter(project__key=str(project_key).upper())
    from django.db.models import Q
    items = items.filter(Q(updated_at__gte=window_start) | ~Q(status__in=_CFD_TERMINAL))

    # Day boundaries: end of each day in the window (last point = `now`).
    boundaries = []
    for n in range(days - 1, 0, -1):
        boundaries.append(now - timedelta(days=n))
    boundaries.append(now)

    counts = [dict() for _ in boundaries]
    for item in items.iterator():
        hist = item_stage_history(item, now=now)
        for i, at in enumerate(boundaries):
            status = _status_at(hist['intervals'], at)
            if status is None:
                continue
            key = str(status).strip().lower()
            counts[i][key] = counts[i].get(key, 0) + 1

    # Stable status ordering: non-terminal first (by first appearance), then
    # terminal — so the stacked areas read bottom=done band like a normal CFD.
    seen = []
    for day in counts:
        for st in day:
            if st not in seen:
                seen.append(st)
    ordered = [s for s in seen if s not in _CFD_TERMINAL] + [s for s in seen if s in _CFD_TERMINAL]

    return {
        'window_days': days,
        'days': [b.date().isoformat() for b in boundaries],
        'statuses': ordered,
        'series': {st: [day.get(st, 0) for day in counts] for st in ordered},
        'done_baseline_excluded': True,
    }

"""Prometheus exposition: urgent/high tasks assigned to no one (operator
directive 2026-09-17).

An urgent task that sits unassigned is potential fleet-wide blockage — the
rebalancer drain should place it within minutes. These gauges make any lapse
visible and alertable:

- ``pulse_urgent_unassigned_total{team, priority}`` — unassigned hands-on
  (todo/open/needs_triage) urgent/high items, per team.
- ``pulse_urgent_unassigned_oldest_hours{team}`` — age of the oldest such item
  per team, so a stale queue is distinguishable from a fresh one.

Cardinality is bounded: teams are a small closed set; no per-item labels.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

_UNASSIGNED = Q(assignee_email__isnull=True) | Q(assignee_email='')
_HANDS_ON = Q(status__in=('todo', 'open', 'needs_triage'))
_URGENT = Q(priority__iexact='urgent') | Q(priority__iexact='high')
_TERMINAL = Q(status__in=(
    'done', 'completed', 'cancelled', 'rejected', 'resolved',
    'resolved_elsewhere', 'closed', 'duplicate', 'archived',
))


def collect_urgent_unassigned_metrics():
    """Return (rows, oldest_hours_by_team) for the urgent-unassigned gauges."""
    from ..models import Item

    now = timezone.now()
    rows = (
        Item.objects.filter(_URGENT, _UNASSIGNED, _HANDS_ON)
        .exclude(_TERMINAL)
        .values('assignment_group', 'priority')
        .annotate(n=__import__('django.db.models', fromlist=['Count']).Count('id'))
    )
    series = []
    for r in rows:
        team = (r['assignment_group'] or 'none').strip().lower() or 'none'
        series.append((team, (r['priority'] or '').strip().lower(), r['n']))

    oldest = {}
    aged = (
        Item.objects.filter(_URGENT, _UNASSIGNED, _HANDS_ON)
        .exclude(_TERMINAL)
        .values('assignment_group', 'created_at')
        .order_by('created_at')
    )
    seen_teams = set()
    for r in aged:
        team = (r['assignment_group'] or 'none').strip().lower() or 'none'
        if team in seen_teams:
            continue
        seen_teams.add(team)
        if r['created_at']:
            oldest[team] = max(0.0, (now - r['created_at']).total_seconds() / 3600.0)
    return series, oldest


def render_urgent_unassigned_metrics() -> str:
    try:
        series, oldest = collect_urgent_unassigned_metrics()
    except Exception:
        logger.warning('urgent-unassigned metrics collection failed', exc_info=True)
        return ''
    lines = ['# urgent/high tasks assigned to no one (operator 2026-09-17)']
    total = 0
    for team, priority, n in series:
        total += n
        lines.append(
            f'pulse_urgent_unassigned_total{{team="{team}",priority="{priority}"}} {n}')
    lines.append(f'pulse_urgent_unassigned_total{{team="all",priority="any"}} {total}')
    for team, hours in sorted(oldest.items()):
        lines.append(
            f'pulse_urgent_unassigned_oldest_hours{{team="{team}"}} {hours:.2f}')
    return '\n'.join(lines) + '\n'

"""PLAT-1192 (PLAT-1180 deliverable c): monthly findings→mechanism conversion metric.

Computed purely from the shared learning-loop contract (Pulse links + labels only,
no new schema — aoi's PLAT-1180 design, 2026-07-02):

  - mechanism tasks carry label ``prevention-mechanism``;
  - retro finding tasks carry label ``retro-finding`` (materialized by the weekly
    retro compile, see ``post_weekly_retro_prompt``);
  - findings link to their mechanism via normal relates_to/blocks/is_blocked_by
    links (bidirectional, matching the #10469 close-gate validator);
  - an explicitly-waived finding carries a comment ``mechanism: N/A — <reason>``
    (same marker the close gate accepts).

Consumers: the monthly org-health digest (``post_monthly_org_health_digest``) and
the Company-State Snapshot API §5.8 ``learning_loop`` section (HLD PLAT-1190).
"""

import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

PREVENTION_LABEL = 'prevention-mechanism'
FINDING_LABEL = 'retro-finding'
NA_MARKER = 'mechanism: N/A — '
# Mirror the #10469 validator defaults (workflow_service._has_linked_labeled_issue_or_comment_marker)
LINK_TYPES = ('relates_to', 'blocks', 'is_blocked_by')
# Statuses that count as an incident "close" for findings_total. Matches the
# close-gate's target statuses (done/resolved/closed) plus the Item model's
# native 'completed'.
CLOSE_STATUSES = ('done', 'resolved', 'closed', 'completed')
# Non-workflow-aware terminal set for the staleness scan (superset of
# WorkflowService._status_is_terminal's static slugs — a mechanism task in any
# of these is shipped/parked, not "open").
TERMINAL_STATUSES = (
    'completed', 'done', 'closed', 'cancelled', 'canceled', 'rejected',
    'resolved', 'silenced', 'expired', 'deferred', 'duplicate', 'wont_fix',
    'merged', 'failed',
)
STALE_AFTER_DAYS = 30


def previous_month_bounds(now=None):
    """[start, end) datetimes of the calendar month before ``now`` (aware)."""
    now = now or timezone.now()
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_last_day = end - timezone.timedelta(days=1)
    start = prev_last_day.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, end


def compute_learning_loop_metrics(period_start, period_end, now=None, base_items=None):
    """Compute the findings→mechanism conversion metric for [start, end).

    Returns a dict:
      findings_total     retro-finding tasks created in the period + incident
                         closes in the period (deduped by item)
      retro_findings / incident_closes  the two components
      mechanisms_linked  DISTINCT prevention-mechanism tasks linked
                         (relates_to/blocks/is_blocked_by, either direction)
                         from those finding items
      na_count           findings carrying an explicit `mechanism: N/A — <reason>`
      conversion_rate    mechanisms_linked / (findings_total − na_count),
                         None when the denominator is 0
      stale_mechanisms   prevention-mechanism tasks non-terminal for >30d
                         (linked-but-never-shipped = the loop silently broken)
    """
    from ..models import Item, ItemActivity, ItemLink
    from .workflow_service import WorkflowService as W

    now = now or timezone.now()
    # The monthly digest intentionally keeps the historical all-company default.
    # Org-scoped consumers (notably company_state.snapshot.v1) pass a fail-closed
    # queryset so findings, incident closes, links, and stale mechanisms cannot
    # cross an organization boundary.
    base_items = base_items if base_items is not None else Item.objects.all()

    # ── findings: retro finding rows created in the period ──
    created_in_period = (
        base_items.filter(mode='task',
                          created_at__gte=period_start,
                          created_at__lt=period_end)
        .only('id', 'item_key', 'title', 'labels', 'tags', 'status'))
    retro_items = [it for it in created_in_period
                   if W._item_has_label(it, FINDING_LABEL)]

    # ── findings: incident closes in the period (via the status audit trail) ──
    # signals.py maps a status change TO the native 'completed' status to
    # action='completed' (not 'status_changed') — filter both actions or
    # native-path incident closes are undercounted (ren's PR #243 P1).
    close_acts = (
        ItemActivity.objects.filter(action__in=('status_changed', 'completed'),
                                    created_at__gte=period_start,
                                    created_at__lt=period_end,
                                    new_value__in=CLOSE_STATUSES,
                                    item__in=base_items,
                                    item__mode='task',
                                    item__workflow_name__icontains='incident')
        .select_related('item'))
    incident_items = {act.item_id: act.item for act in close_acts}

    findings = {it.id: it for it in retro_items}
    retro_count = len(findings)
    incident_count = 0
    for iid, it in incident_items.items():
        if iid not in findings:
            findings[iid] = it
            incident_count += 1
    finding_ids = set(findings)

    # ── distinct prevention-mechanism tasks linked from any finding ──
    mechanism_ids, mechanism_keys = set(), []
    if finding_ids:
        links = (ItemLink.objects.filter(
            Q(source_item_id__in=finding_ids) | Q(target_item_id__in=finding_ids),
            source_item__in=base_items,
            target_item__in=base_items,
            link_type__in=LINK_TYPES)
            .select_related('source_item', 'target_item'))
        for link in links:
            for other in (link.source_item, link.target_item):
                if other.id in finding_ids or other.id in mechanism_ids:
                    continue
                if W._item_has_label(other, PREVENTION_LABEL):
                    mechanism_ids.add(other.id)
                    mechanism_keys.append(other.item_key or f'#{other.id}')

    # ── explicit N/A markers on findings ──
    na_count = sum(
        1 for it in findings.values() if W._comment_marker_present(it, NA_MARKER))

    denominator = len(findings) - na_count
    conversion_rate = (len(mechanism_ids) / denominator) if denominator > 0 else None

    # ── staleness: prevention-mechanism tasks open >30d ──
    stale_cutoff = now - timezone.timedelta(days=STALE_AFTER_DAYS)
    stale_candidates = (
        base_items.filter(mode='task', is_archived=False,
                          created_at__lte=stale_cutoff)
        .exclude(status__in=TERMINAL_STATUSES)
        .only('id', 'item_key', 'title', 'labels', 'tags', 'status', 'created_at'))
    stale_mechanisms = [
        {'item_key': it.item_key or f'#{it.id}',
         'title': it.title,
         'status': it.status,
         'age_days': (now - it.created_at).days}
        for it in stale_candidates if W._item_has_label(it, PREVENTION_LABEL)]

    return {
        'period_start': period_start.isoformat(),
        'period_end': period_end.isoformat(),
        'findings_total': len(findings),
        'retro_findings': retro_count,
        'incident_closes': incident_count,
        'mechanisms_linked': len(mechanism_ids),
        'mechanism_keys': sorted(mechanism_keys),
        'na_count': na_count,
        'conversion_rate': conversion_rate,
        'stale_mechanisms': sorted(stale_mechanisms,
                                   key=lambda s: -s['age_days']),
    }


def format_learning_loop_block(metrics, month_label):
    """Render the metric dict as the org-health digest's human-readable block."""
    rate = metrics['conversion_rate']
    rate_str = f"{rate:.0%}" if rate is not None else 'n/a — no gated findings'
    lines = [
        f"\U0001f4c8 Learning-loop conversion — {month_label}",
        f"- findings: {metrics['findings_total']} "
        f"(retro rows: {metrics['retro_findings']}, "
        f"incident closes: {metrics['incident_closes']})",
        f"- mechanisms linked: {metrics['mechanisms_linked']} distinct "
        f"prevention-mechanism task(s)"
        + (f" [{', '.join(metrics['mechanism_keys'])}]"
           if metrics['mechanism_keys'] else ''),
        f"- explicit N/A: {metrics['na_count']}",
        f"- conversion rate: {rate_str}",
    ]
    stale = metrics['stale_mechanisms']
    if stale:
        detail = ', '.join(
            f"{s['item_key']} ({s['age_days']}d, {s['status']})" for s in stale[:10])
        more = f" +{len(stale) - 10} more" if len(stale) > 10 else ''
        lines.append(
            f"- ⚠️ stale mechanisms (open >{STALE_AFTER_DAYS}d): "
            f"{len(stale)} — {detail}{more}")
    else:
        lines.append(
            f"- stale mechanisms (open >{STALE_AFTER_DAYS}d): none")
    return '\n'.join(lines)

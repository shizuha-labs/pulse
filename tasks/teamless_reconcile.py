"""PLAT-11174: periodic teamless-ITEM reconcile (urgent demand stranded in
``team-none``).

Retro/defect (nagi, 2026-10-04): active items keep reaching the queue with
``assignment_group = null`` (project "none"). The capacity drain cannot place
group-less demand, so urgent work strands until a human-in-the-loop alert
(PulseUrgentTaskUnassigned) fires 2h+ later. The create-time backstop
(``assignment_group_backstop.py``, PLAT-9100) covers API/MCP creates, but
non-API mint paths — recurring engine re-fire, data migrations, internal
internal scripts — bypass it (three instances on 2026-10-03 alone, including
CTX-478 and a platform-wide CI-blocking incident).

This sweep COMPOSES with that backstop rather than replacing it: backstop =
correct-at-create; reconcile = cure-at-queue-reach for every mint path,
including future ones.

Contract (acceptance §3):
  - Detect ACTIVE queue-eligible items with null/empty ``assignment_group``.
    Terminal and roadmap ('backlog' = owned-but-NOT-active) items are fine and
    excluded.
  - Auto-derive the owning group via the shared PLS-933 true-owner resolver
    (``WorkflowService._derive_owner_team`` — project/workflow/tags signals,
    exact-org registered candidates only). Nothing is invented: an
    unresolvable item is LEDGERED and left parked, never blind-stamped.
  - Every stamp writes provenance into ``metadata['assignment_group_reconcile']``
    (what/when/why) and an ERROR-level audit log, so the loss is correctable
    and measurable.
  - Emit ``pulse_teamless_items{priority}`` gauge counts every run (opentelemetry
    metrics when installed; structured audit log always — the gap must be
    visible even with OTEL disabled).
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from .models import Item

logger = logging.getLogger(__name__)

TEAMLESS_FLAG = 'assignment_group_reconcile'

# Statuses that make a teamless item acceptable: terminal cycles and the
# owned-but-not-active roadmap. Everything else active + queue-eligible with
# no group is a defect row.
NON_DEMAND_STATUSES = frozenset({
    'done', 'completed', 'closed', 'cancelled', 'canceled', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred', 'rejected', 'backlog',
})


def teamless_active_qs():
    """Active, queue-eligible Items with a null/empty ``assignment_group``."""
    return (
        Item.objects.filter(mode='task')
        .filter(Q(assignment_group__isnull=True) | Q(assignment_group=''))
        .exclude(status__in=NON_DEMAND_STATUSES)
        .order_by('created_at', 'id')
    )


def reconcile_teamless_items(*, dry_run: bool = False) -> dict:
    """Cure teamless demand: stamp derivable groups, ledger the rest.

    Returns a summary dict:
      {
        'scanned': n, 'backfilled': n, 'parked': n,
        'backfilled_keys': [...], 'parked_keys': [...],
        'gauge': {priority: count}, 'dry_run': bool,
      }
    """
    scanned = 0
    backfilled = 0
    parked = 0
    backfilled_keys = []
    parked_keys = []
    gauge = {}

    try:
        from .services.workflow_service import WorkflowService

        resolver = WorkflowService
    except Exception:
        logger.exception(
            "PLAT-11174 reconcile: workflow resolver unavailable — scanning only"
        )
        resolver = None

    for item in teamless_active_qs().iterator():
        scanned += 1
        priority = (getattr(item, 'priority', '') or 'unknown').strip().lower() or 'unknown'
        gauge[priority] = gauge.get(priority, 0) + 1

        derived = None
        if resolver is not None:
            try:
                derived = resolver._derive_owner_team(item)
            except Exception:
                logger.exception(
                    "PLAT-11174 reconcile: derivation error for %s — treated as parked",
                    getattr(item, 'item_key', getattr(item, 'pk', '?')),
                )

        if derived:
            backfilled += 1
            backfilled_keys.append(getattr(item, 'item_key', getattr(item, 'pk', '?')))
            if not dry_run:
                meta = dict(getattr(item, 'metadata', None) or {})
                meta[TEAMLESS_FLAG] = {
                    'action': 'backfilled',
                    'source': derived,
                    'at': timezone.now().isoformat(),
                }
                item.metadata = meta
                item.assignment_group = derived
                try:
                    item.save(update_fields=['assignment_group', 'metadata'])
                except Exception:
                    logger.exception(
                        "PLAT-11174 reconcile: save failed for %s — group not stamped",
                        getattr(item, 'item_key', getattr(item, 'pk', '?')),
                    )
            logger.error(
                "PLAT-11174 teamless demand BACKFILLED: %s priority=%s -> %s (dry_run=%s)",
                getattr(item, 'item_key', getattr(item, 'pk', '?')), priority,
                derived, dry_run,
            )
        else:
            parked += 1
            parked_keys.append(getattr(item, 'item_key', getattr(item, 'pk', '?')))
            logger.error(
                "PLAT-11174 teamless demand PARKED (not derivable — drain still "
                "cannot place it; needs a named team): %s priority=%s",
                getattr(item, 'item_key', getattr(item, 'pk', '?')), priority,
            )

    summary = {
        'scanned': scanned,
        'backfilled': backfilled,
        'parked': parked,
        'backfilled_keys': backfilled_keys,
        'parked_keys': parked_keys,
        'gauge': gauge,
        'dry_run': bool(dry_run),
    }

    # Always-visible audit line: the gap must be queryable even with OTEL off.
    logger.error(
        "PLAT-11174 reconcile complete: scanned=%(scanned)d backfilled=%(backfilled)d "
        "parked=%(parked)d gauge=%(gauge)s dry_run=%(dry_run)s" % {
            **summary, 'dry_run': str(summary['dry_run']).lower(),
        }
    )
    return summary


def _emit_teamless_gauge(summary: dict) -> None:
    """Record ``pulse_teamless_items`` per priority (best-effort, never raises)."""
    try:
        from opentelemetry import metrics

        meter = metrics.get_meter('shizuha_pulse.teamless_reconcile')
        for priority, count in (summary.get('gauge') or {}).items():
            meter.create_gauge(
                name='pulse_teamless_items',
                description='Active queue-eligible items with no assignment_group',
                unit='items',
            ).set(count, {'priority': priority})
    except ImportError:
        pass
    except Exception:
        logger.exception("PLAT-11174: gauge emission failed (audit log remains authoritative)")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_teamless_reconcile(self) -> dict:
    """Beat entry: cure teamless demand and emit the gauge."""
    summary = reconcile_teamless_items(dry_run=False)
    _emit_teamless_gauge(summary)
    return summary


def setup_teamless_reconcile_schedule() -> None:
    """Self-register the 15-past schedule (same pattern as the lane watchdog)."""
    try:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        schedule, _ = CrontabSchedule.objects.get_or_create(minute=15, hour='*')
        PeriodicTask.objects.get_or_create(
            name='pl-11174-teamless-reconcile',
            defaults={
                'task': 'tasks.teamless_reconcile.run_teamless_reconcile',
                'crontab': schedule,
            },
        )
    except ImportError:
        pass
    except Exception:
        logger.exception("PLAT-11174: schedule registration failed")

"""
Escalate every task that blocks active work to the highest priority (urgent).

Operator directive 2026-08-08
-----------------------------
A task that blocks another task is a critical-path item: if it sits at a lower
priority the rebalancer keeps deferring it, so every dependent stays blocked
even when seats are free. Blocker tasks therefore MUST carry ``urgent`` — the
highest priority — so the rebalance sweep assigns them before anything else.

The write path enforces this invariant automatically (link-boundary escalation
plus a pre-save backstop). This command backfills the invariant for EXISTING
blocker tasks that predate the directive, so the current backlog of blocked
work (e.g. the architecture queue) gets its blocker chain promoted immediately.

Only tasks that still release ACTIVE (non-terminal) blocked descendants are
escalated. A blocker whose dependents are all resolved keeps its chosen
priority (the invariant only applies while the block is live).

Dry-run by default (--commit to write).

Usage
-----
    python manage.py escalate_blocker_priorities                # dry-run
    python manage.py escalate_blocker_priorities --commit       # write
    python manage.py escalate_blocker_priorities --organization shizuha --commit
    python manage.py escalate_blocker_priorities --limit 500 --commit
    python manage.py escalate_blocker_priorities --json         # machine-readable
"""
from django.core.management.base import BaseCommand

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService


class Command(BaseCommand):
    help = (
        "Escalate tasks that block active work to urgent priority so the "
        "rebalancer assigns them first. Dry-run by default — pass --commit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Actually write the escalation (default: dry-run only).',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Cap the number of items processed (helpful for dry-run).',
        )
        parser.add_argument(
            '--json', action='store_true',
            help='Emit a machine-readable JSON report.',
        )

    def handle(self, *args, **options):
        commit = options['commit']
        limit = options['limit']
        as_json = options['json']

        qs = (
            Item.objects.filter(mode='task')
            .exclude(status__in=WorkflowService.TERMINAL_STATUS_SLUGS)
        )
        # Only look at items that are not already urgent — those are already at
        # the invariant value. This keeps the scan small.
        qs = qs.exclude(priority__iexact='urgent').only(
            'pk', 'item_key', 'priority', 'status',
        )
        if limit:
            qs = qs[:limit]
        candidates = list(qs)

        escalated = 0
        skipped_already = 0
        skipped_no_blocker = 0
        rows = []
        for item in candidates:
            if not WorkflowService._has_active_blocked_descendants(item):
                skipped_no_blocker += 1
                continue
            if (item.priority or '').strip().lower() == 'urgent':
                skipped_already += 1
                continue
            rows.append({
                'key': item.item_key or str(item.pk),
                'pk': item.pk,
                'priority': item.priority,
                'to': 'urgent',
            })
            if commit:
                from django.db import transaction
                from django.utils import timezone
                with transaction.atomic():
                    fresh = Item.objects.select_for_update().get(pk=item.pk)
                    fresh.priority = 'urgent'
                    fresh.updated_at = timezone.now()
                    fresh._activity_actor = (0, 'system@pulse')
                    fresh.save(update_fields=['priority', 'updated_at'])
            escalated += 1

        if as_json:
            import json
            self.stdout.write(json.dumps({
                'commit': commit,
                'escalated': escalated,
                'skipped_no_blocker': skipped_no_blocker,
                'skipped_already_urgent': skipped_already,
                'rows': rows,
            }, indent=2))
            return

        self.stdout.write(
            'Mode: %s' % ('COMMIT (writes)' if commit else 'DRY-RUN (no writes)')
        )
        self.stdout.write('Escalated blocker tasks to urgent: %d' % escalated)
        self.stdout.write('Already urgent: %d' % skipped_already)
        self.stdout.write('Not a live blocker (unchanged): %d' % skipped_no_blocker)
        for r in rows[:50]:
            self.stdout.write(
                '  %s  %s -> urgent' % (
                    r['key'], r['priority'],
                )
            )
        if len(rows) > 50:
            self.stdout.write('  ... and %d more' % (len(rows) - 50))

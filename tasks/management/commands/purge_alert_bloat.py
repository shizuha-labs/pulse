"""One-shot / ops purge of mode=alert Pulse bloat.

Operator doctrine: alerts are DM-only, not durable Pulse work items.
Historical Alertmanager ingestion created 10k+ mode=alert rows.

Usage:
  python manage.py purge_alert_bloat --dry-run
  python manage.py purge_alert_bloat --resolved-days 0 --firing-days 0   # purge ALL alerts
  python manage.py purge_alert_bloat --resolved-days 0 --keep-firing
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = 'Purge mode=alert Item rows that bloat Pulse (resolved + optionally stale firing).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Count only; do not delete')
        parser.add_argument(
            '--resolved-days',
            type=int,
            default=0,
            help='Delete terminal alerts with updated_at older than N days (0 = all terminal)',
        )
        parser.add_argument(
            '--firing-days',
            type=int,
            default=0,
            help='Delete firing/acked alerts older than N days (0 = all firing/acked unless --keep-firing)',
        )
        parser.add_argument(
            '--keep-firing',
            action='store_true',
            help='Never delete firing/acknowledged rows',
        )
        parser.add_argument('--batch-size', type=int, default=2000)

    def handle(self, *args, **options):
        from tasks.models import Item

        dry = options['dry_run']
        batch = max(100, int(options['batch_size']))
        now = timezone.now()
        terminal = ['resolved', 'cancelled', 'canceled', 'completed', 'closed']
        active = ['firing', 'acknowledged']

        total_deleted = 0

        # Terminal alerts
        tq = Item.objects.filter(mode='alert', status__in=terminal)
        rd = int(options['resolved_days'])
        if rd > 0:
            tq = tq.filter(updated_at__lt=now - timedelta(days=rd))
        tc = tq.count()
        self.stdout.write(f'terminal mode=alert candidates: {tc}')
        if not dry and tc:
            total_deleted += self._batch_delete(tq, batch)

        # Firing / acked
        if not options['keep_firing']:
            fq = Item.objects.filter(mode='alert', status__in=active)
            fd = int(options['firing_days'])
            if fd > 0:
                fq = fq.filter(updated_at__lt=now - timedelta(days=fd))
            fc = fq.count()
            self.stdout.write(f'firing/acked mode=alert candidates: {fc}')
            if not dry and fc:
                total_deleted += self._batch_delete(fq, batch)
        else:
            self.stdout.write('keeping firing/acked alerts (--keep-firing)')

        remaining = Item.objects.filter(mode='alert').count()
        mode = 'DRY-RUN' if dry else 'APPLIED'
        self.stdout.write(self.style.SUCCESS(
            f'{mode}: deleted~{total_deleted} rows; mode=alert remaining={remaining}'
        ))

    def _batch_delete(self, qs, batch_size: int) -> int:
        from tasks.models import Item
        deleted = 0
        while True:
            ids = list(qs.values_list('pk', flat=True)[:batch_size])
            if not ids:
                break
            with transaction.atomic():
                n, _ = Item.objects.filter(pk__in=ids).delete()
            deleted += n
            self.stdout.write(f'  deleted batch {n} (running total {deleted})')
        return deleted

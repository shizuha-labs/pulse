"""PLAT-5928 — drain the VerifiedAlertDelivery outbox (due-row recovery relay).

Postgres-authoritative recovery: on-demand drain of due recipient-phase outbox
rows with SKIP LOCKED leases and fencing epochs. Redis/broker are wake
accelerators only; this command is the narrow due-row recovery relay plus the
post-commit wake subscriber's drain target.

Usage:
    python manage.py relay_alert_delivery --limit 50
    python manage.py relay_alert_delivery --loop   # drain-until-idle
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from alertdelivery.relay import drain_once


class Command(BaseCommand):
    help = 'Drain due VerifiedAlertDelivery outbox rows (PLAT-4571 slice 3).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=25)
        parser.add_argument('--loop', action='store_true',
                            help='Drain repeatedly until no due rows remain.')

    def handle(self, *args, **options):
        limit = options['limit']
        total_claimed = total_delivered = 0
        while True:
            summary = drain_once(limit=limit)
            total_claimed += summary['claimed']
            total_delivered += summary['delivered']
            self.stdout.write(
                f"relay pass claimed={summary['claimed']} "
                f"delivered={summary['delivered']} "
                f"deferred={summary['deferred']} conflict={summary['conflict']} "
                f"errors={summary['errors']}"
            )
            if not options['loop'] or summary['claimed'] == 0:
                break
        self.stdout.write(f"total claimed={total_claimed} delivered={total_delivered}")

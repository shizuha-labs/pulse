"""PLAT-9682: run the typed-escalation channel health canary.

Drives the REAL escalation-hold commit machinery on a dedicated canary item
(preserve-without-routing branch — never routes to admin-ops, never pages a
human) and fires a deduplicated critical alert on commit failure, naming the
fallback channel. Scheduled by the Pulse recurring engine / cron.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tasks.services.escalation_canary_probe import run_escalation_canary


class Command(BaseCommand):
    help = ('Exercise the typed-escalation hold-commit path on the canary '
            'carrier and alert on commit failure (PLAT-9682).')

    def handle(self, *args, **options):
        result = run_escalation_canary()
        self.stdout.write(
            f"escalation-canary: outcome={result.get('outcome')} "
            f"alert_fired={result.get('alert_fired')} "
            f"item={result.get('canary_item')} "
            f"duration_ms={result.get('duration_ms')}"
        )
        if result.get('outcome') not in ('green',):
            self.stdout.write(
                f"  detail: {result.get('reason') or result.get('detail') or result.get('outcome')}"
            )

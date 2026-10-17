"""PLS-1045: run the critical-path stall probe.

Escalates urgent/high items stranded in todo/in_progress past the stall
threshold (CRITICAL_PATH_STALL_HOURS, default 1h) to a capacity decision
(CoS/flow-owner) instead of a repeat nudge, and surfaces seat-availability
chain desyncs. Scheduled by the Pulse recurring engine / cron.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tasks.services.critical_path_stall import check_critical_path_stalls


class Command(BaseCommand):
    help = 'Escalate stranded critical-path items to a capacity decision.'

    def handle(self, *args, **options):
        result = check_critical_path_stalls()
        self.stdout.write(
            f"critical-path-stall: checked={result['checked']} "
            f"stalled={len(result['stalled'])} escalated={result['escalated']}"
        )
        for s in result['stalled']:
            self.stdout.write(
                f"  stalled item={s['item_key']} priority={s['priority']} "
                f"assignee={s['assignee'] or 'none'} seat={s['seat_available']} "
                f"reason={s['reason']}"
            )

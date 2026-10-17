"""PLS-1041: run the merge-lane stall-escalation sweep.

Re-resolves ``awaiting_merge`` items with a mergeable PR that have waited past
the stall threshold (``PULSE_MERGE_STALL_HOURS``, default 6h) to a distinct
available merger, or fires a merge-lane stall alert when none is selectable so a
capacity-saturated lane never silently strands approved PRs. Scheduled by the
Pulse recurring engine / cron; safe to run frequently (dedup + threshold).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tasks.services.merge_stall import check_merge_stalls


class Command(BaseCommand):
    help = 'Escalate awaiting_merge items past the stall threshold to a live merger or alert.'

    def handle(self, *args, **options):
        result = check_merge_stalls()
        self.stdout.write(
            f"merge-stall: checked={result['checked']} "
            f"reassigned={result['reassigned']} escalated={result['escalated']}"
        )
        for s in result['stalled']:
            self.stdout.write(
                f"  stalled item={s['item_key']} held_at={s['held_at']}"
            )

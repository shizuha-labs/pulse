"""PLS-1037 (S3): run the lane-stall check across all active lanes.

Fires a lane-level alert (with pre-computed failure-mode classification) when a
lane has zero routable seats with queued demand past the debounce window, and
resolves/clears the debounce state when the lane recovers. Scheduled by the
Pulse recurring engine / cron; safe to run frequently (dedup + debounce).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tasks.services.lane_stall import check_all_lanes


class Command(BaseCommand):
    help = 'Detect lane stalls and fire lane-stall alerts with failure-mode classification.'

    def handle(self, *args, **options):
        results = check_all_lanes()
        fired = [r for r in results if r.get('action') == 'fired']
        debouncing = [r for r in results if r.get('action') == 'debouncing']
        self.stdout.write(
            f'lanes checked={len(results)} fired={len(fired)} debouncing={len(debouncing)}'
        )
        for r in fired:
            self.stdout.write(
                f"  FIRED lane={r['team']} mode={r['failure_mode']} "
                f"routable={r['routable_seats']} demand={r['demand']}"
            )
        for r in debouncing:
            self.stdout.write(
                f"  debouncing lane={r['team']} mode={r['failure_mode']}"
            )

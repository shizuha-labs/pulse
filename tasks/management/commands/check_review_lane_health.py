"""PLAT-7062: run the review-lane health probe.

Detects a critical-path PR (urgent/high, in_review, assignment_group='review')
stranded past the stall threshold (REVIEW_LANE_STALL_HOURS, default 1h) while
live idle review seats exist (routing desync) or with no live idle seat
(review-lane starvation). Escalates to a capacity decision (CoS/flow-owner).
Scheduled by the Pulse recurring engine / cron.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tasks.services.review_lane_health import check_review_lane_health


class Command(BaseCommand):
    help = 'Escalate stranded critical-path review items to a capacity decision.'

    def handle(self, *args, **options):
        result = check_review_lane_health()
        self.stdout.write(
            f"review-lane-health: checked={result['checked']} "
            f"waiting={len(result['waiting'])} desync={len(result['desync'])} "
            f"starvation={len(result['starvation'])} escalated={result['escalated']}"
        )
        for e in result['desync']:
            self.stdout.write(
                f"  DESYNC item={e['item_key']} priority={e['priority']} "
                f"live_idle={e['live_idle_seats']}"
            )
        for e in result['starvation']:
            self.stdout.write(
                f"  STARVATION item={e['item_key']} priority={e['priority']} "
                f"live_busy={e['live_busy_seats']} down={e['down_seats']} "
                f"of {e['seat_count']}"
            )

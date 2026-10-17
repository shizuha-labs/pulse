"""
Reconcile the recurring timer set (pulse:timers:recurring) against the DB.

Recurring timers are armed only by (a) scheduler startup bootstrap and (b) the
Item post_save signal. There is no periodic reconciliation (PLS-980 adds one to
the 60s sweep), so any active recurring item that loses its timer — post-save
bypass (bulk .update() on metadata), owner cleared to the group queue and
restored without a save, or a Redis restart without a scheduler restart —
stays silent until the next scheduler start.

This command is the manual/on-demand counterpart to PLS-980's automatic sweep:
it re-arms every active recurring item that is absent from
``pulse:timers:recurring`` and reports what it changed. It is read-mostly and
safe to run at any time — re-arming an already-armed item is a no-op (the zset
score is simply re-set to the item's due_date).

Usage:
    python manage.py reconcile_recurring_timers
    python manage.py reconcile_recurring_timers --dry-run   # report only
"""

from django.core.management.base import BaseCommand

from tasks.scheduler import KEY_RECURRING, get_timer_manager
from tasks.services.recurring_cycle_state import STOP_STATUSES
from tasks.services.recurring_invariants import _active_recurring_items


class Command(BaseCommand):
    help = (
        "Re-arm active recurring items absent from Redis pulse:timers:recurring "
        "(manual counterpart to PLS-980's 60s sweep)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=False,
            help="Report missing timers without re-arming them (read-only).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        tm = get_timer_manager()
        r = tm._redis

        missing = []
        for item in _active_recurring_items():
            # _active_recurring_items() excludes scheduler.RECURRING_STOP_STATUSES
            # ({cancelled, expired, deferred}), but the cycle state machine's
            # STOP_STATUSES additionally covers rejected/duplicate/wont_fix/failed.
            # Re-arming those would pop on the next fire and be immediately
            # cancelled — re-creating the exact drift this command fixes. Skip
            # them so the command never re-arms a status the fire path stops.
            if (item.status or '').strip().lower() in STOP_STATUSES:
                continue
            try:
                present = r.zscore(KEY_RECURRING, str(item.pk)) is not None
            except Exception as exc:  # RedisError
                self.stderr.write(
                    f"Redis query failed for item {item.pk}: {exc}"
                )
                continue
            if not present:
                missing.append(item)

        if not missing:
            self.stdout.write(self.style.SUCCESS(
                "No active recurring items are missing their timer."
            ))
            return

        self.stdout.write(
            f"Found {len(missing)} active recurring item(s) missing from "
            f"{KEY_RECURRING}:"
        )
        for item in missing:
            key = getattr(item, "item_key", None) or f"#{item.pk}"
            team = (
                getattr(item, "assignment_group", None)
                or getattr(item, "assignee_email", None)
                or "?"
            )
            self.stdout.write(
                f"  - {key} (pk={item.pk}, team={team}, "
                f"status={item.status}, due={item.due_date})"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — no timers re-armed. Re-run without --dry-run to fix."
            ))
            return

        re_armed = 0
        for item in missing:
            score = item.due_date.timestamp() if item.due_date else None
            if score is None:
                # Fall back to now so the scheduler picks it up on the next tick.
                from django.utils import timezone
                score = timezone.now().timestamp()
            tm.schedule_recurring(str(item.pk), score)
            re_armed += 1
            key = getattr(item, "item_key", None) or f"#{item.pk}"
            self.stdout.write(f"  re-armed {key} (pk={item.pk})")

        self.stdout.write(self.style.SUCCESS(
            f"Re-armed {re_armed} recurring timer(s). "
            f"Verify pulse_recurring_missing_timer drops to 0."
        ))

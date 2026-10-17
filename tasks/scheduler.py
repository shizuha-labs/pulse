"""
Redis sorted set timer scheduler for Pulse notifications.

Replaces Celery ETA scheduling with Redis ZRANGEBYSCORE polling.
Three sorted sets:
  - pulse:timers:reminder  — pre-due reminders (non-recurring)
  - pulse:timers:due       — one-shot overdue alerts (non-recurring)
  - pulse:timers:recurring — periodic fires for is_recurring items
  - pulse:timers:evidence  — evidence-blocker escalation deadlines

A scheduler loop polls every 100ms, dispatches to Celery .delay() for
immediate execution.
"""

import logging
import signal
import time
from datetime import datetime, timedelta

import redis
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Redis sorted set keys
KEY_REMINDER = 'pulse:timers:reminder'
KEY_DUE = 'pulse:timers:due'
KEY_RECURRING = 'pulse:timers:recurring'
KEY_EVIDENCE = 'pulse:timers:evidence'
KEY_PROCESSED_PREFIX = 'pulse:processed'
# PLS-1416: per-item dedup lease guarding fire_recurring_due publishes. The
# recurring redelivery timer re-arms every RECURRING_DELIVERY_RETRY (60s);
# with a broker backlog, a timer can pop while its previous fire is still
# sitting in the queue, and re-publishing every beat multiplied the mint
# rate ~24x (25k enqueued/day vs ~750 popped). The lease is taken by the
# dispatcher before .delay() and released by the task itself when it exits,
# so at most one delivery is in flight per item regardless of queue latency.
KEY_DISPATCH_LEASE_PREFIX = 'pulse:recurring-dispatch'

# Terminal statuses — items in these states should have no timers
# (except recurring items, which are handled specially below).
TERMINAL_STATUSES = frozenset({
    'completed', 'cancelled', 'resolved', 'failed', 'rejected', 'expired',
    'deferred',
})

# For recurring items, these statuses mean "cycle is done — re-fire next
# period". Everything NOT in this set (active or cancelled) skips the fire.
RECURRING_REFIRE_STATUSES = frozenset({
    'completed', 'resolved', 'done', 'closed',
})

# Recurring items in these statuses stop cycling permanently.
RECURRING_STOP_STATUSES = frozenset({
    'cancelled', 'expired', 'deferred',
})

# Lua script: atomically pop items with score <= threshold
# Returns list of member values (item IDs) that were removed
_LUA_ATOMIC_POP = """
local items = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
if #items > 0 then
    redis.call('ZREM', KEYS[1], unpack(items))
end
return items
"""

# Recurring dispatch also needs the score that was consumed.  The score is the
# expected due boundary carried into Celery; fetching the row later without it
# lets an old queued delivery accidentally claim a newer generation.
_LUA_ATOMIC_POP_WITH_SCORES = """
local items = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'WITHSCORES', 'LIMIT', 0, ARGV[2])
if #items > 0 then
    for i = 1, #items, 2 do
        redis.call('ZREM', KEYS[1], items[i])
    end
end
return items
"""


class TimerManager:
    """API for scheduling and cancelling Redis sorted set timers."""

    def __init__(self, redis_client=None):
        if redis_client is not None:
            self._redis = redis_client
        else:
            self._redis = redis.Redis.from_url(
                settings.CELERY_BROKER_URL,
                decode_responses=True,
            )

    def schedule_item(self, item):
        """
        Inspect item state and schedule/cancel timers as needed.
        Called from post_save signal — must never raise.
        """
        item_id = str(item.pk)

        # ── Recurring items take a dedicated path ────────────────────
        # Any item with is_recurring=True, a valid frequency, and a routing
        # owner lives on KEY_RECURRING only.  A concrete assignee is optional:
        # team-owned recurrence drivers deliberately sit group-queued while no
        # compatible seat is available.  Excluding those rows here made a
        # popped timer disappear permanently (CTX-478).
        # It lives on KEY_RECURRING only. It does NOT get reminder or due
        # timers — the periodic fire IS the notification.
        #
        # due_date is the single source of truth for the next fire time.
        # If the item was created without one, we auto-initialize it so
        # the scheduler can pick it up immediately.
        metadata = getattr(item, 'metadata', None)
        metadata = metadata if isinstance(metadata, dict) else {}
        has_recurring_owner = bool(
            item.assignee_id
            or (getattr(item, 'assignment_group', None) or '').strip()
            or str(metadata.get('recurring_owner_team') or '').strip()
        )
        is_recurring_ready = (
            item.mode == 'task'
            and getattr(item, 'is_recurring', False)
            and bool(getattr(item, 'frequency', None))
            and getattr(item, 'frequency', None) != 'one_time'
            and has_recurring_owner
        )

        if is_recurring_ready:
            # Permanent stop statuses → cancel everything
            if item.status in RECURRING_STOP_STATUSES:
                self.cancel_all(item_id)
                return

            # Ensure due_date (= the NEXT FIRE time) is sane before scheduling:
            # set it if missing, AND clamp it if it has drifted far into the
            # future. A recurring task's due_date is its next fire, never a
            # far-future deadline — if it exceeds ~2 periods out (legacy advance
            # runaway, or a task created with a deadline-style due), the cadence
            # is broken because the scheduler only fires due<=now, so we reset
            # to fire soon. No-ops when due_date is already within range.
            self._init_recurring_due_date(item)
            if item.due_date is None:
                logger.warning(
                    "Cannot schedule recurring item %s: frequency=%r "
                    "pattern=%r couldn't be interpreted",
                    item_id, item.frequency, item.recurrence_pattern,
                )
                self.cancel_recurring(item_id)
                return

            # Otherwise keep it in KEY_RECURRING (even if currently "done")
            # — the fire handler will decide whether to reset-and-notify or
            # skip based on the status it observes at fire time.
            self.schedule_recurring(item_id, item.due_date)
            self.cancel_reminder(item_id)
            self.cancel_due(item_id)
            return

        # ── Non-recurring items (original logic) ────────────────────────
        # Also clear any stale recurring entry (e.g. item was recurring
        # before but is_recurring just flipped to False).
        self.cancel_recurring(item_id)

        # Terminal status → cancel everything
        if item.status in TERMINAL_STATUSES:
            self.cancel_all(item_id)
            return

        # Reminder timer
        if (
            item.mode == 'task'
            and item.reminder_time
            and not item.reminder_sent
            and item.assignee_id
        ):
            self.schedule_reminder(item_id, item.reminder_time)
        else:
            self.cancel_reminder(item_id)

        # Due date timer — only schedule if not yet notified for this due_date
        if (
            item.due_date
            and item.assignee_id
            and item.status in ('pending', 'in_progress')
            and not item.overdue_notified
        ):
            self.schedule_due(item_id, item.due_date)
        else:
            self.cancel_due(item_id)

    def schedule_reminder(self, item_id, dt):
        score = dt.timestamp()
        self._redis.zadd(KEY_REMINDER, {str(item_id): score})
        logger.debug("Scheduled reminder for item %s at %s", item_id, dt)

    def cancel_reminder(self, item_id):
        self._redis.zrem(KEY_REMINDER, str(item_id))

    def schedule_due(self, item_id, dt):
        score = dt.timestamp()
        self._redis.zadd(KEY_DUE, {str(item_id): score})
        logger.debug("Scheduled due timer for item %s at %s", item_id, dt)

    def cancel_due(self, item_id):
        self._redis.zrem(KEY_DUE, str(item_id))

    def schedule_recurring(self, item_id, dt):
        """Add or update a recurring item's next fire time."""
        score = dt.timestamp()
        self._redis.zadd(KEY_RECURRING, {str(item_id): score})
        logger.debug("Scheduled recurring fire for item %s at %s", item_id, dt)

    def cancel_recurring(self, item_id):
        self._redis.zrem(KEY_RECURRING, str(item_id))

    def schedule_evidence(self, item_id, dt):
        """Add or replace an evidence-blocker deadline."""
        self._redis.zadd(KEY_EVIDENCE, {str(item_id): dt.timestamp()})
        logger.debug("Scheduled evidence escalation for item %s at %s", item_id, dt)

    def cancel_evidence(self, item_id):
        self._redis.zrem(KEY_EVIDENCE, str(item_id))

    def cancel_all(self, item_id):
        pipe = self._redis.pipeline(transaction=False)
        pipe.zrem(KEY_REMINDER, str(item_id))
        pipe.zrem(KEY_DUE, str(item_id))
        pipe.zrem(KEY_RECURRING, str(item_id))
        pipe.zrem(KEY_EVIDENCE, str(item_id))
        pipe.execute()
        logger.debug("Cancelled all timers for item %s", item_id)

    def _init_recurring_due_date(self, item):
        """
        Ensure a recurring item's due_date (= its NEXT FIRE time) is sane.

        Two cases, one rule — due_date must be at-or-before ``now + 2*period``:
          - missing  → set it (fire immediately on the first tick)
          - far-future → clamp it (reset to fire soon)
        Anything within range is left untouched (no-op).

        Why clamp: the scheduler only fires items whose due_date <= now, and
        the advance logic resets due_date to the next period after each fire,
        so a healthy recurring task always sits within one period of now. A
        due_date many periods out means the cadence is broken — either legacy
        corruption (a historical advance-without-catch-up runaway) or a task
        created with a deadline-style due_date. Either way it would never fire
        until that far date; resetting to now restores the cadence. (2026-06-23:
        26 recurring tasks were frozen this way — daily QA due +19d, etc.)

        We use Item.objects.filter().update() to persist so we don't re-trigger
        the post_save signal that called us; the in-memory object is updated too.
        """
        from .services.recurring import parse_interval_seconds
        from .models import Item

        # Interval in seconds. Calendar frequencies have no fixed second-count;
        # approximate a period purely to bound how far the next fire may sit.
        interval = parse_interval_seconds(item.frequency, item.recurrence_pattern)
        if interval is None:
            if item.frequency == 'monthly':
                interval = 31 * 86400
            elif item.frequency == 'yearly':
                interval = 366 * 86400
            else:
                return  # uninterpretable → leave due_date None; caller skips

        now = timezone.now()
        max_ahead = now + timedelta(seconds=2 * interval)
        if item.due_date is not None and item.due_date <= max_ahead:
            return  # already within one cadence window — nothing to do

        was = item.due_date
        Item.objects.filter(pk=item.pk).update(due_date=now)
        item.due_date = now
        logger.info(
            "Recurring item %s: reset due_date %s → %s (frequency=%s) — "
            "due was missing or > 2 periods out; firing soon to restore cadence",
            item.pk, was.isoformat() if was else None, now.isoformat(),
            item.frequency,
        )


# Module-level singleton (lazy)
_manager = None


def get_timer_manager():
    global _manager
    if _manager is None:
        _manager = TimerManager()
    return _manager


class SchedulerLoop:
    """
    Main scheduler loop. Polls Redis sorted sets every 100ms,
    dispatches to Celery .delay() for immediate execution.
    """

    POLL_INTERVAL = 0.1       # 100ms
    BATCH_SIZE = 100          # max items per poll
    DEDUP_TTL = 3600          # 1 hour dedup window
    BOOTSTRAP_BATCH = 500     # items per DB query during bootstrap
    # A recurring delivery is late-acked by Celery, but Redis keeps an
    # interrupted delivery invisible until the broker visibility timeout
    # (currently one hour).  Arm a short timer lease before publishing so a
    # rolling pod termination cannot silence the recurrence for that hour.
    # A successful cycle save overwrites this with the natural next boundary;
    # a duplicate delivery is rejected by the revision/boundary CAS.
    RECURRING_DELIVERY_RETRY = 60.0
    # PLS-1416: how long a dispatch dedup lease outlives its fire when the
    # task exits abnormally without releasing it (lost task). Deliberately
    # the same order as the broker's visibility timeout — insurance should
    # recover within one lease, never multiply under it.
    RECURRING_DISPATCH_LEASE = 3600.0
    LIVENESS_THROTTLE = 5.0   # s — loop ticks every 100ms; one heartbeat every
                              # few seconds is ample for a staleness probe and
                              # avoids hammering Redis 10x/second (PLAT-1295)

    def __init__(self):
        self._redis = redis.Redis.from_url(
            settings.CELERY_BROKER_URL,
            decode_responses=True,
        )
        self._pop_script = self._redis.register_script(_LUA_ATOMIC_POP)
        self._pop_with_scores_script = self._redis.register_script(_LUA_ATOMIC_POP_WITH_SCORES)
        self._running = False
        self._last_liveness_ts = 0.0

    def start(self):
        """Bootstrap from DB then enter the polling loop."""
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        logger.info("Scheduler starting — bootstrapping timers from database")
        self.bootstrap_timers()
        logger.info("Bootstrap complete — entering polling loop (%.0fms interval)", self.POLL_INTERVAL * 1000)

        backoff = 0.1
        while self._running:
            try:
                self._poll_once()
                backoff = 0.1  # reset on success
            except redis.ConnectionError as e:
                logger.error("Redis connection error: %s (retry in %.1fs)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception:
                logger.exception("Unexpected error in scheduler loop")
                time.sleep(1)

            time.sleep(self.POLL_INTERVAL)

        logger.info("Scheduler stopped gracefully")

    def _handle_shutdown(self, signum, frame):
        logger.info("Received signal %s — shutting down scheduler", signum)
        self._running = False

    def _record_liveness(self):
        """Emit the recurring-engine liveness heartbeat (PLAT-1295), throttled.

        The loop ticking at all IS the signal: PLS-210 was the engine going
        silently dead while the pod stayed up, which a k8s-availability proxy
        cannot see. Best-effort and throttled; a write failure must never
        break the poll loop.
        """
        from .liveness import RECURRING_ANDON, record_heartbeat
        now = time.time()
        if now - self._last_liveness_ts < self.LIVENESS_THROTTLE:
            return
        self._last_liveness_ts = now
        record_heartbeat(RECURRING_ANDON, client=self._redis, now=now)

    def _poll_once(self):
        """Pop due items from all sorted sets and dispatch Celery tasks."""
        self._record_liveness()
        now_ts = str(time.time())

        # Pop reminders
        reminder_ids = self._pop_script(
            keys=[KEY_REMINDER], args=[now_ts, self.BATCH_SIZE]
        )
        for item_id in reminder_ids:
            if self._mark_processed('reminder', item_id):
                self._dispatch_reminder(item_id)

        # Pop due items
        due_ids = self._pop_script(
            keys=[KEY_DUE], args=[now_ts, self.BATCH_SIZE]
        )
        for item_id in due_ids:
            if self._mark_processed('due', item_id):
                self._dispatch_overdue(item_id)

        # Pop recurring items — each one fires the recurring cycle handler,
        # which advances due_date and (if currently terminal) re-activates
        # the task for the new period.
        #
        # Note: NO dedup here. Recurring items get re-added to KEY_RECURRING
        # by the post_save signal with a NEW score after fire_recurring_due
        # advances due_date. Each pop = one fire = one logical period. Two
        # scheduler instances can't double-pop the same entry because the
        # Lua _pop_script removes atomically. The 1-hour processed-key TTL
        # would WRONGLY suppress legitimate manual re-fires (e.g. during
        # tests, or if an admin advances due_date back to the past) so we
        # skip it for the recurring path.
        recurring_rows = self._pop_with_scores_script(
            keys=[KEY_RECURRING], args=[now_ts, self.BATCH_SIZE]
        )
        for offset in range(0, len(recurring_rows), 2):
            self._dispatch_recurring(recurring_rows[offset], recurring_rows[offset + 1])

        # Evidence timers carry their generation from the durable Item metadata
        # into Celery.  A due-date/evidence/link mutation can therefore replace
        # the timer while an older callback is queued without leaking a stale DM.
        evidence_ids = self._pop_script(
            keys=[KEY_EVIDENCE], args=[now_ts, self.BATCH_SIZE]
        )
        for item_id in evidence_ids:
            self._dispatch_evidence(item_id)

    def _mark_processed(self, timer_type, item_id):
        """
        Idempotency guard — returns True if this is the first time processing.
        Uses SET NX with TTL.
        """
        key = f"{KEY_PROCESSED_PREFIX}:{timer_type}:{item_id}"
        return bool(self._redis.set(key, '1', nx=True, ex=self.DEDUP_TTL))

    def _dispatch_reminder(self, item_id):
        from .tasks import send_reminder
        try:
            send_reminder.delay(int(item_id))
            logger.info("Dispatched send_reminder for item %s", item_id)
        except Exception:
            logger.exception("Failed to dispatch send_reminder for item %s", item_id)

    def _dispatch_overdue(self, item_id):
        from .tasks import send_overdue_notification
        try:
            send_overdue_notification.delay(int(item_id))
            logger.info("Dispatched send_overdue_notification for item %s", item_id)
        except Exception:
            logger.exception("Failed to dispatch send_overdue_notification for item %s", item_id)

    def _dispatch_recurring(self, item_id, popped_score=None):
        from .tasks import fire_recurring_due
        from .models import Item
        from .services.recurring_cycle_state import cycle_state
        try:
            item = Item.objects.only('id', 'metadata', 'due_date').get(pk=int(item_id), mode='task')
            _metadata, cycle = cycle_state(item)
            popped_score_value = (
                float(popped_score) if popped_score is not None else None
            )
            due_score = item.due_date.timestamp() if item.due_date else None
            # Delivery-retry timers intentionally fire later than the durable
            # DB boundary. Preserve that original boundary in the CAS. For a
            # genuinely stale older timer, min() preserves the older score and
            # therefore remains a no-op against the newer DB boundary.
            expected_score = min(
                score for score in (popped_score_value, due_score)
                if score is not None
            ) if (popped_score_value is not None or due_score is not None) else None
            expected_due = (
                datetime.fromtimestamp(
                    expected_score, tz=timezone.get_current_timezone(),
                ).isoformat()
                if expected_score is not None else None
            )
            retry_at = time.time() + self.RECURRING_DELIVERY_RETRY
            # PLS-1416: dedup lease — skip the publish when a previous fire
            # for this item is still in flight. The redelivery timer below
            # keeps the recurrence armed either way; without the lease a
            # queued-backlog timer popped every beat and re-enqueued a
            # duplicate that the worker would CAS-reject after paying the
            # full broker cost.
            dispatch_lease_key = f"{KEY_DISPATCH_LEASE_PREFIX}:{item_id}"
            lease_acquired = bool(
                self._redis.set(
                    dispatch_lease_key, repr(time.time()),
                    nx=True, ex=int(self.RECURRING_DISPATCH_LEASE),
                )
            )
            self._redis.zadd(KEY_RECURRING, {str(item_id): retry_at})
            if not lease_acquired:
                logger.info(
                    "Skipped duplicate fire dispatch item=%s (dispatch lease held; "
                    "redelivery timer re-armed at %s)",
                    item_id,
                    datetime.fromtimestamp(
                        retry_at, tz=timezone.get_current_timezone(),
                    ).isoformat(),
                )
                return
            try:
                fire_recurring_due.delay(
                    int(item_id),
                    expected_cycle_revision=cycle['cycle_revision'],
                    expected_due_at=expected_due,
                    expected_generation=cycle['active_generation'],
                    expected_completion_watermark=cycle.get('last_terminal_completion_at') or '',
                )
            except Exception:
                # Publish failed — release the lease so the next timer pop
                # re-dispatches instead of stranding the recurrence for the
                # whole lease window.
                try:
                    self._redis.delete(dispatch_lease_key)
                except Exception:
                    logger.exception(
                        "Failed to release dispatch lease for item %s after "
                        "publish failure", item_id,
                    )
                raise
            logger.info(
                "Dispatched fire_recurring_due item=%s revision=%s boundary=%s "
                "generation=%s delivery_retry_at=%s",
                item_id, cycle['cycle_revision'], expected_due,
                cycle['active_generation'],
                datetime.fromtimestamp(
                    retry_at, tz=timezone.get_current_timezone(),
                ).isoformat(),
            )
        except Item.DoesNotExist:
            logger.info("Recurring timer item %s disappeared before dispatch", item_id)
        except Exception:
            logger.exception("Failed to dispatch fire_recurring_due for item %s", item_id)

    def _dispatch_evidence(self, item_id):
        from .models import Item
        from .tasks import send_evidence_blocker_escalation

        try:
            item = Item.objects.only('id', 'metadata').get(pk=int(item_id), mode='task')
            state = (item.metadata or {}).get('evidence_escalation') or {}
            generation = state.get('generation')
            if state.get('state') != 'scheduled' or not generation:
                logger.info("Evidence timer %s no longer scheduled", item_id)
                return
            send_evidence_blocker_escalation.delay(int(item_id), generation)
            logger.info(
                "Dispatched evidence escalation item=%s generation=%s",
                item_id, generation,
            )
        except Item.DoesNotExist:
            logger.info("Evidence timer item %s disappeared before dispatch", item_id)
        except Exception:
            logger.exception("Failed to dispatch evidence escalation for item %s", item_id)

    def bootstrap_timers(self):
        """
        Repopulate Redis sorted sets from the database.
        Called on scheduler startup to recover from Redis restarts.
        """
        from .models import Item

        now = timezone.now()
        count = 0

        # Recurring items first. We iterate full objects (not pk/due_date
        # tuples) and route each through schedule_item so that (a) items
        # with due_date=None get their due_date auto-initialized, and
        # (b) the scheduling logic lives in one place. This is slower
        # than a pipelined ZADD batch, but recurring items are few and
        # bootstrap runs only on scheduler startup.
        recurring_manager = get_timer_manager()
        recurring_qs = Item.objects.filter(
            mode='task',
            is_recurring=True,
        ).filter(
            Q(assignee_id__isnull=False)
            | (Q(assignment_group__isnull=False) & ~Q(assignment_group=''))
            | (
                Q(metadata__recurring_owner_team__isnull=False)
                & ~Q(metadata__recurring_owner_team='')
            ),
        ).exclude(
            frequency='one_time',
        ).exclude(
            status__in=RECURRING_STOP_STATUSES,
        ).iterator(chunk_size=self.BOOTSTRAP_BATCH)

        recurring_pks = set()
        for item in recurring_qs:
            try:
                recurring_manager.schedule_item(item)
                recurring_pks.add(item.pk)
                count += 1
            except Exception:
                logger.exception(
                    "Failed to schedule recurring item %s during bootstrap",
                    item.pk,
                )

        pipe = self._redis.pipeline(transaction=False)

        # Reminders: active tasks with future reminder_time, not yet sent.
        # Skip any items already handled as recurring.
        reminder_qs = Item.objects.filter(
            mode='task',
            reminder_sent=False,
            reminder_time__isnull=False,
            assignee_id__isnull=False,
        ).exclude(
            status__in=TERMINAL_STATUSES,
        ).exclude(
            pk__in=recurring_pks,
        ).values_list('pk', 'reminder_time').iterator(chunk_size=self.BOOTSTRAP_BATCH)

        for pk, reminder_time in reminder_qs:
            pipe.zadd(KEY_REMINDER, {str(pk): reminder_time.timestamp()})
            count += 1
            if count % self.BOOTSTRAP_BATCH == 0:
                pipe.execute()
                pipe = self._redis.pipeline(transaction=False)

        # Due dates: active items with due_date, assigned, not yet notified.
        # Skip any items already handled as recurring.
        due_qs = Item.objects.filter(
            due_date__isnull=False,
            assignee_id__isnull=False,
            overdue_notified=False,
            status__in=['pending', 'in_progress'],
        ).exclude(
            pk__in=recurring_pks,
        ).values_list('pk', 'due_date').iterator(chunk_size=self.BOOTSTRAP_BATCH)

        for pk, due_date in due_qs:
            pipe.zadd(KEY_DUE, {str(pk): due_date.timestamp()})
            count += 1
            if count % self.BOOTSTRAP_BATCH == 0:
                pipe.execute()
                pipe = self._redis.pipeline(transaction=False)

        # PLS-463 recovery bootstrap.  This is not an age sweep: mutations have
        # already materialized the exact generation/fire time in the Item row;
        # startup only restores those durable scheduled entries after Redis loss.
        evidence_qs = Item.objects.filter(
            metadata__evidence_escalation__state='scheduled',
        ).values_list('pk', 'metadata').iterator(chunk_size=self.BOOTSTRAP_BATCH)
        evidence_count = 0
        for pk, metadata in evidence_qs:
            state = (metadata or {}).get('evidence_escalation') or {}
            try:
                fire_at = datetime.fromisoformat(state['fire_at'])
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed evidence timer metadata for item %s", pk)
                continue
            pipe.zadd(KEY_EVIDENCE, {str(pk): fire_at.timestamp()})
            evidence_count += 1
            count += 1
            if count % self.BOOTSTRAP_BATCH == 0:
                pipe.execute()
                pipe = self._redis.pipeline(transaction=False)

        pipe.execute()
        logger.info(
            "Bootstrapped %d timers from database (recurring=%d evidence=%d)",
            count, len(recurring_pks), evidence_count,
        )

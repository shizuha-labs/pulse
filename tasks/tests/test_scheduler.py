"""
Tests for the Redis sorted set timer scheduler.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from tasks.scheduler import (
    KEY_DUE,
    KEY_EVIDENCE,
    KEY_RECURRING,
    KEY_REMINDER,
    KEY_DISPATCH_LEASE_PREFIX,
    TERMINAL_STATUSES,
    TimerManager,
    SchedulerLoop,
)
from tasks.models import Item, ItemLink, Project
from tasks.services.evidence_blocker_escalation import (
    ESCALATION_STATE_KEY,
    reconcile_evidence_blocker,
)
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import send_evidence_blocker_escalation, sweep_missed_notifications
from tasks.liveness import STALE_SWEEP


class FakeRedis:
    """Minimal in-memory fake for redis.Redis used by TimerManager tests."""

    def __init__(self):
        self._sorted_sets = {}  # key -> {member: score}
        self._strings = {}

    def zadd(self, key, mapping):
        ss = self._sorted_sets.setdefault(key, {})
        ss.update(mapping)

    def zrem(self, key, *members):
        ss = self._sorted_sets.get(key, {})
        for m in members:
            ss.pop(m, None)

    def zscore(self, key, member):
        return self._sorted_sets.get(key, {}).get(member)

    def zcard(self, key):
        return len(self._sorted_sets.get(key, {}))

    def set(self, key, value):
        # PLAT-9452: the sweep stamps its liveness key via SET (string).
        self._strings[key] = value

    def expire(self, key, seconds):
        # Recorded so tests can assert the evidence window is bounded.
        self._expirations = getattr(self, '_expirations', {})
        self._expirations[key] = seconds

    def get(self, key):
        return self._strings.get(key)

    def zrangebyscore(self, key, min_score, max_score, start=0, num=-1):
        ss = self._sorted_sets.get(key, {})
        items = sorted(
            ((m, s) for m, s in ss.items() if s <= float(max_score)),
            key=lambda x: x[1],
        )
        if num > 0:
            items = items[start:start + num]
        return [m for m, s in items]

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._strings:
            return False
        self._strings[key] = value
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self._strings:
                self._strings.pop(key)
                removed += 1
        return removed

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    def register_script(self, script_text):
        """Return a callable that simulates the Lua atomic pop."""
        parent = self
        with_scores = 'WITHSCORES' in script_text

        def lua_pop(keys, args):
            key = keys[0]
            threshold = float(args[0])
            limit = int(args[1])
            items = parent.zrangebyscore(key, '-inf', threshold, num=limit)
            scores = [parent.zscore(key, item) for item in items]
            if items:
                parent.zrem(key, *items)
            if not with_scores:
                return items
            return [part for pair in zip(items, scores) for part in pair]

        return lua_pop


class FakePipeline:
    def __init__(self, fake_redis):
        self._redis = fake_redis
        self._ops = []

    def zadd(self, key, mapping):
        self._ops.append(('zadd', key, mapping))
        return self

    def zrem(self, key, *members):
        self._ops.append(('zrem', key, members))
        return self

    def execute(self):
        for op in self._ops:
            if op[0] == 'zadd':
                self._redis.zadd(op[1], op[2])
            elif op[0] == 'zrem':
                self._redis.zrem(op[1], *op[2])
        self._ops.clear()


def _make_item(**kwargs):
    """Create a mock Item with sensible defaults."""
    defaults = {
        'pk': 42,
        'mode': 'task',
        'status': 'pending',
        'reminder_time': timezone.now() + timedelta(hours=1),
        'reminder_sent': False,
        'assignee_id': 17,
        'due_date': timezone.now() + timedelta(days=1),
        # schedule_item now reads these; on a bare MagicMock they auto-vivify as
        # truthy, which would send every item down the recurring path (cancelling
        # the due timer) and make `not overdue_notified` false. Default them falsy.
        'is_recurring': False,
        'frequency': None,
        'assignment_group': '',
        'metadata': {},
        'overdue_notified': False,
    }
    defaults.update(kwargs)
    item = MagicMock()
    for k, v in defaults.items():
        setattr(item, k, v)
    return item


class TimerManagerTests(TestCase):
    """Unit tests for TimerManager."""

    def setUp(self):
        self.fake_redis = FakeRedis()
        self.tm = TimerManager(redis_client=self.fake_redis)

    def test_schedule_reminder(self):
        dt = timezone.now() + timedelta(hours=1)
        self.tm.schedule_reminder('42', dt)
        self.assertAlmostEqual(
            self.fake_redis.zscore(KEY_REMINDER, '42'),
            dt.timestamp(),
            places=2,
        )

    def test_cancel_reminder(self):
        dt = timezone.now() + timedelta(hours=1)
        self.tm.schedule_reminder('42', dt)
        self.tm.cancel_reminder('42')
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))

    def test_schedule_due(self):
        dt = timezone.now() + timedelta(days=1)
        self.tm.schedule_due('42', dt)
        self.assertAlmostEqual(
            self.fake_redis.zscore(KEY_DUE, '42'),
            dt.timestamp(),
            places=2,
        )

    def test_cancel_due(self):
        dt = timezone.now() + timedelta(days=1)
        self.tm.schedule_due('42', dt)
        self.tm.cancel_due('42')
        self.assertIsNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_cancel_all(self):
        dt = timezone.now() + timedelta(hours=1)
        self.tm.schedule_reminder('42', dt)
        self.tm.schedule_due('42', dt)
        self.tm.cancel_all('42')
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_schedule_item_active_task(self):
        """Active task with reminder + due date → both timers set."""
        item = _make_item()
        self.tm.schedule_item(item)
        self.assertIsNotNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNotNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_schedule_item_terminal_status(self):
        """Terminal status → all timers cancelled."""
        item = _make_item()
        self.tm.schedule_item(item)
        # Now complete it
        item.status = 'completed'
        self.tm.schedule_item(item)
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_schedule_item_reminder_already_sent(self):
        """reminder_sent=True → reminder timer cancelled, due timer kept."""
        item = _make_item(reminder_sent=True)
        self.tm.schedule_item(item)
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNotNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_schedule_item_no_assignee(self):
        """No assignee → no timers."""
        item = _make_item(assignee_id=None)
        self.tm.schedule_item(item)
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_team_owned_unassigned_recurring_item_keeps_live_timer(self):
        """CTX-478 shape: group queue is routing ownership, not timer death."""
        due = timezone.now() + timedelta(minutes=5)
        item = _make_item(
            is_recurring=True,
            frequency='daily',
            assignee_id=None,
            assignment_group='engineering',
            metadata={'recurring_owner_team': 'engineering'},
            status='scheduled',
            due_date=due,
        )

        self.tm.schedule_item(item)

        self.assertAlmostEqual(
            self.fake_redis.zscore(KEY_RECURRING, '42'),
            due.timestamp(), places=2,
        )
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_schedule_item_no_reminder_time(self):
        """No reminder_time → no reminder timer, due timer still set."""
        item = _make_item(reminder_time=None)
        self.tm.schedule_item(item)
        self.assertIsNone(self.fake_redis.zscore(KEY_REMINDER, '42'))
        self.assertIsNotNone(self.fake_redis.zscore(KEY_DUE, '42'))

    def test_schedule_item_reschedule_overwrites_score(self):
        """ZADD overwrites score — due date update replaces timer."""
        dt1 = timezone.now() + timedelta(days=1)
        dt2 = timezone.now() + timedelta(days=3)
        item = _make_item(due_date=dt1)
        self.tm.schedule_item(item)
        score1 = self.fake_redis.zscore(KEY_DUE, '42')

        item.due_date = dt2
        self.tm.schedule_item(item)
        score2 = self.fake_redis.zscore(KEY_DUE, '42')

        self.assertNotAlmostEqual(score1, score2, places=2)
        self.assertAlmostEqual(score2, dt2.timestamp(), places=2)

    def test_all_terminal_statuses_cancel(self):
        """Every terminal status should cancel all timers."""
        for status in TERMINAL_STATUSES:
            item = _make_item(status=status)
            self.tm.schedule_item(item)
            self.assertIsNone(
                self.fake_redis.zscore(KEY_REMINDER, '42'),
                f"Reminder timer not cancelled for status={status}",
            )
            self.assertIsNone(
                self.fake_redis.zscore(KEY_DUE, '42'),
                f"Due timer not cancelled for status={status}",
            )


class SweepMissedNotificationsRecurringTests(TestCase):
    """sweep_missed_notifications must reconcile pulse:timers:recurring.

    PLS-5322: recurring timers are armed only by scheduler startup bootstrap and
    post_save. A recurring item that drops off the recurring zset (post-save
    bypass, group-queue owner clear/restore, Redis restart without scheduler
    restart) stays silent until the next scheduler start. The 60s sweep is the
    durable self-heal for that PulseRecurringMissingTimer condition.
    """

    def setUp(self):
        self.fake_redis = FakeRedis()
        self.project = Project.objects.create(name='Sweep recurring', key='SW', org_id=1)

    def _recurring_item(self, **kwargs):
        defaults = dict(
            project=self.project,
            item_key='SW-1',
            title='recurring sweep fixture',
            mode='task',
            workflow_name='simple',
            status='scheduled',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            due_date=timezone.now() + timedelta(hours=6),
            metadata={'recurring_owner_team': 'engineering'},
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    @patch('redis.Redis.from_url')
    def test_sweep_re_arms_recurring_item_missing_from_zset(self, mock_from_url):
        """An active recurring item absent from pulse:timers:recurring is re-added."""
        mock_from_url.return_value = self.fake_redis
        item = self._recurring_item()
        manager = TimerManager(redis_client=self.fake_redis)

        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            result = sweep_missed_notifications.run()

        self.assertIn('Re-added 1 missed items', result)
        self.assertIsNotNone(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
        )

    @patch('redis.Redis.from_url')
    def test_sweep_does_not_duplicate_existing_recurring_timer(self, mock_from_url):
        """An already-armed recurring item is left untouched (no re-fire)."""
        mock_from_url.return_value = self.fake_redis
        item = self._recurring_item()
        self.fake_redis.zadd(
            KEY_RECURRING, {str(item.pk): item.due_date.timestamp()},
        )
        manager = TimerManager(redis_client=self.fake_redis)

        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            result = sweep_missed_notifications.run()

        self.assertEqual(result, 'No missed notifications found')
        # Score unchanged — the sweep must not move an armed timer.
        self.assertEqual(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
            item.due_date.timestamp(),
        )

    @patch('redis.Redis.from_url')
    def test_sweep_skips_terminal_recurring_item(self, mock_from_url):
        """Cancelled/expired recurring items are NOT re-armed (stop status)."""
        mock_from_url.return_value = self.fake_redis
        # bulk_create bypasses workflow-status healing so the sweep's stop-status
        # filter is tested directly against terminal data (same pattern as the
        # recurring-invariants detector test).
        Item.objects.bulk_create([Item(
            project=self.project,
            item_key='SW-TERM',
            title='terminal recurring fixture',
            mode='task',
            workflow_name='simple',
            status='cancelled',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            due_date=timezone.now() + timedelta(hours=6),
            metadata={'recurring_owner_team': 'engineering'},
        )])
        manager = TimerManager(redis_client=self.fake_redis)

        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            sweep_missed_notifications.run()

        self.assertEqual(self.fake_redis.zcard(KEY_RECURRING), 0)

    @patch('redis.Redis.from_url')
    def test_sweep_re_arms_future_due_item_at_its_due_date_not_now(self, mock_from_url):
        """PLAT-9452 addendum 3 (kai's #20465 class): a future-due group-queued
        item absent from the zset is re-armed with score = its due_date, NOT
        now — arming at now would fire the cycle early and silently consume
        the boundary (the Cortex/DeepSeek health guard must not skip or
        early-fire its cycle)."""
        mock_from_url.return_value = self.fake_redis
        item = self._recurring_item()  # due_date = now + 6h (future)
        manager = TimerManager(redis_client=self.fake_redis)

        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            sweep_missed_notifications.run()

        score = self.fake_redis.zscore(KEY_RECURRING, str(item.pk))
        self.assertIsNotNone(score)
        self.assertEqual(
            score, item.due_date.timestamp(),
            'future-due item must be re-armed at its due_date, not at now',
        )

    @patch('redis.Redis.from_url')
    def test_sweep_stamps_liveness_key_with_bounded_window(self, mock_from_url):
        """PLAT-9452 defect 3: every completed sweep stamps
        pulse:sweep:last_success_ts (now, TTL 3600) so the liveness gauge can
        distinguish 'beat firing but healer no-op' from 'beat dead'."""
        mock_from_url.return_value = self.fake_redis
        self._recurring_item()
        manager = TimerManager(redis_client=self.fake_redis)

        before = timezone.now().timestamp()
        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            sweep_missed_notifications.run()

        stamped = self.fake_redis.get('pulse:sweep:last_success_ts')
        self.assertIsNotNone(stamped, 'sweep must stamp its liveness key')
        self.assertGreaterEqual(float(stamped), before - 1)
        self.assertEqual(
            self.fake_redis._expirations.get('pulse:sweep:last_success_ts'),
            3600,
            'the liveness stamp must be bounded (key must not outlive the evidence window)',
        )

    @patch('redis.Redis.from_url')
    def test_sweep_skips_rejected_recurring_item(self, mock_from_url):
        """A rejected recurring item is NOT re-armed (PLAT-6289).

        The fire handler (fire_due_cycle) treats `rejected` as a permanent stop
        and will never re-fire it, so re-arming it would pop→stop→re-arm in a
        loop and keep the PulseRecurringMissingTimer alert firing. The sweep
        must leave intentionally-stopped items out of the recurring zset.
        """
        mock_from_url.return_value = self.fake_redis
        Item.objects.bulk_create([Item(
            project=self.project,
            item_key='SW-REJ',
            title='rejected recurring fixture',
            mode='task',
            workflow_name='simple',
            status='rejected',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            due_date=timezone.now() + timedelta(hours=6),
            metadata={'recurring_owner_team': 'engineering'},
        )])
        manager = TimerManager(redis_client=self.fake_redis)

        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            result = sweep_missed_notifications.run()

        self.assertEqual(result, 'No missed notifications found')
        self.assertEqual(self.fake_redis.zcard(KEY_RECURRING), 0)


class SchedulerLoopTests(TestCase):
    """Unit tests for SchedulerLoop dispatch logic."""

    def setUp(self):
        self.fake_redis = FakeRedis()

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_bootstrap_restores_group_queued_recurring_timer(self, mock_from_url):
        """A Redis restart cannot strand a CTX-478-shaped team recurrence."""
        mock_from_url.return_value = self.fake_redis
        project = Project.objects.create(name='Scheduler test', key='SCH', org_id=1)
        due = timezone.now() + timedelta(minutes=5)
        item = Item.objects.create(
            project=project,
            item_key='SCH-CTX-478',
            title='Recurring Cortex TTFT guard',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            due_date=due,
            metadata={'recurring_owner_team': 'engineering'},
        )
        loop = SchedulerLoop()
        manager = TimerManager(redis_client=self.fake_redis)

        with patch('tasks.scheduler.get_timer_manager', return_value=manager):
            loop.bootstrap_timers()

        self.assertAlmostEqual(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
            due.timestamp(), places=2,
        )

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_poll_dispatches_reminders(self, mock_from_url):
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()

        # Add an item that's past due
        past_ts = (timezone.now() - timedelta(minutes=5)).timestamp()
        self.fake_redis.zadd(KEY_REMINDER, {'99': past_ts})

        with patch('tasks.scheduler.SchedulerLoop._dispatch_reminder') as mock_dispatch:
            loop._poll_once()
            mock_dispatch.assert_called_once_with('99')

        # Item should be removed from sorted set
        self.assertEqual(self.fake_redis.zcard(KEY_REMINDER), 0)

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_poll_dispatches_overdue(self, mock_from_url):
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()

        past_ts = (timezone.now() - timedelta(minutes=5)).timestamp()
        self.fake_redis.zadd(KEY_DUE, {'77': past_ts})

        with patch('tasks.scheduler.SchedulerLoop._dispatch_overdue') as mock_dispatch:
            loop._poll_once()
            mock_dispatch.assert_called_once_with('77')

        self.assertEqual(self.fake_redis.zcard(KEY_DUE), 0)

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_poll_carries_popped_recurring_boundary_to_dispatch(self, mock_from_url):
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()
        past_ts = (timezone.now() - timedelta(minutes=5)).timestamp()
        self.fake_redis.zadd(KEY_RECURRING, {'55': past_ts})

        with patch('tasks.scheduler.SchedulerLoop._dispatch_recurring') as dispatch:
            loop._poll_once()
            dispatch.assert_called_once_with('55', past_ts)

        self.assertEqual(self.fake_redis.zcard(KEY_RECURRING), 0)

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_recurring_dispatch_arms_retry_lease_before_publish(self, mock_from_url):
        """A rollout-interrupted Celery delivery remains timer-backed."""
        mock_from_url.return_value = self.fake_redis
        project = Project.objects.create(name='Delivery lease', key='LEASE', org_id=1)
        due = timezone.now() - timedelta(minutes=2)
        item = Item.objects.create(
            project=project,
            item_key='LEASE-1',
            title='Recurring delivery lease',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='engineering',
            is_recurring=True,
            frequency='daily',
            due_date=due,
        )
        loop = SchedulerLoop()
        before = timezone.now().timestamp()

        with patch('tasks.tasks.fire_recurring_due.delay') as delay:
            loop._dispatch_recurring(str(item.pk), due.timestamp())

        retry_score = self.fake_redis.zscore(KEY_RECURRING, str(item.pk))
        self.assertGreaterEqual(
            retry_score, before + loop.RECURRING_DELIVERY_RETRY - 1,
        )
        self.assertAlmostEqual(
            datetime.fromisoformat(
                delay.call_args.kwargs['expected_due_at'],
            ).timestamp(),
            due.timestamp(), places=3,
        )

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_recurring_delivery_retry_preserves_original_db_boundary(self, mock_from_url):
        """A retry lease cannot turn a lost delivery into a newer cycle."""
        mock_from_url.return_value = self.fake_redis
        project = Project.objects.create(name='Delivery retry', key='RETRY', org_id=1)
        due = timezone.now() - timedelta(minutes=2)
        item = Item.objects.create(
            project=project,
            item_key='RETRY-1',
            title='Recurring delivery retry',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='engineering',
            is_recurring=True,
            frequency='daily',
            due_date=due,
        )
        loop = SchedulerLoop()
        delivery_retry_score = timezone.now().timestamp()

        with patch('tasks.tasks.fire_recurring_due.delay') as delay:
            loop._dispatch_recurring(str(item.pk), delivery_retry_score)

        self.assertAlmostEqual(
            datetime.fromisoformat(
                delay.call_args.kwargs['expected_due_at'],
            ).timestamp(),
            due.timestamp(), places=3,
        )

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_recurring_publish_failure_keeps_retry_lease(self, mock_from_url):
        """Broker publish failure must not consume the durable timer."""
        mock_from_url.return_value = self.fake_redis
        project = Project.objects.create(name='Publish retry', key='PUB', org_id=1)
        due = timezone.now() - timedelta(minutes=2)
        item = Item.objects.create(
            project=project,
            item_key='PUB-1',
            title='Recurring publish retry',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='engineering',
            is_recurring=True,
            frequency='daily',
            due_date=due,
        )
        loop = SchedulerLoop()

        with patch(
            'tasks.tasks.fire_recurring_due.delay',
            side_effect=RuntimeError('broker unavailable'),
        ):
            loop._dispatch_recurring(str(item.pk), due.timestamp())

        self.assertIsNotNone(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
        )

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_dedup_prevents_double_dispatch(self, mock_from_url):
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()

        past_ts = (timezone.now() - timedelta(minutes=5)).timestamp()
        self.fake_redis.zadd(KEY_REMINDER, {'99': past_ts})

        with patch('tasks.scheduler.SchedulerLoop._dispatch_reminder') as mock_dispatch:
            loop._poll_once()
            mock_dispatch.assert_called_once()

        # Re-add the same item (simulate bootstrap race)
        self.fake_redis.zadd(KEY_REMINDER, {'99': past_ts})
        with patch('tasks.scheduler.SchedulerLoop._dispatch_reminder') as mock_dispatch:
            loop._poll_once()
            mock_dispatch.assert_not_called()  # dedup blocks it

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_future_items_not_popped(self, mock_from_url):
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()

        future_ts = (timezone.now() + timedelta(hours=1)).timestamp()
        self.fake_redis.zadd(KEY_REMINDER, {'100': future_ts})

        with patch('tasks.scheduler.SchedulerLoop._dispatch_reminder') as mock_dispatch:
            loop._poll_once()
            mock_dispatch.assert_not_called()

        # Item should still be in sorted set
        self.assertEqual(self.fake_redis.zcard(KEY_REMINDER), 1)

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_poll_dispatches_evidence_deadline(self, mock_from_url):
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()
        past_ts = (timezone.now() - timedelta(minutes=1)).timestamp()
        self.fake_redis.zadd(KEY_EVIDENCE, {'101': past_ts})

        with patch('tasks.scheduler.SchedulerLoop._dispatch_evidence') as dispatch:
            loop._poll_once()
            dispatch.assert_called_once_with('101')

        self.assertEqual(self.fake_redis.zcard(KEY_EVIDENCE), 0)


class EvidenceBlockerDeadlineTests(TestCase):
    """PLS-463 mutation-derived evidence deadline lifecycle."""

    OWNER = 'evidence.owner@shizuha.com'

    def setUp(self):
        self.fake_redis = FakeRedis()
        self.tm = TimerManager(redis_client=self.fake_redis)
        self.parent = Item.objects.create(
            title='Accounting close waiting on bank statement',
            mode='task', workflow_name='simple', status='open', priority='urgent',
            metadata={'pre_block_state': {
                'status': 'open', 'workflow_status': 'open',
                'had_active_blocker': True,
            }},
        )
        # PLAT-4215: an Item can't be CREATED directly in `blocked` (a durable
        # parent row must exist first). Create the parent in its working status,
        # then move it to blocked — this row is the pre-existing parent the
        # blocker links to. `.update()` sets the end-state without re-running the
        # save-time entry validation (the block transition itself isn't under test).
        Item.objects.filter(pk=self.parent.pk).update(status='blocked')
        self.parent.status = 'blocked'
        self.blocker = Item.objects.create(
            title='Supply June bank statement',
            mode='task', workflow_name='simple', status='open', priority='urgent',
            assignee_id=78, assignee_email=self.OWNER,
            due_date=timezone.now() + timedelta(hours=4),
            tags='blocker,evidence-request',
            metadata={'evidence_request': {
                'owner_email': self.OWNER,
                'required_evidence': 'June bank statement PDF from the bank portal',
            }},
        )
        self.link = ItemLink.objects.create(
            source_item=self.blocker, target_item=self.parent, link_type='blocks',
            created_by_id=1, created_by_email='kai@shizuha.com',
        )

    def _reconcile(self):
        with patch('tasks.scheduler.get_timer_manager', return_value=self.tm), \
             patch(
                 'tasks.services.evidence_blocker_escalation.transaction.on_commit',
                 side_effect=lambda callback: callback(),
             ):
            return reconcile_evidence_blocker(self.blocker.pk)

    def test_create_and_link_register_one_current_generation(self):
        self.assertEqual(self._reconcile(), 'scheduled')
        self.blocker.refresh_from_db()
        state = self.blocker.metadata[ESCALATION_STATE_KEY]
        self.assertEqual(state['state'], 'scheduled')
        self.assertEqual(state['owner_email'], self.OWNER)
        self.assertEqual([row['id'] for row in state['parents']], [self.parent.pk])
        self.assertIsNotNone(
            self.fake_redis.zscore(KEY_EVIDENCE, str(self.blocker.pk))
        )

    def test_due_date_change_replaces_generation_and_timer_score(self):
        self._reconcile()
        self.blocker.refresh_from_db()
        old_generation = self.blocker.metadata[ESCALATION_STATE_KEY]['generation']
        old_score = self.fake_redis.zscore(KEY_EVIDENCE, str(self.blocker.pk))

        self.blocker.due_date = timezone.now() + timedelta(days=3)
        self.blocker.save(update_fields=['due_date', 'updated_at'])
        self.assertEqual(self._reconcile(), 'scheduled')
        self.blocker.refresh_from_db()
        state = self.blocker.metadata[ESCALATION_STATE_KEY]
        self.assertNotEqual(state['generation'], old_generation)
        self.assertNotEqual(
            self.fake_redis.zscore(KEY_EVIDENCE, str(self.blocker.pk)), old_score,
        )

    @patch('requests.post')
    @patch('tasks.user_service.user_service.get_user_by_email')
    def test_delivery_is_deduplicated_per_generation(self, get_user, post):
        get_user.side_effect = lambda email: {
            'id': 78 if email == self.OWNER else 24,
            'email': email,
        }
        post.return_value.raise_for_status.return_value = None
        self._reconcile()
        self.blocker.refresh_from_db()
        generation = self.blocker.metadata[ESCALATION_STATE_KEY]['generation']

        first = send_evidence_blocker_escalation.run(self.blocker.pk, generation)
        second = send_evidence_blocker_escalation.run(self.blocker.pk, generation)

        self.assertIn('Sent evidence escalation', first)
        self.assertIn('Stale evidence escalation skipped', second)
        self.assertEqual(post.call_count, 2)  # owner + CoS, once each
        self.blocker.refresh_from_db()
        state = self.blocker.metadata[ESCALATION_STATE_KEY]
        self.assertEqual(state['state'], 'sent')
        self.assertEqual(
            sorted(state['delivered_recipient_emails']),
            sorted([self.OWNER, 'shizuha@shizuha.com']),
        )

    @patch.object(WorkflowService, '_resolve_unblock_assignee', return_value=(None, None))
    def test_terminal_blocker_clears_trigger_and_parent_unblocks(self, _resolve):
        self._reconcile()
        self.blocker.status = 'completed'
        self.blocker.workflow_status = 'completed'
        self.blocker.save(update_fields=['status', 'workflow_status', 'updated_at'])

        resumed = WorkflowService._auto_unblock_tasks_blocked_by(self.blocker)
        self.assertEqual(resumed, [str(self.parent.pk)])
        self.assertEqual(self._reconcile(), 'cleared')
        self.parent.refresh_from_db()
        self.blocker.refresh_from_db()
        self.assertNotEqual(self.parent.status, 'blocked')
        self.assertNotIn(ESCALATION_STATE_KEY, self.blocker.metadata)
        self.assertIsNone(
            self.fake_redis.zscore(KEY_EVIDENCE, str(self.blocker.pk))
        )


class RecurringDispatchDedupTests(TestCase):
    """PLS-1416: per-item dispatch lease for fire_recurring_due enqueues.

    The redelivery timer re-arms every RECURRING_DELIVERY_RETRY (60s); with a
    broker backlog a timer pop raced the previous fire and re-published a
    duplicate each beat (~25k enqueued/day vs ~750 popped). The lease keeps
    at most one delivery in flight per item and is released by the task
    itself when it exits, so a completed fire re-arms immediately instead of
    waiting out the lease TTL.
    """

    def setUp(self):
        self.fake_redis = FakeRedis()
        self.project = Project.objects.create(
            name='Dispatch dedup', key='DDED', org_id=1,
        )
        self.item = Item.objects.create(
            project=self.project,
            item_key='DDED-1',
            title='Recurring dedup fixture',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            due_date=timezone.now() - timedelta(minutes=2),
            metadata={'recurring_owner_team': 'engineering'},
        )

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_dispatch_lease_blocks_duplicate_publish(self, mock_from_url):
        """A timer pop while the previous fire is still queued re-arms only."""
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()
        boundary = timezone.now().timestamp()
        item_id = str(self.item.pk)

        with patch('tasks.tasks.fire_recurring_due.delay') as delay:
            loop._dispatch_recurring(item_id, boundary)
            loop._dispatch_recurring(item_id, boundary)

        self.assertEqual(delay.call_count, 1)
        # The redelivery timer must stay armed even though nothing published.
        self.assertIsNotNone(self.fake_redis.zscore(KEY_RECURRING, item_id))

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_lease_release_allows_next_publish(self, mock_from_url):
        """After the fire exits and releases the lease, the next pop publishes."""
        mock_from_url.return_value = self.fake_redis
        loop = SchedulerLoop()
        boundary = timezone.now().timestamp()
        item_id = str(self.item.pk)

        with patch('tasks.tasks.fire_recurring_due.delay') as delay:
            loop._dispatch_recurring(item_id, boundary)
            # Simulate the task's exit-side release.
            self.fake_redis.delete(
                f'{KEY_DISPATCH_LEASE_PREFIX}:{item_id}',
            )
            loop._dispatch_recurring(item_id, boundary)

        self.assertEqual(delay.call_count, 2)

    @patch('tasks.scheduler.redis.Redis.from_url')
    def test_dispatch_lease_survives_across_instances(self, mock_from_url):
        """A second scheduler instance cannot bypass the item's lease."""
        mock_from_url.return_value = self.fake_redis
        first = SchedulerLoop()
        second = SchedulerLoop()
        boundary = timezone.now().timestamp()
        item_id = str(self.item.pk)

        with patch('tasks.tasks.fire_recurring_due.delay') as delay:
            first._dispatch_recurring(item_id, boundary)
            second._dispatch_recurring(item_id, boundary)

        self.assertEqual(delay.call_count, 1)

    @patch('tasks.liveness.record_heartbeat')
    def test_sweep_stamps_mechanism_liveness(self, mock_record):
        """A fresh DB sweep stamp also records the liveness heartbeat."""
        mock_record.return_value = 1785708666.242
        result = sweep_missed_notifications.run()

        self.assertIsNotNone(result)
        mock_record.assert_called_once_with(STALE_SWEEP)

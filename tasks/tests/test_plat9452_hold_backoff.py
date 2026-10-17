"""PLAT-9452 defect 2: a recurrence-routing HOLD terminates the retry chain.

Root cause (09-20 incident): the hold branch returned without re-arming the
timer, while the hold's item.save() re-armed it via the post_save signal at
the still-past due_date — the popped boundary was instantly re-queued and
held fires spun through the retry path (~3/sec observed live). The fix re-arms
at a bounded backoff (ROUTING_HOLD_RETRY_BACKOFF_SECONDS = 300s) so held items
stay timer-covered and re-evaluate at most once per window.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project
from tasks.scheduler import KEY_RECURRING, TimerManager
from tasks.services.recurring_cycle_state import CycleResult
from tasks.tasks import ROUTING_HOLD_RETRY_BACKOFF_SECONDS, fire_recurring_due


class RoutingHoldBackoffReArmTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Hold backoff', key='P9452', org_id=1)

    def _held_item(self, **kwargs):
        defaults = dict(
            project=self.project,
            item_key='P9452-1',
            title='held recurring fixture',
            mode='task',
            workflow_name='simple',
            status='scheduled',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            # Past-due boundary: the spin class requires the popped score to be
            # in the past at fire time.
            due_date=timezone.now() - timedelta(minutes=5),
            metadata={'recurring_owner_team': 'engineering'},
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_hold_re_arms_at_bounded_backoff_not_past_boundary(self):
        """A held fire re-arms the timer at now+backoff, never at the past due."""
        item = self._held_item()
        hold = CycleResult(
            item=item, applied=False, reason='no_live_member',
            kind='routing_hold', old_status=item.status,
            new_status=item.status, next_due=item.due_date, revision=1,
        )
        manager = mock.MagicMock()

        with mock.patch('tasks.services.recurring_cycle_state.fire_due_cycle', return_value=hold), \
             mock.patch('tasks.scheduler.get_timer_manager', return_value=manager):
            result = fire_recurring_due(str(item.pk))

        self.assertIn('Held recurring', result)
        # Exactly one re-arm, at a future backoff boundary — not the past-due
        # boundary whose instantaneous re-pop produced the ~3/sec spin.
        self.assertEqual(
            manager.schedule_recurring.call_count, 1,
            manager.schedule_recurring.call_args,
        )
        _, armed_at = manager.schedule_recurring.call_args[0]
        expected_floor = timezone.now() + timedelta(
            seconds=ROUTING_HOLD_RETRY_BACKOFF_SECONDS - 5,
        )
        self.assertGreater(
            armed_at, expected_floor,
            f"held item re-armed at {armed_at}, expected >= now+backoff-5s",
        )

    def test_hold_re_arm_failure_does_not_raise_out_of_the_fire(self):
        """A Redis failure during the backoff re-arm is logged, not raised."""
        item = self._held_item()
        hold = CycleResult(
            item=item, applied=False, reason='no_live_member',
            kind='routing_hold', old_status=item.status,
            new_status=item.status, next_due=item.due_date, revision=1,
        )
        manager = mock.MagicMock()
        manager.schedule_recurring.side_effect = RuntimeError('redis down')

        with mock.patch('tasks.services.recurring_cycle_state.fire_due_cycle', return_value=hold), \
             mock.patch('tasks.scheduler.get_timer_manager', return_value=manager):
            result = fire_recurring_due(str(item.pk))

        # The hold outcome still returns cleanly — the fire must not crash the
        # scheduler loop because the re-arm failed; the DB-driven sweep is the
        # backstop that re-covers the item within one beat tick.
        self.assertIn('Held recurring', result)

    def test_hold_backoff_constant_is_bounded(self):
        """The backoff window stays bounded (no hot-loop, no indefinite silence)."""
        self.assertGreaterEqual(ROUTING_HOLD_RETRY_BACKOFF_SECONDS, 60)
        self.assertLessEqual(ROUTING_HOLD_RETRY_BACKOFF_SECONDS, 900)

    def test_held_item_stays_timer_covered_after_backoff_re_arm(self):
        """End-to-end: after a held fire the item is present in the zset with a
        future score — the missing-timer invariant reads 0 for held items."""
        item = self._held_item()
        hold = CycleResult(
            item=item, applied=False, reason='no_live_member',
            kind='routing_hold', old_status=item.status,
            new_status=item.status, next_due=item.due_date, revision=1,
        )

        class _FakeRedis:
            def __init__(self):
                self._zsets = {}

            def zadd(self, key, mapping):
                self._zsets.setdefault(key, {}).update(mapping)

            def zscore(self, key, member):
                return self._zsets.get(key, {}).get(member)

        fake = _FakeRedis()
        manager = TimerManager(redis_client=fake)

        with mock.patch('tasks.services.recurring_cycle_state.fire_due_cycle', return_value=hold), \
             mock.patch('tasks.scheduler.get_timer_manager', return_value=manager):
            fire_recurring_due(str(item.pk))

        score = fake.zscore(KEY_RECURRING, str(item.pk))
        self.assertIsNotNone(score, 'held item must stay timer-covered after the fire')
        self.assertGreater(
            score,
            (timezone.now() - timedelta(minutes=1)).timestamp(),
            'armed score must be the future backoff boundary, not the past due',
        )

    def test_stale_fire_re_arms_past_due_item_at_backoff_not_past_boundary(self):
        """Defect-2 stale half (sara 827637): the stale branch re-adds at
        max(due_date, now+backoff) — never a past score. The confirmed ~3/sec
        loop was hold-save → next fire CAS-mismatch → stale re-add at the
        still-past due_date → ~100ms tick pops instantly → dispatch → repeat;
        the hold-half backoff alone never holds the boundary because the stale
        outcome re-adds past it first."""
        item = self._held_item()  # due_date 5 minutes in the past
        stale = CycleResult(
            item=item, applied=False, reason='revision_mismatch',
            kind='stale', old_status=item.status,
            new_status=item.status, next_due=item.due_date, revision=1,
        )
        manager = mock.MagicMock()

        with mock.patch('tasks.services.recurring_cycle_state.fire_due_cycle', return_value=stale), \
             mock.patch('tasks.scheduler.get_timer_manager', return_value=manager):
            result = fire_recurring_due(str(item.pk))

        self.assertIn('stale', result)
        self.assertEqual(
            manager.schedule_recurring.call_count, 1,
            manager.schedule_recurring.call_args,
        )
        _, armed_at = manager.schedule_recurring.call_args[0]
        expected_floor = timezone.now() + timedelta(
            seconds=ROUTING_HOLD_RETRY_BACKOFF_SECONDS - 5,
        )
        self.assertGreater(
            armed_at, expected_floor,
            f"stale item re-armed at {armed_at}, expected >= now+backoff-5s — "
            "a past score is popped by the next ~100ms tick and re-enters the spin",
        )

    def test_stale_fire_keeps_future_due_date_when_it_exceeds_the_backoff(self):
        """The clamp is max(due_date, now+backoff): a legitimately future-due
        item re-firing stale keeps its own boundary — the backoff never
        postpones a scheduled future fire."""
        future_due = timezone.now() + timedelta(hours=2)
        item = self._held_item(due_date=future_due)
        stale = CycleResult(
            item=item, applied=False, reason='revision_mismatch',
            kind='stale', old_status=item.status,
            new_status=item.status, next_due=future_due, revision=1,
        )
        manager = mock.MagicMock()

        with mock.patch('tasks.services.recurring_cycle_state.fire_due_cycle', return_value=stale), \
             mock.patch('tasks.scheduler.get_timer_manager', return_value=manager):
            fire_recurring_due(str(item.pk))

        self.assertEqual(manager.schedule_recurring.call_count, 1)
        _, armed_at = manager.schedule_recurring.call_args[0]
        self.assertGreaterEqual(
            armed_at, future_due,
            'a future-due stale re-fire must keep its own boundary, not the backoff',
        )

"""Tests for the reconcile_recurring_timers management command.

The command is the manual/on-demand counterpart to PLS-980's automatic 60s
sweep: it re-arms active recurring items that are absent from
``pulse:timers:recurring``. These tests exercise the DB query
(``_active_recurring_items``) against a fake Redis so no live broker is needed.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tasks.models import Item
from tasks.scheduler import KEY_RECURRING


class _FakeRedis:
    """Minimal in-memory fake for the Redis zset used by the command."""

    def __init__(self):
        self.zsets = {}

    def zscore(self, key, member):
        return self.zsets.get(key, {}).get(str(member))

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(
            {str(m): s for m, s in mapping.items()}
        )


class _FakeTimerManager:
    """TimerManager stand-in whose schedule_recurring writes to _FakeRedis."""

    def __init__(self, redis_client):
        self._redis = redis_client

    def schedule_recurring(self, item_id, score):
        self._redis.zadd(KEY_RECURRING, {str(item_id): score})


class ReconcileRecurringTimersCommandTests(TestCase):
    def setUp(self):
        self.fake_redis = _FakeRedis()
        self.tm_patch = patch(
            'tasks.management.commands.reconcile_recurring_timers.get_timer_manager',
            return_value=_FakeTimerManager(self.fake_redis),
        )
        self.tm_patch.start()
        self.addCleanup(self.tm_patch.stop)
        self.now = timezone.now()

    def _recurring_item(self, title, **overrides):
        data = {
            'title': title,
            'mode': 'task',
            'status': 'in_progress',
            'is_recurring': True,
            'frequency': 'daily',
            'assignee_id': 101,
            'assignee_email': 'agent@example.com',
            'due_date': self.now + timedelta(hours=2),
        }
        data.update(overrides)
        return Item.objects.create(**data)

    def test_rearms_missing_recurring_timer(self):
        """An active recurring item absent from the zset is re-armed."""
        item = self._recurring_item('missing timer recurring')
        self.assertIsNone(self.fake_redis.zscore(KEY_RECURRING, str(item.pk)))

        out = StringIO()
        call_command('reconcile_recurring_timers', stdout=out)

        self.assertIsNotNone(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
            'missing recurring item should be re-armed',
        )
        self.assertIn('Re-armed 1 recurring timer', out.getvalue())

    def test_rearms_group_queued_recurring_timer(self):
        """CTX-478 shape: a team-owned (group-queued) recurring item is re-armed."""
        item = self._recurring_item(
            'group queued missing timer',
            assignee_id=None,
            assignee_email='',
            assignment_group='engineering',
            metadata={'recurring_owner_team': 'engineering'},
        )
        self.assertIsNone(self.fake_redis.zscore(KEY_RECURRING, str(item.pk)))

        out = StringIO()
        call_command('reconcile_recurring_timers', stdout=out)

        self.assertIsNotNone(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
            'group-queued recurring item should be re-armed',
        )
        self.assertIn('Re-armed 1 recurring timer', out.getvalue())

    def test_dry_run_does_not_rearm(self):
        """--dry-run reports the missing timer but leaves the zset untouched."""
        item = self._recurring_item('dry run recurring')
        self.assertIsNone(self.fake_redis.zscore(KEY_RECURRING, str(item.pk)))

        out = StringIO()
        call_command('reconcile_recurring_timers', '--dry-run', stdout=out)

        self.assertIsNone(
            self.fake_redis.zscore(KEY_RECURRING, str(item.pk)),
            'dry-run must not re-arm timers',
        )
        self.assertIn('DRY RUN', out.getvalue())
        self.assertIn('Found 1 active recurring item', out.getvalue())

    def test_terminal_recurring_item_not_rearmed(self):
        """A cancelled (terminal) recurring item is not in the active set."""
        # bulk_create bypasses workflow-status healing so the stop status is
        # preserved (the invariant's RECURRING_STOP_STATUSES filter is what we
        # are testing, exactly like test_plat1255_recurring_invariants).
        Item.objects.bulk_create([Item(
            title='terminal recurring',
            mode='task',
            workflow_name='simple',
            status='cancelled',
            is_recurring=True,
            frequency='daily',
            assignee_id=101,
            assignee_email='agent@example.com',
            due_date=self.now + timedelta(hours=2),
        )])

        out = StringIO()
        call_command('reconcile_recurring_timers', stdout=out)

        self.assertIn('No active recurring items are missing', out.getvalue())
        self.assertEqual(self.fake_redis.zsets.get(KEY_RECURRING, {}), {})

    def test_rejected_recurring_item_not_rearmed(self):
        """A rejected recurring item is skipped (cycle STOP, not REFIRE).

        _active_recurring_items() excludes only {cancelled, expired, deferred}
        (scheduler.RECURRING_STOP_STATUSES), so a `rejected` item IS in the
        active set. The command must still skip it: the fire path's cycle state
        machine classifies rejected as STOP and would immediately cancel a
        re-armed timer — re-creating the exact drift this command fixes.
        """
        # bulk_create bypasses workflow-status healing so `rejected` survives
        # (the command's STOP_STATUSES skip is what we are testing).
        Item.objects.bulk_create([Item(
            title='rejected recurring',
            mode='task',
            workflow_name='simple',
            status='rejected',
            is_recurring=True,
            frequency='daily',
            assignee_id=101,
            assignee_email='agent@example.com',
            due_date=self.now + timedelta(hours=2),
        )])

        out = StringIO()
        call_command('reconcile_recurring_timers', stdout=out)

        self.assertIn('No active recurring items are missing', out.getvalue())
        self.assertEqual(self.fake_redis.zsets.get(KEY_RECURRING, {}), {})

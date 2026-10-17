"""
PLS-207: a stale duplicate fire must NOT advance a recurring item's due_date.

Root cause: with >1 pulse replica, every pod ran its own scheduler loop. A
second scheduler's startup bootstrap re-added recurring items to
pulse:timers:recurring with the PRE-fire score while the first fire was
advancing due_date. The duplicate pop then called fire_recurring_due against
an item whose due_date was already one period in the FUTURE, and
calculate_next_future_due_date happily advanced it ANOTHER period — silently
skipping a whole recurring cycle (observed live 2026-07-02: weekly items at
due = now + 14d).

Fix (two layers):
  - fire_recurring_due skips (no advance, no status reset) when it observes
    due_date > now + 5min — a legitimate fire always sees due_date <= now.
  - leader_exec makes beat + scheduler cluster singletons (topology fix).
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, TeamRoster
from tasks.tasks import fire_recurring_due

User = get_user_model()


class StaleRecurringFireTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='rin', email='rin@shizuha.com', password='x',
        )
        self.project = Project.objects.create(
            name='Stale recurrence test', key='P207', org_id=1,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={
                'organization_id': 1,
                'tiers': [[self.user.email]],
                'is_active': True,
            },
        )

    def _recurring(self, **kw):
        kw.setdefault('status', 'done')
        # Recurrence route validation is project/org scoped, so keep the
        # concrete assignee identity canonical as the database guard requires.
        return Item.objects.create(
            project=self.project,
            title='weekly sweep', mode='task',
            workflow_name='autonomous-dev',
            assignment_group='engineering',
            assignee_id=self.user.id,
            assignee_email=self.user.email,
            is_recurring=True, frequency='weekly',
            **kw,
        )

    def test_stale_fire_with_future_due_is_skipped(self):
        """due_date already a period out → duplicate fire: no advance, no reopen."""
        future_due = timezone.now() + timedelta(days=7)
        item = self._recurring(due_date=future_due)

        result = fire_recurring_due(item.pk)

        item.refresh_from_db()
        self.assertIn('stale fire', result)
        self.assertEqual(item.due_date, future_due)   # NOT advanced another period
        self.assertEqual(item.status, 'done')         # NOT reopened early

    def test_stale_fire_reasserts_timer_entry(self):
        """The pop removed the redis entry; the skip path must re-add it."""
        future_due = timezone.now() + timedelta(days=7)
        item = self._recurring(due_date=future_due)

        with mock.patch('tasks.scheduler.TimerManager.schedule_recurring') as sched:
            fire_recurring_due(item.pk)
        sched.assert_called_once()
        self.assertEqual(sched.call_args[0][0], str(item.pk))

    def test_legitimate_due_fire_still_advances_and_reopens(self):
        """Normal fire (due in the past) keeps the original behavior."""
        item = self._recurring(due_date=timezone.now() - timedelta(minutes=1))

        with mock.patch('requests.post'):
            result = fire_recurring_due(item.pk)

        item.refresh_from_db()
        self.assertIn('Fired recurring', result)
        self.assertGreater(item.due_date, timezone.now())  # advanced to next period
        self.assertNotEqual(item.status, 'done')           # reopened

    def test_slightly_future_due_within_grace_still_fires(self):
        """Sub-5-minute clock skew must not suppress a legitimate fire."""
        item = self._recurring(due_date=timezone.now() + timedelta(minutes=2))

        with mock.patch('requests.post'):
            result = fire_recurring_due(item.pk)

        item.refresh_from_db()
        self.assertIn('Fired recurring', result)
        self.assertNotEqual(item.status, 'done')

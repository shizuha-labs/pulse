"""PLS-438 regression coverage for future-due stale recurring events."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from tasks.models import Item, ItemActivity, Project, TeamRoster
from tasks.services.recurring_cycle_state import (
    complete_cycle,
    cycle_snapshot,
    fire_due_cycle,
)
from tasks.tasks import fire_recurring_due


User = get_user_model()


@override_settings(SHIZUHA_CONNECT_API_URL='http://connect.test/api')
class RecurringStaleDetectorFutureDueTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='aoi', email='aoi@shizuha.com', password='x',
        )
        self.project = Project.objects.create(
            name='Recurring stale detector test',
            key='P438',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            team_name='architecture', organization_slug='shizuha',
            defaults={
                'organization_id': 1,
                'tiers': [[self.user.email]],
                'is_active': True,
            },
        )

    def _recurring(self, **kwargs):
        kwargs.setdefault('status', 'open')
        kwargs.setdefault('due_date', timezone.now() - timedelta(minutes=1))
        kwargs.setdefault('assignment_group', 'architecture')
        return Item.objects.create(
            project=self.project,
            title='8h executive cycle',
            mode='task',
            workflow_name='autonomous-dev',
            assignee_id=self.user.id,
            assignee_email=self.user.email,
            is_recurring=True,
            frequency='daily',
            **kwargs,
        )

    def test_completion_resets_stale_metadata_and_preserves_future_boundary(self):
        future_due = timezone.now() + timedelta(hours=8)
        item = self._recurring(
            due_date=future_due,
            metadata={'recurring_cycle': {
                'cycle_revision': 3,
                'active_generation': 1,
                'consecutive_skips': 6,
                'escalated_at_skips': 6,
                'last_terminal_completion_at': '2026-07-09T09:37:13+00:00',
            }},
        )

        result = complete_cycle(
            item.pk, actor_id=self.user.id, actor_email=self.user.email,
        )

        self.assertTrue(result.applied)
        item.refresh_from_db()
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.due_date, future_due)
        self.assertEqual(cycle['consecutive_skips'], 0)
        self.assertEqual(cycle['escalated_at_skips'], 0)
        self.assertNotEqual(
            cycle['last_terminal_completion_at'],
            '2026-07-09T09:37:13+00:00',
        )
        self.assertEqual(cycle['cycle_revision'], 4)

    def test_future_due_is_a_noop_even_with_six_stale_skips(self):
        future_due = timezone.now() + timedelta(hours=8)
        item = self._recurring(
            status='todo',
            due_date=future_due,
            metadata={'recurring_cycle': {
                'cycle_revision': 3,
                'active_generation': 1,
                'consecutive_skips': 6,
                'escalated_at_skips': 3,
                'last_terminal_completion_at': '2026-07-09T09:37:13+00:00',
            }},
        )
        expected = cycle_snapshot(item)
        before_activity_count = ItemActivity.objects.filter(item=item).count()

        result = fire_due_cycle(
            item.pk,
            expected_cycle_revision=expected['cycle_revision'],
            expected_generation=expected['generation'],
            expected_due_at=expected['due_boundary'],
            expected_completion_watermark=expected['completion_watermark'],
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, 'stale_future_due')
        item.refresh_from_db()
        self.assertEqual(item.status, 'todo')
        self.assertEqual(item.assignee_email, self.user.email)
        self.assertEqual(item.due_date, future_due)
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 6)
        self.assertEqual(
            ItemActivity.objects.filter(item=item).count(), before_activity_count,
        )

    def test_pre_completion_event_is_an_audited_noop_after_completion(self):
        item = self._recurring(
            metadata={'recurring_cycle': {
                'cycle_revision': 3,
                'active_generation': 1,
                'consecutive_skips': 6,
                'escalated_at_skips': 3,
                'last_terminal_completion_at': '2026-07-09T09:37:13+00:00',
            }},
        )
        stale = cycle_snapshot(item)
        self.assertTrue(complete_cycle(item.pk).applied)
        item.refresh_from_db()
        parked_due = item.due_date
        parked_revision = item.metadata['recurring_cycle']['cycle_revision']
        before_activity_count = ItemActivity.objects.filter(item=item).count()

        result = fire_recurring_due(
            item.pk,
            expected_cycle_revision=stale['cycle_revision'],
            expected_generation=stale['generation'],
            expected_due_at=stale['due_boundary'],
            expected_completion_watermark=stale['completion_watermark'],
        )

        self.assertIn('stale fire', result)
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.assignee_email, self.user.email)
        self.assertEqual(item.due_date, parked_due)
        self.assertEqual(
            item.metadata['recurring_cycle']['cycle_revision'], parked_revision,
        )
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 0)
        self.assertEqual(
            ItemActivity.objects.filter(item=item).count(), before_activity_count,
        )

    def test_next_real_boundary_fires_exactly_once(self):
        item = self._recurring()
        self.assertTrue(complete_cycle(item.pk).applied)
        item.refresh_from_db()
        expected = cycle_snapshot(item)
        at_boundary = item.due_date + timedelta(minutes=1)

        with mock.patch(
            'tasks.services.recurring_cycle_state.timezone.now',
            return_value=at_boundary,
        ), mock.patch('requests.post'):
            first = fire_recurring_due(
                item.pk,
                expected_cycle_revision=expected['cycle_revision'],
                expected_generation=expected['generation'],
                expected_due_at=expected['due_boundary'],
                expected_completion_watermark=expected['completion_watermark'],
            )
            duplicate = fire_recurring_due(
                item.pk,
                expected_cycle_revision=expected['cycle_revision'],
                expected_generation=expected['generation'],
                expected_due_at=expected['due_boundary'],
                expected_completion_watermark=expected['completion_watermark'],
            )

        self.assertIn('Fired recurring', first)
        self.assertIn('stale fire', duplicate)
        item.refresh_from_db()
        self.assertNotEqual(item.status, 'scheduled')
        self.assertEqual(item.metadata['recurring_cycle']['active_generation'], 1)
        self.assertEqual(
            ItemActivity.objects.filter(
                item=item, action='recurring_cycle_fired',
            ).count(),
            1,
        )

"""PLS-792 regression: recurring Run Now exposes a readable cycle revision.

Covers the acceptance contract: a client can read the current cycle revision,
see it echoed in stale errors, and clean up a park-in-scheduled task by turning
recurrence off. Mirrors the run-now API + transition tests in
test_reviewed_task_workflow and the recurring-cycle fixtures in
test_plat1353_recurring_cycle_close.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from tasks.models import Item, Project, Status, TeamRoster, WorkflowTransition
from tasks.serializers import ItemSerializer, RecurringTaskSerializer
from tasks.services.workflow_service import WorkflowService


class _BaseRecurringRunNow(TestCase):
    def setUp(self):
        Status.objects.get_or_create(
            slug='scheduled',
            defaults={
                'name': 'Scheduled',
                'category': 'scheduled',
                'color': '#a855f7',
                'position': 30,
                'is_system': True,
            },
        )
        self.user = get_user_model().objects.create_user(
            username='pls792-owner', email='pls792-owner@shizuha.com', password='x',
        )
        self.user.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(
            key='PLS792', name='PLS-792 test project', org_id=1,
        )
        # Org-membership / assignee authorization: put the fixture user in an
        # active TeamRoster of org 1 (slug 'shizuha') so the direct-assignment
        # org guard in Item.save resolves them as a same-org member.
        TeamRoster.objects.update_or_create(
            team_name='review',
            organization_slug='shizuha',
            organization_id=1,
            defaults={'tiers': [[self.user.email]], 'is_active': True},
        )
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        super().tearDown()

    def _recurring_item(self, **kw):
        kw.setdefault('title', 'pls792 weekly fixture')
        kw.setdefault('mode', 'task')
        kw.setdefault('status', 'scheduled')
        kw.setdefault('workflow_name', 'reviewed-task')
        kw.setdefault('workflow_status', 'Scheduled')
        kw.setdefault('project', self.project)
        kw.setdefault('is_recurring', True)
        kw.setdefault('frequency', 'weekly')
        kw.setdefault('due_date', timezone.now() + timedelta(days=7))
        kw.setdefault('assignee_id', self.user.id)
        kw.setdefault('assignee_email', self.user.email)
        kw.setdefault('metadata', {'recurring_cycle': {
            'cycle_revision': 9,
            'active_generation': 2,
            'completed_generation': 2,
        }})
        return Item.objects.create(**kw)

    def _run_now_transition(self):
        return WorkflowTransition.objects.get(
            workflow__slug='reviewed-task', from_status='scheduled', name='Run Now',
        )


class Pls792ReadSurfaceTests(_BaseRecurringRunNow):
    def test_item_detail_serializer_exposes_recurring_cycle_with_cycle_revision(self):
        item = self._recurring_item()
        data = ItemSerializer(item).data
        self.assertIn('recurring_cycle', data)
        cycle = data['recurring_cycle']
        self.assertIsInstance(cycle, dict)
        self.assertEqual(cycle['cycle_revision'], 9)
        self.assertEqual(cycle['generation'], 2)
        self.assertTrue(cycle['event_id'])

    def test_item_detail_serializer_recurring_cycle_is_none_for_non_recurring(self):
        item = Item.objects.create(
            title='pls792 one-time', mode='task',
            workflow_name='autonomous-bug', status='open',
            workflow_status='Open', is_recurring=False,
        )
        self.assertIsNone(ItemSerializer(item).data['recurring_cycle'])

    def test_recurring_task_serializer_exposes_cycle_revision(self):
        item = self._recurring_item()
        data = RecurringTaskSerializer(item).data
        self.assertEqual(data['recurring_cycle']['cycle_revision'], 9)

    def test_read_api_endpoint_exposes_recurring_cycle(self):
        """Live endpoint (not just serializer): GET item returns recurring_cycle."""
        item = self._recurring_item()
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get(f'/api/items/{item.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['recurring_cycle']['cycle_revision'], 9)


class Pls792StaleErrorTests(_BaseRecurringRunNow):
    def _stale_item(self):
        """Create a scheduled recurring item whose stored cycle revision is
        ahead of the value a client would have cached (simulating a newer
        owner completing the cycle between the client's read and its Run Now).
        """
        item = self._recurring_item()
        metadata = dict(item.metadata or {})
        cycle = dict(metadata.get('recurring_cycle') or {})
        cycle['cycle_revision'] = 10  # newer owner advanced the cycle
        metadata['recurring_cycle'] = cycle
        item.metadata = metadata
        item.save(update_fields=['metadata', 'updated_at'])
        return item

    def test_run_now_stale_error_returns_current_recurring_cycle(self):
        item = self._stale_item()
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            f'/api/items/{item.pk}/run-now/',
            {'expected_cycle_revision': 9},  # stale — stored revision is 10
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'stale_recurring_cycle')
        current = response.data.get('current_recurring_cycle')
        self.assertIsNotNone(current)
        self.assertEqual(current['cycle_revision'], 10)
        # The stale error body tells the client the exact value to refresh to.
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')

    def test_transition_stale_error_includes_current_cycle(self):
        item = self._stale_item()
        run_now = self._run_now_transition()
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            f'/api/items/{item.pk}/execute-transition/',
            {
                'to_status': 'todo',
                'transition_id': str(run_now.id),
                'screen_data': {'expected_cycle_revision': 9},
            },
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'stale_recurring_cycle')
        self.assertEqual(response.data['current_recurring_cycle']['cycle_revision'], 10)

    def test_run_now_with_refreshed_revision_succeeds(self):
        """A client that reads the current revision and echoes it completes the cycle."""
        item = self._recurring_item()
        client = APIClient()
        client.force_authenticate(user=self.user)
        read = client.get(f'/api/items/{item.pk}/')
        cycle = read.data['recurring_cycle']
        response = client.post(
            f'/api/items/{item.pk}/run-now/',
            {
                'expected_cycle_revision': cycle['cycle_revision'],
                'expected_generation': cycle['generation'],
                'expected_due_at': cycle['due_boundary'],
                'recurring_event_id': cycle['event_id'],
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, 'todo')
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], 10)


class Pls792TransitionMetadataTests(_BaseRecurringRunNow):
    def test_run_now_transition_declares_expected_cycle_revision_screen_field(self):
        """available transitions metadata advertises the required CAS field."""
        item = self._recurring_item()
        # Ensure the scheduled-gap state exists for the workflow (creates the
        # Run Now transition with its schema-backed metadata).
        WorkflowService._park_recurring_scheduled(item, user=self.user)
        run_now = self._run_now_transition()
        keys = {f.get('key') for f in (run_now.screen_fields or [])}
        self.assertIn('expected_cycle_revision', keys)
        self.assertIn('expected_generation', keys)
        self.assertIn('expected_due_at', keys)
        self.assertIn('recurring_event_id', keys)
        # The required flag is declared so MCP / REST adapters can expose it.
        rev = next((f for f in (run_now.screen_fields or []) if f.get('key') == 'expected_cycle_revision'), None)
        self.assertTrue(rev.get('required'))


class Pls792DisableCleanupTests(_BaseRecurringRunNow):
    def test_disabling_recurrence_while_scheduled_restores_initial_status(self):
        item = self._recurring_item()
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.patch(
            f'/api/items/{item.pk}/',
            {'is_recurring': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertNotEqual(str(item.status).lower(), 'scheduled')
        self.assertEqual(item.status, 'todo')  # workflow initial — actionable again
        self.assertIsNone(item.due_date)  # parked boundary cleared

    def test_disable_cleanup_is_scoped_to_scheduled_parked_items(self):
        """Non-scheduled recurring item is untouched by the cleanup path."""
        item = Item.objects.create(
            title='pls792 not parked', mode='task',
            workflow_name='reviewed-task', status='todo',
            workflow_status='Todo', is_recurring=True, frequency='weekly',
            project=self.project,
            due_date=timezone.now() + timedelta(days=3),
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.patch(
            f'/api/items/{item.pk}/',
            {'is_recurring': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, 'todo')
        self.assertIsNotNone(item.due_date)  # not cleared — not a parked scheduled gap

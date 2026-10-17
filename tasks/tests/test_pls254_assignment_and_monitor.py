from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item, Project, TeamRoster


@override_settings(ROOT_URLCONF='taskmanager.urls')
class PLS254CreateAssignmentContractTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        User = get_user_model()
        self.actor = User.objects.create_user(
            username='codex', email='codex@shizuha.com', password='x', is_staff=True,
        )
        self.client.force_authenticate(user=self.actor)
        self.project = Project.objects.create(
            name='Platform', key='P254', org_id=1, access_level='open',
            default_assignee_id=self.actor.id,
            default_assignee_email=self.actor.email,
        )

    @mock.patch('tasks.tasks.send_item_notification.delay', return_value=None)
    @mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(None, None))
    def test_group_routed_create_with_no_assignee_does_not_fall_back_to_creator_or_project_default(self, *_):
        response = self.client.post('/api/items/', {
            'project': self.project.id,
            'title': 'security-ci rolled-up finding',
            'mode': 'task',
            'item_type': 'task.security',
            'workflow_name': 'security-finding',
            'status': 'accepted',
            'assignment_group': 'security',
            'source': 'security-ci',
            'source_id': 'security-ci:test',
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        item = Item.objects.get(id=response.data['id'])
        self.assertEqual(item.assignment_group, 'security')
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)


class PLS254QueueDepthAnomalyTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.project = Project.objects.create(name='Platform', key='Q254', org_id=1, access_level='open')
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='security',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['codex@shizuha.com']],
                'is_active': True,
            },
        )

    @override_settings(
        PULSE_OPS_ASSIGNEE_ACTIVE_ITEM_THRESHOLD=1,
        PULSE_OPS_SOURCE_ACTIVE_24H_THRESHOLD=999,
        PULSE_OPS_GROUP_24H_JUMP_MIN=999,
    )
    def test_assignee_active_item_depth_creates_fail_loud_alert_for_any_status(self):
        Item.objects.create(
            mode='task', title='first accepted finding', status='accepted',
            workflow_name='security-finding', project=self.project,
            assignment_group='security', assignee_id=4, assignee_email='codex@shizuha.com',
        )
        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Item.objects.create(
                    mode='task', title='second accepted finding', status='accepted',
                    workflow_name='security-finding', project=self.project,
                    assignment_group='security', assignee_id=4, assignee_email='codex@shizuha.com',
                )

        alert = Item.objects.get(mode='alert', source='pulse-ops-anomaly')
        self.assertEqual(alert.labels['rule'], 'assignee_active_item_depth')
        self.assertEqual(alert.labels['assignee_id'], '4')
        self.assertEqual(alert.payload['active_count'], 2)
        notify_delay.assert_called_once_with(alert.id)

    @override_settings(
        PULSE_OPS_ASSIGNEE_ACTIVE_ITEM_THRESHOLD=1,
        PULSE_OPS_SOURCE_ACTIVE_24H_THRESHOLD=999,
        PULSE_OPS_GROUP_24H_JUMP_MIN=999,
    )
    def test_assignee_depth_ignores_scheduled_and_backlog_waiting_holds(self):
        """Parking statuses must not inflate queue-depth Attention.

        Mika-class piles (dozens of blocked/scheduled Focused-QA waiting holds)
        are not misroute floods; security-ci floods still show as accepted/found.
        (Uses scheduled/backlog — not blocked — to avoid PLAT-4215 blocker-link
        DB constraints that require a real owned blocker edge.)
        """
        for title, status in (
            ('scheduled hold', 'scheduled'),
            ('backlog hold', 'backlog'),
        ):
            item = Item.objects.create(
                mode='task', title=title, status='todo',
                workflow_name='simple', project=self.project,
                assignment_group='qa', assignee_id=4,
                assignee_email='mika@agents.shizuha.io',
            )
            Item.objects.filter(pk=item.pk).update(status=status)
        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Item.objects.create(
                    mode='task', title='only one actionable', status='todo',
                    workflow_name='reviewed-task', project=self.project,
                    assignment_group='qa', assignee_id=4,
                    assignee_email='mika@agents.shizuha.io',
                )

        self.assertFalse(
            Item.objects.filter(mode='alert', source='pulse-ops-anomaly').exists(),
            'waiting holds alone must not fire assignee-depth',
        )
        notify_delay.assert_not_called()

    @override_settings(
        PULSE_OPS_ASSIGNEE_ACTIVE_ITEM_THRESHOLD=999,
        PULSE_OPS_SOURCE_ACTIVE_24H_THRESHOLD=2,
        PULSE_OPS_GROUP_24H_JUMP_MIN=999,
    )
    def test_source_24h_active_flood_creates_alert(self):
        for idx in range(2):
            Item.objects.create(
                mode='task', title=f'finding {idx}', status='found',
                workflow_name='security-finding', project=self.project,
                assignment_group='security', source='security-ci', source_id=f's{idx}',
            )

        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Item.objects.create(
                    mode='task', title='finding 3', status='found',
                    workflow_name='security-finding', project=self.project,
                    assignment_group='security', source='security-ci', source_id='s3',
                )

        alert = Item.objects.get(mode='alert', source='pulse-ops-anomaly')
        self.assertEqual(alert.labels['rule'], 'source_active_24h_flood')
        self.assertEqual(alert.labels['source'], 'security-ci')
        self.assertEqual(alert.payload['active_count_24h'], 3)
        notify_delay.assert_called_once_with(alert.id)

    @override_settings(
        PULSE_OPS_ASSIGNEE_ACTIVE_ITEM_THRESHOLD=999,
        PULSE_OPS_SOURCE_ACTIVE_24H_THRESHOLD=2,
        PULSE_OPS_GROUP_24H_JUMP_MIN=999,
    )
    def test_manual_source_is_not_a_flood(self):
        for idx in range(2):
            Item.objects.create(
                mode='task', title=f'human {idx}', status='todo',
                workflow_name='simple', project=self.project,
                assignment_group='engineering', source='manual',
            )
        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Item.objects.create(
                    mode='task', title='human 2', status='todo',
                    workflow_name='simple', project=self.project,
                    assignment_group='engineering', source='manual',
                )
        self.assertFalse(
            Item.objects.filter(
                mode='alert', source='pulse-ops-anomaly',
                labels__rule='source_active_24h_flood',
            ).exists()
        )
        notify_delay.assert_not_called()

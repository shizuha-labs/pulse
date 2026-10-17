from unittest import mock

from django.test import TestCase

from tasks.models import Item, ItemLink, Project, ProjectMembership, Notification, TeamRoster
from tasks.tasks import _unassigned_alert_recipient_ids, send_alert_notification
from tasks.services.notifications import notification_service
from tasks.services.workflow_service import WorkflowService


class AlertNotificationRecipientTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Platform',
            key='PLAT',
            org_id=1,
            lead_id=11,
            default_assignee_id=22,
        )

    def make_alert(self, **kwargs):
        data = {
            'mode': 'alert',
            'item_type': 'alert.warning',
            'title': 'Alertmanager test',
            'description': 'Synthetic alert',
            'source': 'alertmanager',
            'status': 'firing',
            'severity': 'warning',
            'priority': 'high',
            'project': self.project,
            'assignment_group': 'DevOps',
        }
        data.update(kwargs)
        return Item.objects.create(**data)

    def make_assigned_task(self, **kwargs):
        data = {
            'mode': 'task',
            'item_type': 'task',
            'title': 'Assigned task',
            'status': 'in_progress',
            'priority': 'urgent',
            'workflow_name': 'autonomous-bug',
            'project': self.project,
            'assignee_id': 101,
            'assignee_email': 'kai@shizuha.com',
            'assignment_group': 'engineering',
        }
        data.update(kwargs)
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            return Item.objects.create(**data)

    def test_unassigned_group_alert_notifies_resolved_team_recipient_only(self):
        alert = self.make_alert(assignee_id=None)
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(77, 'devops-lead@shizuha.com'),
        ):
            self.assertEqual(_unassigned_alert_recipient_ids(alert), [77])

    def test_unassigned_project_alert_falls_back_to_project_owners(self):
        alert = self.make_alert(assignment_group=None, assignee_id=None)
        ProjectMembership.objects.create(
            project=self.project,
            user_id=33,
            user_email='admin@example.com',
            role='admin',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=44,
            user_email='member@example.com',
            role='member',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=55,
            user_email='stale-admin@example.com',
            role='admin',
        )

        with mock.patch(
            'tasks.services.admin_service.admin_service.get_org_members',
            return_value=[
                {'user_id': 33, 'user_email': 'admin@example.com'},
                {'user_id': 44, 'user_email': 'member@example.com'},
            ],
        ), mock.patch(
            'tasks.user_service.user_service.get_agent_identity_index',
            return_value={'user_ids': set(), 'emails': set()},
        ):
            self.assertEqual(_unassigned_alert_recipient_ids(alert), [22, 11, 33])

    def test_send_alert_notification_passes_fallback_recipients_to_service(self):
        alert = self.make_alert(assignee_id=None)
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(77, 'devops-lead@shizuha.com'),
        ), mock.patch.object(
            notification_service,
            'notify_alert',
            return_value={'success': 0, 'failed': 0, 'channels': [], 'connect_success': 0},
        ) as notify:
            result = send_alert_notification.run(alert.id)

        self.assertIn('Sent notification for alert', result)
        notify.assert_called_once_with(alert, user_ids=[77])

    def test_notify_alert_creates_notification_per_explicit_recipient(self):
        alert = self.make_alert(assignee_id=None)
        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            result = notification_service.notify_alert(alert, user_ids=[101, 102, 101])

        self.assertEqual(result['recipients'], [101, 102])
        self.assertEqual(result['connect_success'], 2)
        self.assertEqual(
            sorted(Notification.objects.filter(item=alert).values_list('user_id', flat=True)),
            [101, 102],
        )

    def test_notify_alert_connect_message_contains_actionable_context(self):
        alert = self.make_alert(
            assignee_id=None,
            labels={
                'alertname': 'ShizuhaServiceDown',
                'namespace': 'monitoring',
                'service': 'pulse',
            },
            source_url='http://prometheus.example/graph',
            payload={'starts_at': '2026-06-18T10:00:00Z', 'annotations': {'summary': 'Pulse probe failed'}},
        )

        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            notification_service.notify_alert(alert, user_ids=[101])

        connect_post.assert_called_once()
        payload = connect_post.call_args.kwargs['json']
        self.assertEqual(payload['recipient_user_id'], 101)
        self.assertEqual(payload['sender_service'], 'shizuha-pulse')
        self.assertIn('[Alert] #', payload['content'])
        self.assertIn('Team: DevOps', payload['content'])
        self.assertIn('Call pulse_get_my_work (alerts + tasks in one snapshot)', payload['content'])
        self.assertIn('Labels: alertname=ShizuhaServiceDown, namespace=monitoring, service=pulse', payload['content'])
        self.assertIn('Runbook: deploy/k3s/monitoring/agent-alert-remediation-runbook.md', payload['content'])
        self.assertIn('Do not perform hardware, data-loss, or destructive actions automatically.', payload['content'])

    def test_lower_priority_alert_does_not_wake_agent_holding_urgent_task(self):
        alert = self.make_alert(assignee_id=None, priority='high')
        self.make_assigned_task(
            title='Urgent blocker root',
            priority='urgent',
        )

        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            result = notification_service.notify_alert(alert, user_ids=[101])

        connect_post.assert_not_called()
        self.assertEqual(result['connect_success'], 0)
        self.assertEqual(result['connect_deferred'], 1)
        self.assertTrue(Notification.objects.filter(item=alert, user_id=101).exists())

    def test_blocker_inherits_descendant_priority_for_alert_wake_arbitration(self):
        alert = self.make_alert(assignee_id=None, priority='high')
        blocker = self.make_assigned_task(
            title='Normal root that releases urgent work',
            priority='normal',
        )
        blocked = Item.objects.create(
            mode='task',
            item_type='task',
            title='Urgent blocked descendant',
            status='in_progress',
            priority='urgent',
            workflow_name='autonomous-bug',
            project=self.project,
            assignment_group='engineering',
        )
        ItemLink.objects.create(
            source_item=blocker,
            target_item=blocked,
            link_type='blocks',
            created_by_id=101,
        )
        Item.objects.filter(pk=blocked.pk).update(status='blocked')
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, 'blocked')

        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            result = notification_service.notify_alert(alert, user_ids=[101])

        connect_post.assert_not_called()
        self.assertEqual(result['connect_deferred'], 1)

    def test_equal_priority_alert_still_wins_tie_and_wakes_agent(self):
        alert = self.make_alert(assignee_id=None, priority='urgent')
        self.make_assigned_task(
            title='Urgent task',
            priority='urgent',
        )

        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            result = notification_service.notify_alert(alert, user_ids=[101])

        connect_post.assert_called_once()
        self.assertEqual(result['connect_success'], 1)
        self.assertEqual(result['connect_deferred'], 0)

    def test_notify_alert_suppresses_delegates_that_are_direct_recipients(self):
        alert = self.make_alert(assignee_id=None)

        def delegates_for(user_id, event_type=''):
            if user_id == 101:
                return [{'id': 102, 'email': 'direct-two@shizuha.com'}]
            return []

        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch.object(notification_service, '_get_delegates', side_effect=delegates_for), \
             mock.patch.object(notification_service, '_deliver_to_agent'), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            result = notification_service.notify_alert(alert, user_ids=[101, 102])

        self.assertEqual(result['recipients'], [101, 102])
        notifications = Notification.objects.filter(item=alert).order_by('user_id')
        self.assertEqual(list(notifications.values_list('user_id', flat=True)), [101, 102])
        self.assertFalse(notifications.filter(delegated_from_id__isnull=False).exists())

    def test_mixed_case_devops_team_resolves_from_team_roster(self):
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='devops',
            defaults={'tiers': [['ichi@shizuha.com']], 'is_active': True},
        )
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            return_value={'id': 77, 'email': 'ichi@shizuha.com'},
        ):
            self.assertEqual(WorkflowService._resolve_team_assignee('DevOps'), (77, 'ichi@shizuha.com'))

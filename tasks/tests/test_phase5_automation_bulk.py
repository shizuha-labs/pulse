"""
Exhaustive E2E tests for Phase 5: Automation Engine, Bulk Operations, Project Configuration.
"""
import json
from django.utils import timezone
from rest_framework import status as http_status
from tasks.models import (
    AutomationRule, AutomationRuleLog,
    Project, Item,
    IssueTypeScheme, ScreenScheme, IssueTypeScreenScheme,
    SecurityScheme, PermissionScheme, NotificationScheme,
    ProjectConfiguration,
)
from .base import AuthenticatedAPITestCase


class AutomationRuleAPITests(AuthenticatedAPITestCase):
    """CRUD + toggle + log tests for AutomationRule API."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Auto Project', key='AUTO', org_id=1
        )

    def test_create_issue_created_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Auto-assign bugs',
            'trigger_type': 'issue_created',
            'trigger_config': {},
            'conditions': [
                {'type': 'jql_match', 'jql': 'type = Bug'},
            ],
            'actions': [
                {'type': 'assign', 'target': 'project_lead'},
                {'type': 'comment', 'body': 'Auto-assigned by rule: {{rule.name}}'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['trigger_type'], 'issue_created')
        self.assertEqual(len(resp.json()['actions']), 2)

    def test_create_status_changed_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Notify on Done',
            'trigger_type': 'status_changed',
            'trigger_config': {
                'from_status': 'in_progress',
                'to_status': 'done',
            },
            'actions': [
                {'type': 'send_email', 'to': '{{issue.reporter.email}}',
                 'subject': 'Issue completed', 'body': '{{issue.summary}} is done'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_field_changed_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Priority escalation',
            'trigger_type': 'field_changed',
            'trigger_config': {
                'field': 'priority',
                'from_value': 'low',
                'to_value': 'urgent',
            },
            'conditions': [
                {'type': 'field_match', 'field': 'assignee_id', 'operator': 'is_not_empty'},
            ],
            'actions': [
                {'type': 'comment', 'body': 'Priority escalated to urgent!'},
                {'type': 'webhook', 'url': 'https://slack.example.com/hook',
                 'method': 'POST', 'body': {'text': '{{issue.key}} escalated'}},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_scheduled_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'name': 'Daily stale check',
            'trigger_type': 'scheduled',
            'trigger_config': {
                'cron': '0 9 * * 1-5',
                'timezone': 'Asia/Kolkata',
            },
            'actions': [
                {'type': 'comment', 'body': 'This issue has been open for a while.'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_rule_with_if_else(self):
        """Test if/else branching in actions."""
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Branching rule',
            'trigger_type': 'issue_created',
            'actions': [
                {
                    'type': 'if',
                    'condition': {'type': 'jql_match', 'jql': 'priority = urgent'},
                    'then': [
                        {'type': 'assign', 'target': 'project_lead'},
                        {'type': 'comment', 'body': 'Urgent: escalated to lead'},
                    ],
                    'else': [
                        {'type': 'comment', 'body': 'Standard assignment will apply'},
                    ],
                },
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        actions = resp.json()['actions']
        self.assertEqual(actions[0]['type'], 'if')
        self.assertEqual(len(actions[0]['then']), 2)
        self.assertEqual(len(actions[0]['else']), 1)

    def test_create_manual_trigger_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Manual deploy',
            'trigger_type': 'manual',
            'actions': [
                {'type': 'transition', 'to_status': 'deployed'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_webhook_trigger_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Webhook handler',
            'trigger_type': 'incoming_webhook',
            'trigger_config': {'webhook_key': 'my-secret-key'},
            'actions': [
                {'type': 'edit_field', 'field': 'labels', 'value': 'webhook-triggered'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_subtask_completed_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'All subtasks done → close parent',
            'trigger_type': 'subtask_completed',
            'conditions': [
                {'type': 'subtasks_done'},
            ],
            'actions': [
                {'type': 'transition', 'to_status': 'done'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_sprint_started_rule(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Sprint kickoff',
            'trigger_type': 'sprint_started',
            'actions': [
                {'type': 'comment', 'body': 'Sprint started!'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_all_13_trigger_types_valid(self):
        expected = [
            'issue_created', 'issue_updated', 'status_changed', 'field_changed',
            'comment_added', 'sprint_started', 'sprint_completed', 'version_released',
            'worklog_added', 'subtask_completed', 'scheduled', 'manual', 'incoming_webhook',
        ]
        valid = [c[0] for c in AutomationRule.TRIGGER_TYPE_CHOICES]
        for t in expected:
            self.assertIn(t, valid)
        self.assertEqual(len(valid), 13)

    def test_toggle_rule(self):
        """Toggle rule enabled/disabled."""
        rule = AutomationRule.objects.create(
            project=self.project, name='Toggle Test',
            trigger_type='issue_created', is_enabled=True,
            created_by_id=self.user.id
        )
        # Disable
        resp = self.client.post(f'/shizuha-pulse/api/automation-rules/{rule.id}/toggle/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        rule.refresh_from_db()
        self.assertFalse(rule.is_enabled)
        # Enable again
        resp = self.client.post(f'/shizuha-pulse/api/automation-rules/{rule.id}/toggle/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        rule.refresh_from_db()
        self.assertTrue(rule.is_enabled)

    def test_view_rule_logs(self):
        rule = AutomationRule.objects.create(
            project=self.project, name='Log Test',
            trigger_type='issue_created',
            created_by_id=self.user.id
        )
        item = Item.objects.create(
            mode='task', title='Log Item', project=self.project,
            item_key='AUTO-1', status='pending', created_by_id=self.user.id
        )
        # Create some logs
        AutomationRuleLog.objects.create(
            rule=rule, item=item, status='success',
            trigger_data={'event': 'issue_created'},
            actions_executed=[{'type': 'assign', 'result': 'ok'}],
            duration_ms=150
        )
        AutomationRuleLog.objects.create(
            rule=rule, item=item, status='error',
            error_message='Failed to send email',
            duration_ms=50
        )
        AutomationRuleLog.objects.create(
            rule=rule, status='skipped',
            trigger_data={'event': 'issue_created'},
            duration_ms=5
        )
        resp = self.client.get(f'/shizuha-pulse/api/automation-rules/{rule.id}/logs/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertEqual(len(results), 3)

    def test_rate_limiting_config(self):
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'project': self.project.id,
            'name': 'Rate Limited',
            'trigger_type': 'issue_updated',
            'max_executions_per_hour': 10,
            'actions': [{'type': 'comment', 'body': 'Updated!'}],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['max_executions_per_hour'], 10)

    def test_global_rule_no_project(self):
        """Rules without a project apply globally."""
        resp = self.client.post('/shizuha-pulse/api/automation-rules/', {
            'name': 'Global rule',
            'trigger_type': 'issue_created',
            'actions': [{'type': 'comment', 'body': 'Global rule fired'}],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertIsNone(resp.json().get('project'))

    def test_filter_by_project(self):
        AutomationRule.objects.create(
            project=self.project, name='Filtered',
            trigger_type='issue_created',
            created_by_id=self.user.id
        )
        resp = self.client.get(f'/shizuha-pulse/api/automation-rules/?project={self.project.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_update_rule(self):
        rule = AutomationRule.objects.create(
            project=self.project, name='Old',
            trigger_type='issue_created',
            created_by_id=self.user.id
        )
        resp = self.client.patch(f'/shizuha-pulse/api/automation-rules/{rule.id}/', {
            'name': 'Updated Rule',
            'description': 'Now with description',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        rule.refresh_from_db()
        self.assertEqual(rule.name, 'Updated Rule')

    def test_delete_rule(self):
        rule = AutomationRule.objects.create(
            project=self.project, name='Del',
            trigger_type='issue_created',
            created_by_id=self.user.id
        )
        resp = self.client.delete(f'/shizuha-pulse/api/automation-rules/{rule.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class AutomationRuleLogAPITests(AuthenticatedAPITestCase):
    """Read-only tests for AutomationRuleLog API."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Log Project', key='LOG', org_id=1
        )
        self.rule = AutomationRule.objects.create(
            project=self.project, name='Log Rule',
            trigger_type='issue_created',
            created_by_id=self.user.id
        )

    def test_list_logs(self):
        AutomationRuleLog.objects.create(
            rule=self.rule, status='success', duration_ms=100
        )
        resp = self.client.get('/shizuha-pulse/api/automation-logs/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_logs_are_readonly(self):
        resp = self.client.post('/shizuha-pulse/api/automation-logs/', {
            'rule': str(self.rule.id),
            'status': 'success',
        }, format='json')
        self.assertIn(resp.status_code, [
            http_status.HTTP_405_METHOD_NOT_ALLOWED,
            http_status.HTTP_403_FORBIDDEN,
        ])

    def test_filter_logs_by_rule(self):
        AutomationRuleLog.objects.create(
            rule=self.rule, status='success', duration_ms=100
        )
        resp = self.client.get(f'/shizuha-pulse/api/automation-logs/?rule={self.rule.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)


class ItemBulkOperationAPITests(AuthenticatedAPITestCase):
    """Tests for the unified bulk operations endpoint."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Bulk Project', key='BULK', org_id=1
        )
        self.items = []
        for i in range(5):
            item = Item.objects.create(
                mode='task', title=f'Bulk Item {i}',
                project=self.project, item_key=f'BULK-{i+1}',
                status='pending', priority='normal',
                created_by_id=self.user.id
            )
            self.items.append(item)

        # The bulk 'assign' op runs through the PLS-16 operator-assignment gate,
        # which resolves assignee_id via user_service.get_user_by_id and fails
        # closed when the ID service is unreachable (it is in tests). Mock it to
        # resolve to the email these tests pass so the gate passes.
        from unittest.mock import patch
        _us = patch('tasks.views.user_service').start()
        self.addCleanup(patch.stopall)
        _us.get_user_by_id.side_effect = lambda uid: (
            {'id': self.user.id, 'email': 'test@example.com'}
            if int(uid) == self.user.id else None
        )
        _admin = patch('tasks.services.admin_service.admin_service').start()
        _admin.get_organization.return_value = {
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
        }
        _admin.is_org_member.return_value = True

    def test_bulk_transition(self):
        ids = [item.id for item in self.items]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'transition',
            'item_ids': ids,
            'data': {'to_status': 'in_progress'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['updated'], 5)
        for item in self.items:
            item.refresh_from_db()
            self.assertEqual(item.status, 'in_progress')

    def test_bulk_assign(self):
        ids = [item.id for item in self.items[:3]]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'assign',
            'item_ids': ids,
            'data': {
                'assignee_id': self.user.id,
                'assignee_email': 'test@example.com',
            },
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        for item in self.items[:3]:
            item.refresh_from_db()
            self.assertEqual(item.assignee_id, self.user.id)

    def test_bulk_edit_priority(self):
        ids = [item.id for item in self.items]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'edit',
            'item_ids': ids,
            'data': {'priority': 'high'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        for item in self.items:
            item.refresh_from_db()
            self.assertEqual(item.priority, 'high')

    def test_bulk_edit_labels(self):
        ids = [self.items[0].id, self.items[1].id]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'edit',
            'item_ids': ids,
            'data': {'tags': 'critical,hotfix'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_bulk_delete(self):
        ids = [item.id for item in self.items[3:]]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'delete',
            'item_ids': ids,
            'data': {},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['deleted'], 2)
        self.assertEqual(Item.objects.filter(project=self.project).count(), 3)

    def test_bulk_no_item_ids_fails(self):
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'transition',
            'item_ids': [],
            'data': {'to_status': 'done'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_400_BAD_REQUEST)

    def test_bulk_unknown_operation_fails(self):
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'nonexistent',
            'item_ids': [self.items[0].id],
            'data': {},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_400_BAD_REQUEST)

    def test_bulk_move_sprint(self):
        from tasks.models import Sprint
        sprint = Sprint.objects.create(
            project=self.project, name='Sprint 1',
            start_date=timezone.now().date(),
            end_date=timezone.now().date(),
        )
        ids = [self.items[0].id, self.items[1].id]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'move_sprint',
            'item_ids': ids,
            'data': {'sprint_id': sprint.id},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)


    # ---- PLAT-933: bulk endpoint must tolerate task KEYS and never 500 on bad ids ----
    def test_plat933_bulk_assign_by_item_key_no_500(self):
        """Operator repro: reassigning by task KEY (e.g. 'PLAT-921') 500'd because the
        endpoint filtered the integer pk column with a key string. Must resolve + assign."""
        keys = [self.items[0].item_key, self.items[1].item_key]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'assign',
            'item_ids': keys,
            'data': {'assignee_id': self.user.id, 'assignee_email': 'test@example.com'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['updated'], 2)
        for item in self.items[:2]:
            item.refresh_from_db()
            self.assertEqual(item.assignee_id, self.user.id)

    def test_plat933_bulk_assign_mixed_key_and_pk(self):
        """A mix of task key + numeric pk both resolve."""
        ids = [self.items[0].item_key, self.items[1].id]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'assign',
            'item_ids': ids,
            'data': {'assignee_id': self.user.id, 'assignee_email': 'test@example.com'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['updated'], 2)

    def test_plat933_bulk_unresolvable_id_no_500(self):
        """An unresolvable id yields a deterministic non-500 (processes the resolvable
        subset, like the alert bulk endpoints) — never a 500."""
        ids = ['NOPE-99999', self.items[0].item_key]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'assign',
            'item_ids': ids,
            'data': {'assignee_id': self.user.id, 'assignee_email': 'test@example.com'},
        }, format='json')
        self.assertNotEqual(resp.status_code, http_status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['updated'], 1)

    def test_plat933_bulk_transition_by_item_key(self):
        """Key tolerance applies to all bulk ops, not just assign."""
        keys = [self.items[0].item_key, self.items[1].item_key]
        resp = self.client.post('/shizuha-pulse/api/items/bulk/', {
            'operation': 'transition',
            'item_ids': keys,
            'data': {'to_status': 'in_progress'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['updated'], 2)
        for item in self.items[:2]:
            item.refresh_from_db()
            self.assertEqual(item.status, 'in_progress')


class ProjectConfigurationAPITests(AuthenticatedAPITestCase):
    """CRUD tests for ProjectConfiguration API."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Config Project', key='CFG', org_id=1
        )

    def test_create_project_configuration(self):
        its = IssueTypeScheme.objects.get(slug='default')
        ps = PermissionScheme.objects.get(slug='default')
        ns = NotificationScheme.objects.get(slug='default')
        ss = ScreenScheme.objects.get(slug='default')
        itss = IssueTypeScreenScheme.objects.get(slug='default')

        resp = self.client.post('/shizuha-pulse/api/project-configurations/', {
            'project': self.project.id,
            'issue_type_scheme': str(its.id),
            'permission_scheme': str(ps.id),
            'notification_scheme': str(ns.id),
            'issue_type_screen_scheme': str(itss.id),
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        config = ProjectConfiguration.objects.get(project=self.project)
        self.assertEqual(config.issue_type_scheme, its)
        self.assertEqual(config.permission_scheme, ps)

    def test_update_project_configuration(self):
        config = ProjectConfiguration.objects.create(project=self.project)
        ns = NotificationScheme.objects.get(slug='default')
        resp = self.client.patch(
            f'/shizuha-pulse/api/project-configurations/{config.pk}/', {
                'notification_scheme': str(ns.id),
            }, format='json'
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        config.refresh_from_db()
        self.assertEqual(config.notification_scheme, ns)

    def test_add_security_scheme_to_config(self):
        config = ProjectConfiguration.objects.create(project=self.project)
        sec = SecurityScheme.objects.create(name='Test Sec', slug='test-sec-config')
        resp = self.client.patch(
            f'/shizuha-pulse/api/project-configurations/{config.pk}/', {
                'security_scheme': str(sec.id),
            }, format='json'
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        config.refresh_from_db()
        self.assertEqual(config.security_scheme, sec)

    def test_list_configurations(self):
        ProjectConfiguration.objects.create(project=self.project)
        resp = self.client.get('/shizuha-pulse/api/project-configurations/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_retrieve_configuration(self):
        config = ProjectConfiguration.objects.create(project=self.project)
        resp = self.client.get(f'/shizuha-pulse/api/project-configurations/{config.pk}/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_delete_configuration(self):
        config = ProjectConfiguration.objects.create(project=self.project)
        resp = self.client.delete(f'/shizuha-pulse/api/project-configurations/{config.pk}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)

    def test_onetoone_constraint(self):
        """Only one configuration per project."""
        ProjectConfiguration.objects.create(project=self.project)
        resp = self.client.post('/shizuha-pulse/api/project-configurations/', {
            'project': self.project.id,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_400_BAD_REQUEST)

    def test_full_configuration_all_schemes(self):
        """Configure a project with all available schemes."""
        its = IssueTypeScheme.objects.get(slug='default')
        ps = PermissionScheme.objects.get(slug='default')
        ns = NotificationScheme.objects.get(slug='default')
        ss_screen = ScreenScheme.objects.get(slug='default')
        itss = IssueTypeScreenScheme.objects.get(slug='default')
        sec = SecurityScheme.objects.create(name='Full Sec', slug='full-sec-cfg')

        resp = self.client.post('/shizuha-pulse/api/project-configurations/', {
            'project': self.project.id,
            'issue_type_scheme': str(its.id),
            'issue_type_screen_scheme': str(itss.id),
            'security_scheme': str(sec.id),
            'permission_scheme': str(ps.id),
            'notification_scheme': str(ns.id),
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        config = ProjectConfiguration.objects.get(project=self.project)
        self.assertEqual(config.issue_type_scheme, its)
        self.assertEqual(config.issue_type_screen_scheme, itss)
        self.assertEqual(config.security_scheme, sec)
        self.assertEqual(config.permission_scheme, ps)
        self.assertEqual(config.notification_scheme, ns)

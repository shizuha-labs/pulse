import sys
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from rest_framework.test import APITestCase

from tasks.models import Item, Status, Workflow, WorkflowStatus
from tasks.services.actionable_items import filter_runnable_owner_queue_items
from tasks.services.workflow_service import WorkflowService
from tasks.serializers import ItemListSerializer, ItemSerializer, WorkflowStatusInlineSerializer


class OwnerActionPolicyTests(APITestCase):
    def setUp(self):
        self.agent = get_user_model().objects.create_user(
            username='owner-policy-agent', email='owner-policy-agent@shizuha.com',
            password='x',
        )
        self.client.force_authenticate(self.agent)

    def item(self, title, *, status='resolved_elsewhere', workflow='simple', priority='urgent'):
        item = Item.objects.create(
            title=title, mode='task', workflow_name=workflow,
            assignee_id=self.agent.pk, assignee_email=self.agent.email,
            status='todo', priority=priority,
        )
        Item.objects.filter(pk=item.pk).update(status=status, workflow_name=workflow)
        item.refresh_from_db()
        return item

    def queue(self, **extra):
        params = {
            'mode': 'task', 'assignee_email': self.agent.email,
            'queue_order': 'true', 'is_active': 'true', 'permission_hint': 'false',
        }
        params.update(extra)
        response = self.client.get('/api/items/', params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def preflight(self):
        response = self.client.get('/api/items/heartbeat-preflight/', {
            'assignee_email': self.agent.email,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def test_resolved_elsewhere_remains_visible_nonterminal_but_not_runnable(self):
        item = self.item('Answered ask remains owned')
        self.assertTrue(WorkflowService._status_releases_blocks(item.status))
        self.assertFalse(WorkflowService._status_is_terminal(item.status))
        # PLS-1382: resolved_elsewhere is assignment-EXCLUDED (capacity-pull /
        # WIP / critical-override must never seat it — PLAT-9072 / SCLI-707)
        # while remaining non-terminal and block-releasing (PLS-719). The
        # owner queue still surfaces it as awareness — pinned by the
        # assertions below, which use the waiting/link filter, not this set.
        self.assertIn(item.status, WorkflowService._assignment_excluded_statuses())
        self.assertEqual(Status.objects.get(slug=item.status).category, 'todo')
        queue = self.queue()
        self.assertEqual([row['id'] for row in queue['results']], [item.pk])
        self.assertFalse(filter_runnable_owner_queue_items(Item.objects.filter(pk=item.pk)).exists())
        self.assertEqual(queue['results'][0].get('owner_action_policy'), 'awareness')

    def test_resolved_elsewhere_does_not_wake_heartbeat(self):
        self.item('Answered ask')
        result = self.preflight()
        self.assertEqual(result['decision'], 'skip', result)
        self.assertEqual(result['reason'], 'awareness_only', result)
        self.assertEqual(result['ready_task_count'], 0)

    def test_resolved_elsewhere_does_not_focus_pin_ordinary_work(self):
        self.item('Urgent answered ask')
        runnable = self.item('Current owner work', status='todo', priority='normal')
        response = self.client.post('/api/comments/', {
            'item': runnable.pk, 'content': 'Current owner work can advance.',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        queue = self.queue()['results']
        self.assertEqual(queue[0]['id'], runnable.pk)

    def test_custom_nonterminal_defaults_actionable_despite_legacy_active_flag(self):
        status, _ = Status.objects.get_or_create(
            slug='owner_policy_custom', defaults={'name': 'Custom Owner Work', 'category': 'todo'},
        )
        workflow, _ = Workflow.objects.get_or_create(
            slug='owner-policy-custom',
            defaults={'name': 'Owner Policy Custom', 'initial_status': status},
        )
        WorkflowStatus.objects.get_or_create(workflow=workflow, status=status)
        item = self.item('Custom owner work', workflow=workflow.slug, status=status.slug)
        self.assertFalse(item.is_active())
        self.assertTrue(filter_runnable_owner_queue_items(Item.objects.filter(pk=item.pk)).exists())
        self.assertEqual(self.queue()['results'][0].get('owner_action_policy'), 'actionable')
        self.assertEqual(self.preflight()['decision'], 'run')

    def test_mcp_actual_queue_roundtrip_reports_awareness_not_ready(self):
        item = self.item('Answered ask still visible')
        mcp_dir = str(Path(__file__).resolve().parents[2] / 'mcp-server')
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        import pulse_server

        def api_request(method, endpoint, *, params=None, **kwargs):
            self.assertEqual(method, 'GET')
            self.assertEqual(endpoint, '/items/')
            response = self.client.get('/api/items/', params or {})
            self.assertEqual(response.status_code, 200, response.content)
            return response.data

        with mock.patch.object(pulse_server, '_get_my_user_id', return_value=(self.agent.pk, self.agent.email)), \
             mock.patch.object(pulse_server, 'api_request', side_effect=api_request):
            result = pulse_server.get_my_tasks()
        self.assertIn('0 ready/movable', result)
        self.assertIn('1 awareness-only', result)
        self.assertIn(item.title, result)
        self.assertIn('0 terminal', result)

    def test_policy_is_workflow_scoped_and_supports_display_name(self):
        item = self.item('Display-name workflow', workflow=Workflow.objects.get(slug='simple').name)
        self.assertEqual(self.queue()['results'][0]['owner_action_policy'], 'awareness')
        self.assertFalse(filter_runnable_owner_queue_items(Item.objects.filter(pk=item.pk)).exists())
        workflow = Workflow.objects.create(
            slug='owner-policy-alternate', name='Owner Policy Alternate',
            initial_status=Status.objects.get(slug='resolved_elsewhere'),
        )
        WorkflowStatus.objects.create(workflow=workflow, status=workflow.initial_status)
        Item.objects.filter(pk=item.pk).update(workflow_name=workflow.slug)
        self.assertEqual(self.queue()['results'][0]['owner_action_policy'], 'actionable')
        self.assertTrue(filter_runnable_owner_queue_items(Item.objects.filter(pk=item.pk)).exists())

    def test_policy_change_rearms_real_queue_and_preflight_without_state_mutation(self):
        item = self.item('Policy-controlled owner action')
        membership = WorkflowStatus.objects.get(
            workflow__slug='simple', status__slug='resolved_elsewhere',
        )
        self.assertEqual(self.preflight()['decision'], 'skip')
        membership.owner_action_policy = 'actionable'
        membership.save(update_fields=['owner_action_policy'])
        self.assertEqual(self.preflight()['decision'], 'run')
        self.assertEqual(self.queue()['results'][0]['owner_action_policy'], 'actionable')
        membership.owner_action_policy = 'awareness'
        membership.save(update_fields=['owner_action_policy'])
        self.assertEqual(self.preflight()['decision'], 'skip')
        item.refresh_from_db()
        self.assertEqual(item.status, 'resolved_elsewhere')
        self.assertEqual(item.assignee_id, self.agent.pk)

    def test_detail_list_and_catalog_serialize_same_readonly_policy(self):
        item = self.item('Read-only owner projection')
        membership = WorkflowStatus.objects.get(workflow__slug='simple', status__slug=item.status)
        self.assertEqual(WorkflowStatusInlineSerializer(membership).data['owner_action_policy'], 'awareness')
        for serializer_class in (ItemSerializer, ItemListSerializer):
            serializer = serializer_class(item)
            self.assertTrue(serializer.fields['owner_action_policy'].read_only)
            self.assertEqual(serializer.data['owner_action_policy'], 'awareness')
        response = self.client.get(f'/api/items/{item.pk}/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['owner_action_policy'], 'awareness')
        self.assertFalse(response.data['is_active_item'])

    def test_data_migration_and_reference_reload_preserve_exact_policy(self):
        from importlib import import_module
        migration = import_module('tasks.migrations.0177_workflowstatus_owner_action_policy')
        membership = WorkflowStatus.objects.get(workflow__slug='simple', status__slug='resolved_elsewhere')
        WorkflowStatus.objects.filter(pk=membership.pk).update(owner_action_policy='actionable')
        with connection.schema_editor() as schema_editor:
            migration.seed_owner_action_policy(apps, schema_editor)
        membership.refresh_from_db()
        self.assertEqual(membership.owner_action_policy, 'awareness')
        call_command('loaddata', 'reference_data.json', verbosity=0)
        membership.refresh_from_db()
        self.assertEqual(membership.owner_action_policy, 'awareness')
        self.assertFalse(WorkflowStatus.objects.exclude(pk=membership.pk).exclude(owner_action_policy='actionable').exists())

    def test_awareness_does_not_hide_ordinary_custom_ready_work_from_mcp(self):
        self.item('Answered ask')
        ordinary = self.item('Unknown but active custom status', status='owner_policy_unknown', workflow='autonomous-bug')
        rows = self.queue()['results']
        ordinary_row = next(row for row in rows if row['id'] == ordinary.pk)
        self.assertEqual(ordinary_row['owner_action_policy'], 'actionable')
        mcp_dir = str(Path(__file__).resolve().parents[2] / 'mcp-server')
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        import pulse_server
        self.assertTrue(pulse_server._is_backlog_pull_ready(ordinary_row))
        result = pulse_server._fmt_items(rows)
        self.assertIn('1 ready/movable', result)
        self.assertIn('1 awareness-only', result)

    def test_unannotated_serializer_reads_catalog_once_per_request(self):
        items = [self.item(f'Answered ask {number}') for number in range(3)]
        serializer = ItemListSerializer()
        with self.assertNumQueries(1):
            policies = [serializer.get_owner_action_policy(item) for item in items]
        self.assertEqual(policies, ['awareness'] * 3)
        WorkflowStatus.objects.filter(
            workflow__slug='simple', status__slug='resolved_elsewhere',
        ).update(owner_action_policy='actionable')
        with self.assertNumQueries(1):
            self.assertEqual(ItemListSerializer().get_owner_action_policy(items[0]), 'actionable')

"""PLS-216 regression coverage for transition JSON-list field normalization."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class TransitionJsonFieldNormalizationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls216',
            email='pls216@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        self.done_status, _ = Status.objects.get_or_create(
            slug='done',
            defaults={'name': 'Done', 'category': 'done', 'position': 2},
        )
        self.workflow = Workflow.objects.create(
            name='Autonomous Dev PLS216',
            slug='autonomous-dev-pls216',
            initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.open_status, position=1)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.done_status, position=2)
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Resolve',
            from_status='open',
            to_status='done',
            validators=[{'type': 'field_required', 'field': 'title'}],
            post_functions=[],
            screen_fields=[],
            allowed_roles=[],
        )
        self.project = Project.objects.create(name='PLS216 Project', key='P216', org_id=1)
        self.item = Item.objects.create(
            mode='task',
            title='Open task',
            project=self.project,
            item_key='P216-1',
            status='open',
            workflow_name=self.workflow.name,
            created_by_id=self.user.id,
        )

    def test_transition_patch_parses_json_string_validators(self):
        validators = [{'type': 'field_required', 'field': 'description'}]

        response = self.client.patch(
            f'/api/transitions/{self.transition.id}/',
            {'validators': json.dumps(validators)},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.validators, validators)
        self.assertEqual(response.data['validators'], validators)

    def test_transition_patch_rejects_double_encoded_validator_string(self):
        original = self.transition.validators
        double_encoded = json.dumps(json.dumps([{'type': 'field_required', 'field': 'description'}]))

        response = self.client.patch(
            f'/api/transitions/{self.transition.id}/',
            {'validators': double_encoded},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.validators, original)

    def test_transition_patch_rejects_double_encoded_post_functions_string(self):
        double_encoded = json.dumps(json.dumps([{'type': 'set_resolution', 'resolution': 'fixed'}]))

        response = self.client.patch(
            f'/api/transitions/{self.transition.id}/',
            {'post_functions': double_encoded},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.post_functions, [])

    def test_transition_patch_accepts_native_allowed_roles_list(self):
        response = self.client.patch(
            f'/api/transitions/{self.transition.id}/',
            {'allowed_roles': ['admin', 'member']},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.allowed_roles, ['admin', 'member'])

    def test_run_validators_skips_malformed_legacy_string_row(self):
        self.transition.validators = 'not json'
        self.transition.save(update_fields=['validators'])

        with self.assertLogs('tasks.services.workflow_service', level='WARNING'):
            WorkflowService._run_validators(self.transition, self.item, self.user)


    def test_execute_post_functions_skips_double_encoded_legacy_string_row(self):
        self.transition.post_functions = json.dumps(json.dumps([{'type': 'set_resolution', 'resolution': 'fixed'}]))
        self.transition.save(update_fields=['post_functions'])

        with self.assertLogs('tasks.services.workflow_service', level='WARNING'):
            WorkflowService.execute_post_functions(self.transition, self.item, self.user)

        self.item.refresh_from_db()
        self.assertIsNone(self.item.resolution_id)

    def test_run_validators_parses_legacy_json_string_row(self):
        self.transition.validators = json.dumps([
            {'type': 'field_required', 'field': 'description', 'message': 'description required'}
        ])
        self.transition.save(update_fields=['validators'])

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(self.transition, self.item, self.user)

        self.assertIn('description required', str(ctx.exception))

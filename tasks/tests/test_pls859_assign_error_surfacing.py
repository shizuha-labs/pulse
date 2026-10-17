"""PLS-859: assignment failures name the failing field; team aliases are not silent.

Acceptance:
1. Direct-assign ValidationError → 409 with human ``detail`` naming field/constraint
2. Unknown assignment_group slug → 400 naming the offending slug (no silent fallback)
3. Alias rewrite (trading-engineering → engineering) is announced, never silent
"""
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tasks.models import Item, Project, TeamRoster
from tasks.views import (
    _format_assignment_validation_detail,
    _normalize_validation_error_details,
)

User = get_user_model()


class FormatHelpersTests(TestCase):
    def test_normalize_and_format_field_map(self):
        exc = ValidationError({
            'assignee_id': ['routing_authority:direct_writer_denied'],
            'assignee_email': 'not authorized',
        })
        details = _normalize_validation_error_details(exc)
        self.assertEqual(
            details['assignee_id'],
            ['routing_authority:direct_writer_denied'],
        )
        self.assertEqual(details['assignee_email'], ['not authorized'])
        text = _format_assignment_validation_detail(details)
        self.assertIn('assignee_id: routing_authority:direct_writer_denied', text)
        self.assertIn('assignee_email: not authorized', text)


@override_settings(ROOT_URLCONF='taskmanager.urls')
class AssignValidationDetailAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        suffix = uuid.uuid4().hex[:8]
        self.actor = User.objects.create_user(
            username=f'pls859a{suffix}', email=f'pls859a{suffix}@shizuha.com',
            password='x', is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.actor)
        key = f'P8{suffix[:4].upper()}'
        self.project = Project.objects.create(
            name=f'PLS859-{suffix}', key=key, org_id=1,
            access_level='open', is_active=True,
        )
        roster, _ = TeamRoster.objects.get_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={'tiers': [['eng859@shizuha.com']], 'is_active': True},
        )
        if 'eng859@shizuha.com' not in str(roster.tiers) or not roster.is_active:
            roster.tiers = [['eng859@shizuha.com']]
            roster.is_active = True
            roster.save(update_fields=['tiers', 'is_active'])
        self.item = Item.objects.create(
            mode='task', item_type='task.bug', title='pls859',
            project=self.project, item_key=f'{key}-1',
            assignment_group='engineering', status='open',
            workflow_name='autonomous-bug',
        )

    @patch('tasks.services.admin_service.admin_service.get_organization',
           side_effect=lambda org_id: {'id': int(org_id), 'slug': 'shizuha'})
    @patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True)
    @patch('tasks.user_service.user_service.resolve_user_info')
    @patch.object(Item, 'save', autospec=True)
    def test_direct_assign_validation_failure_names_field_in_detail(
        self, mock_save, mock_resolve, _org_member, _get_org,
    ):
        mock_resolve.return_value = {
            'id': 4242,
            'email': 'target859@shizuha.com',
            'username': 'target859',
            'full_name': 'Target 859',
            'is_agent': True,
        }
        mock_save.side_effect = ValidationError({
            'assignee_id': 'routing_authority:direct_writer_denied',
        })

        resp = self.client.post(
            f'/api/items/{self.item.pk}/assign/',
            {
                'assignee_email': 'target859@shizuha.com',
                'direct_assignment_reason': 'SoD handoff to independent architect',
            },
            format='json',
        )

        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.data['error'], 'assignment_validation_failed')
        self.assertIn('detail', resp.data)
        self.assertIn('assignee_id', resp.data['detail'])
        self.assertIn('direct_writer_denied', resp.data['detail'])
        self.assertEqual(
            resp.data['details']['assignee_id'],
            ['routing_authority:direct_writer_denied'],
        )

    @patch('tasks.services.admin_service.admin_service.get_organization',
           side_effect=lambda org_id: {'id': int(org_id), 'slug': 'shizuha'})
    def test_unknown_assignment_group_400_names_slug(self, _get_org):
        resp = self.client.post(
            f'/api/items/{self.item.pk}/assign/',
            {'assignment_group': 'platform'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.data if hasattr(resp, 'data') else resp.json()
        err = str(body.get('error') or body.get('detail') or body)
        self.assertIn('platform', err)
        self.assertIn('not registered', err.lower())

    @patch('tasks.services.admin_service.admin_service.get_organization',
           side_effect=lambda org_id: {'id': int(org_id), 'slug': 'shizuha'})
    @patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    @patch('tasks.services.workflow_service.WorkflowService.assignment_org_mismatch',
           return_value=(False, 'shizuha', 'shizuha'))
    @patch('tasks.services.workflow_service.WorkflowService._email_in_current_team_active_tier',
           return_value=True)
    @patch('tasks.user_service.user_service.resolve_user_info')
    def test_alias_rewrite_is_announced_not_silent(
        self, mock_resolve, _tier, _mismatch, mock_resolve_team, _org_member, _get_org,
    ):
        mock_resolve_team.return_value = (9001, 'eng859@shizuha.com')
        mock_resolve.return_value = {
            'id': 9001,
            'email': 'eng859@shizuha.com',
            'username': 'eng859',
            'full_name': 'Eng 859',
            'is_agent': True,
        }

        resp = self.client.post(
            f'/api/items/{self.item.pk}/assign/',
            {'assignment_group': 'trading-engineering'},
            format='json',
        )

        self.assertIn(resp.status_code, (200, 202), resp.content)
        self.assertEqual(resp.data.get('assignment_group'), 'engineering')
        self.assertEqual(resp.data.get('requested_assignment_group'), 'trading-engineering')
        self.assertTrue(resp.data.get('assignment_group_canonicalized'))
        self.assertIn('canonicalized', (resp.data.get('message') or '').lower())
        self.assertIn('trading-engineering', resp.data.get('message') or '')

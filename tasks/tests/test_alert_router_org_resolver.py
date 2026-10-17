"""Exact-organization, item-free resolver contract for the alert router."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import TeamRoster
from tasks.services.workflow_service import WorkflowService


User = get_user_model()


class AlertRouterOrganizationResolverTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='alert-router-staff',
            email='alert-router-staff@example.com',
            password='x',
            is_staff=True,
        )
        self.member = User.objects.create_user(
            username='alert-router-member',
            email='alert-router-member@example.com',
            password='x',
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'organization_id': 1,
                'tiers': [['core-engineer@shizuha.com']],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha-digital',
            team_name='engineering',
            defaults={
                'organization_id': 2,
                'tiers': [['digital-engineer@shizuha.com']],
                'is_active': True,
            },
        )

    @patch.object(WorkflowService, '_agent_workable_load', return_value=0)
    @patch.object(WorkflowService, '_agent_active_load', return_value=0)
    @patch.object(
        WorkflowService,
        '_is_agent_available_for_assignment',
        return_value=True,
    )
    @patch('tasks.user_service.user_service.get_user_by_email')
    def test_service_resolver_never_borrows_same_named_foreign_roster(
        self,
        get_user,
        _available,
        _active,
        _workable,
    ):
        get_user.side_effect = lambda email: {
            'id': 22 if email.startswith('digital-') else 11,
            'email': email,
            'username': email.split('@', 1)[0],
        }

        uid, email = WorkflowService._resolve_team_assignee(
            'engineering',
            organization_id=2,
            enforce_cap=False,
        )

        self.assertEqual(uid, 22)
        self.assertEqual(email, 'digital-engineer@shizuha.com')

    @patch.object(
        WorkflowService,
        '_resolve_team_assignee',
        return_value=(22, 'digital-engineer@shizuha.com'),
    )
    @patch(
        'tasks.user_service.user_service.get_user_by_id',
        return_value={'username': 'digital-engineer'},
    )
    def test_privileged_probe_passes_explicit_org_and_disables_wip(
        self,
        _get_user,
        resolve,
    ):
        self.client.force_authenticate(self.staff)

        response = self.client.get(
            '/api/teams/resolve-assignee/',
            {'group': 'engineering', 'organization_id': '2'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['organization_id'], 2)
        self.assertEqual(response.data['username'], 'digital-engineer')
        self.assertTrue(response.data['available'])
        resolve.assert_called_once_with(
            'engineering',
            item=None,
            enforce_cap=False,
            organization_id=2,
        )

    def test_unprivileged_org_probe_is_denied(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(
            '/api/teams/resolve-assignee/',
            {'group': 'engineering', 'organization_id': '2'},
        )
        self.assertEqual(response.status_code, 403)

    def test_org_probe_requires_positive_integer(self):
        self.client.force_authenticate(self.staff)
        for value in ('0', '-1', 'not-an-org'):
            with self.subTest(value=value):
                response = self.client.get(
                    '/api/teams/resolve-assignee/',
                    {'group': 'engineering', 'organization_id': value},
                )
                self.assertEqual(response.status_code, 400)

from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from tasks.models import Item, Project, ProjectMembership, TeamRoster
from tasks.permissions import is_item_participant
from tasks.services.permissions import PermissionService


User = get_user_model()


class ProjectMembershipOrganizationIsolationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='hritik',
            email='owner@example.test',
            password='test',
            is_staff=False,
        )
        self.user.organization_memberships = {'4': 'owner'}
        self.personal = Project.objects.create(
            org_id=4,
            name='Books',
            key='BKSH',
            access_level='private',
        )
        self.digital = Project.objects.create(
            org_id=2,
            name='Digital Books',
            key='BKSD',
            access_level='private',
        )

    def test_direct_project_membership_never_grants_a_foreign_org_project(self):
        ProjectMembership.objects.create(
            project=self.personal,
            user_id=self.user.id,
            user_email=self.user.email,
        )
        ProjectMembership.objects.create(
            project=self.digital,
            user_id=self.user.id,
            user_email=self.user.email,
        )

        projects = PermissionService.get_user_projects(user=self.user)

        self.assertEqual(list(projects.values_list('id', flat=True)), [self.personal.id])

    def test_foreign_project_participant_is_not_visible(self):
        local = Item.objects.create(
            project=self.personal,
            title='Local',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        foreign = Item.objects.create(
            project=self.digital,
            title='Foreign',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        private = Item.objects.create(
            title='Personal',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

        visible = PermissionService.get_user_item_queryset(self.user)

        self.assertSetEqual(
            set(visible.values_list('id', flat=True)),
            {local.id, private.id},
        )
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=False,
        ):
            self.assertFalse(is_item_participant(self.user, foreign))

    @patch('tasks.serializers.user_service.get_user_by_id', return_value=None)
    @patch('tasks.user_service.user_service.get_agent_identity_index')
    @patch('tasks.services.admin_service.admin_service.get_org_members')
    def test_project_members_endpoint_hides_stale_foreign_rows(
        self,
        get_org_members,
        get_agent_identity_index,
        _get_user,
    ):
        # Keep fixture identities disjoint from the database-assigned test user.
        # In an eight-worker full-suite run the sequence can legitimately reach
        # legacy hard-coded IDs such as 44/91, making this test order-dependent.
        local_agent_id = self.user.id + 100_000
        foreign_agent_id = self.user.id + 100_001
        get_org_members.return_value = [
            {'user_id': self.user.id, 'user_email': self.user.email},
            {'user_id': local_agent_id, 'user_email': 'sato@shizuha.com'},
            {'user_id': foreign_agent_id, 'user_email': 'banto@shizuha.com'},
        ]
        get_agent_identity_index.return_value = {
            'user_ids': {local_agent_id, foreign_agent_id},
            'emails': {'sato@shizuha.com', 'banto@shizuha.com'},
        }
        TeamRoster.objects.create(
            organization_id=self.personal.org_id,
            organization_slug='personal-3',
            team_name='accounting',
            tiers=[['sato@shizuha.com']],
        )
        ProjectMembership.objects.create(
            project=self.personal,
            user_id=self.user.id,
            user_email=self.user.email,
            role='admin',
        )
        ProjectMembership.objects.create(
            project=self.personal,
            user_id=local_agent_id,
            user_email='sato@shizuha.com',
        )
        ProjectMembership.objects.create(
            project=self.personal,
            user_id=foreign_agent_id,
            user_email='banto@shizuha.com',
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(f'/api/projects/{self.personal.id}/members/')

        self.assertEqual(response.status_code, 200)
        returned = {row['user_email'] for row in response.data['members']}
        self.assertEqual(returned, {self.user.email, 'sato@shizuha.com'})
        self.assertNotIn('banto@shizuha.com', returned)

    @patch('tasks.views.user_service.get_user_by_id', return_value=None)
    @patch('tasks.user_service.user_service.get_agent_identity_index')
    @patch('tasks.services.admin_service.admin_service.get_org_members')
    def test_assignable_users_hides_stale_foreign_rows(
        self,
        get_org_members,
        get_agent_identity_index,
        _get_user,
    ):
        # Derive fixture identities from the database-assigned user. In the
        # eight-worker full suite that sequence can legitimately be 44 or 91;
        # hard-coding either value then creates the same (project, user_id)
        # twice inside this test and randomly wedges the deployment gate.
        local_agent_id = self.user.id + 100_000
        foreign_agent_id = self.user.id + 100_001
        get_org_members.return_value = [
            {'user_id': self.user.id, 'user_email': self.user.email},
            {'user_id': local_agent_id, 'user_email': 'sato@shizuha.com'},
            {'user_id': foreign_agent_id, 'user_email': 'banto@shizuha.com'},
        ]
        get_agent_identity_index.return_value = {
            'user_ids': {local_agent_id, foreign_agent_id},
            'emails': {'sato@shizuha.com', 'banto@shizuha.com'},
        }
        TeamRoster.objects.create(
            organization_id=self.personal.org_id,
            organization_slug='personal-3',
            team_name='accounting',
            tiers=[['sato@shizuha.com']],
        )
        for user_id, email in (
            (self.user.id, self.user.email),
            (local_agent_id, 'sato@shizuha.com'),
            (foreign_agent_id, 'banto@shizuha.com'),
        ):
            ProjectMembership.objects.create(
                project=self.personal,
                user_id=user_id,
                user_email=email,
            )
        self.client.force_authenticate(self.user)

        response = self.client.get(
            '/api/items/assignable_users/',
            {'project': self.personal.id, 'organization': self.personal.org_id},
        )

        self.assertEqual(response.status_code, 200)
        returned = {row['email'] for row in response.data['users']}
        self.assertEqual(returned, {self.user.email, 'sato@shizuha.com'})
        self.assertNotIn('banto@shizuha.com', returned)

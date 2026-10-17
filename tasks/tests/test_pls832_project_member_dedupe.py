"""PLS-832: project membership list must return unique identities."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from tasks.models import Project, ProjectMembership
from tasks.services.permissions import PermissionService


User = get_user_model()


class ProjectMemberDedupeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='pls832-admin',
            email='pls832-admin@example.com',
            password='test',
            is_staff=False,
        )
        self.user.organization_memberships = {'4': 'owner'}
        self.project = Project.objects.create(
            org_id=4,
            name='Pulse',
            key='PLS832',
            access_level='private',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='admin',
        )

    def test_dedupe_collapses_user_id_and_email_only_edges(self):
        hana_uid = self.user.id + 50_000
        a = ProjectMembership.objects.create(
            project=self.project,
            user_id=hana_uid,
            user_email='hana@shizuha.com',
            role='member',
        )
        b = ProjectMembership.objects.create(
            project=self.project,
            user_id=None,
            user_email='hana@shizuha.com',
            role='member',
        )
        deduped = PermissionService.dedupe_project_memberships(
            ProjectMembership.objects.filter(project=self.project, is_active=True),
        )
        hana_rows = [m for m in deduped if (m.user_email or '').lower() == 'hana@shizuha.com']
        self.assertEqual(len(hana_rows), 1)
        self.assertEqual(hana_rows[0].id, a.id)
        self.assertEqual(hana_rows[0].user_id, hana_uid)
        self.assertNotEqual(hana_rows[0].id, b.id)

    def test_dedupe_prefers_higher_role(self):
        uid = self.user.id + 50_001
        ProjectMembership.objects.create(
            project=self.project,
            user_id=uid,
            user_email='kai@shizuha.com',
            role='viewer',
        )
        admin_row = ProjectMembership.objects.create(
            project=self.project,
            user_id=None,
            user_email='kai@shizuha.com',
            role='admin',
        )
        deduped = PermissionService.dedupe_project_memberships(
            ProjectMembership.objects.filter(project=self.project, is_active=True),
        )
        kai = [m for m in deduped if 'kai@' in (m.user_email or '').lower()]
        self.assertEqual(len(kai), 1)
        self.assertEqual(kai[0].role, 'admin')
        self.assertEqual(kai[0].id, admin_row.id)

    def test_dedupe_collapses_shizuha_agent_email_aliases(self):
        uid = self.user.id + 50_002
        ProjectMembership.objects.create(
            project=self.project,
            user_id=uid,
            user_email='zen@shizuha.com',
            role='member',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=None,
            user_email='zen@agents.shizuha.io',
            role='member',
        )
        deduped = PermissionService.dedupe_project_memberships(
            ProjectMembership.objects.filter(project=self.project, is_active=True),
        )
        zen_rows = [
            m for m in deduped
            if (m.user_email or '').lower() in {
                'zen@shizuha.com', 'zen@agents.shizuha.io',
            }
        ]
        self.assertEqual(len(zen_rows), 1)

    @patch('tasks.serializers.user_service.get_user_by_id', return_value=None)
    @patch('tasks.user_service.user_service.get_agent_identity_index')
    @patch('tasks.services.admin_service.admin_service.get_org_members')
    def test_members_endpoint_returns_unique_emails(
        self,
        get_org_members,
        get_agent_identity_index,
        _get_user,
    ):
        hana_uid = self.user.id + 50_003
        get_org_members.return_value = [
            {'user_id': self.user.id, 'user_email': self.user.email},
            {'user_id': hana_uid, 'user_email': 'hana@shizuha.com'},
        ]
        get_agent_identity_index.return_value = {
            'user_ids': set(),
            'emails': set(),
        }
        ProjectMembership.objects.create(
            project=self.project,
            user_id=hana_uid,
            user_email='hana@shizuha.com',
            role='member',
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=None,
            user_email='hana@shizuha.com',
            role='member',
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(f'/api/projects/{self.project.id}/members/')
        self.assertEqual(response.status_code, 200)
        emails = [row['user_email'] for row in response.data['members']]
        self.assertEqual(emails.count('hana@shizuha.com'), 1)
        self.assertEqual(response.data['count'], len(response.data['members']))
        self.assertEqual(len(emails), len(set(emails)))

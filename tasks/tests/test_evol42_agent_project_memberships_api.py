"""EVOL-42: API feed for project-affinity cognitive-load metrics."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from tasks.models import AgentProjectMembership, Project
from tasks.tests.base import AuthenticatedAPITestCase


class AgentProjectMembershipListViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.platform = Project.objects.create(
            name='Shizuha Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self.hive = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )
        self.trading = Project.objects.create(
            name='Trading', key='TRD', org_id=1,
            access_level='private', is_active=True,
        )

    def _membership(self, *, project=None, email='nagi@shizuha.com', uid=101,
                    tier='primary', active=True, expires_at=None, is_lead=False):
        return AgentProjectMembership.objects.create(
            project=project or self.platform,
            agent_user_email=email,
            agent_user_id=uid,
            tier=tier,
            active=active,
            expires_at=expires_at,
            is_lead=is_lead,
        )

    def test_lists_only_active_non_expired_memberships_with_project_fields(self):
        active = self._membership(project=self.platform, tier='primary', is_lead=True)
        secondary = self._membership(
            project=self.hive,
            email='sara@shizuha.com',
            uid=102,
            tier='secondary',
            expires_at=timezone.now() + timedelta(days=1),
        )
        self._membership(project=self.trading, email='old@shizuha.com', uid=103, active=False)
        self._membership(
            project=self.trading,
            email='expired@shizuha.com',
            uid=104,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.client.get(reverse('agent-project-memberships'))

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual([row['id'] for row in rows], [active.pk, secondary.pk])
        self.assertEqual(rows[0]['agent_user_email'], 'nagi@shizuha.com')
        self.assertEqual(rows[0]['agent_user_id'], 101)
        self.assertEqual(rows[0]['project_id'], self.platform.id)
        self.assertEqual(rows[0]['project_key'], 'PLAT')
        self.assertEqual(rows[0]['project_name'], 'Shizuha Platform')
        self.assertEqual(rows[0]['project'], {
            'id': self.platform.id,
            'key': 'PLAT',
            'name': 'Shizuha Platform',
        })
        self.assertEqual(rows[0]['tier'], 'primary')
        self.assertTrue(rows[0]['is_lead'])
        self.assertTrue(rows[0]['active'])
        self.assertIsNone(rows[0]['expires_at'])
        self.assertIsNotNone(rows[0]['created_at'])
        self.assertIsNotNone(rows[0]['updated_at'])
        self.assertEqual(rows[1]['tier'], 'secondary')

    def test_filters_by_agent_email_user_id_and_project_id(self):
        self._membership(project=self.platform, email='nagi@shizuha.com', uid=101)
        self._membership(project=self.hive, email='sara@shizuha.com', uid=102)

        by_email = self.client.get(reverse('agent-project-memberships'), {
            'agent_email': 'SARA@SHIZUHA.COM',
        }).json()
        self.assertEqual(len(by_email), 1)
        self.assertEqual(by_email[0]['agent_user_email'], 'sara@shizuha.com')

        by_uid = self.client.get(reverse('agent-project-memberships'), {
            'agent_user_id': '101',
        }).json()
        self.assertEqual(len(by_uid), 1)
        self.assertEqual(by_uid[0]['agent_user_id'], 101)

        by_project = self.client.get(reverse('agent-project-memberships'), {
            'project_id': str(self.hive.id),
        }).json()
        self.assertEqual(len(by_project), 1)
        self.assertEqual(by_project[0]['project_key'], 'HIVE')

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('agent-project-memberships'))
        self.assertEqual(response.status_code, 401)

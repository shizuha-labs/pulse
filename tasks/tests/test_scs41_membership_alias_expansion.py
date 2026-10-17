"""SCS-41: InternalUserProjectMembershipsView must expand the
@shizuha.com ↔ @agents.shizuha.io alias when matching ProjectMembership.user_email.

Historical or explicitly managed membership rows may use the agent-domain alias
('zen@agents.shizuha.io'), while shizuha-id queries with the canonical
company-domain identity ('zen@shizuha.com') — and vice-versa. Without alias
expansion an explicit grant can disappear from the JWT projection.
"""
from unittest.mock import patch

from django.urls import reverse
from rest_framework.test import APITestCase

from tasks.models import Project, ProjectMembership, TeamRoster


class SCS41MembershipAliasExpansionTests(APITestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1, access_level='private', is_active=True,
        )

    def _seed(self, email, role='member'):
        # Email-only explicit grant (user_id NULL) — the shape that exposed the gap.
        return ProjectMembership.objects.create(
            project=self.project, user_id=None, user_email=email, role=role, is_active=True,
        )

    def _get(self, user_id, email):
        url = reverse('internal-user-project-memberships', kwargs={'user_id': user_id})
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member_strict',
            return_value=True,
        ):
            return self.client.get(url, {'email': email})

    def test_agent_alias_row_found_by_company_domain_query(self):
        # Row seeded @agents.shizuha.io; queried with @shizuha.com → must match.
        self._seed('zen@agents.shizuha.io')
        resp = self._get(999999, 'zen@shizuha.com')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.project.id, resp.data['project_ids'])
        self.assertEqual(resp.data['project_memberships'][str(self.project.id)], 'member')

    def test_company_domain_row_found_by_agent_alias_query(self):
        # Inverse: row seeded @shizuha.com; queried with @agents.shizuha.io → must match.
        self._seed('ryo@shizuha.com')
        resp = self._get(999998, 'ryo@agents.shizuha.io')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.project.id, resp.data['project_ids'])

    def test_unrelated_local_part_does_not_overmatch(self):
        # Alias expansion swaps only the domain — it must not widen to a different user.
        self._seed('zen@agents.shizuha.io')
        resp = self._get(999997, 'someoneelse@shizuha.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['project_ids'], [])

    def test_exact_email_still_matches(self):
        # No regression: exact (non-aliased) match still works.
        self._seed('zen@agents.shizuha.io')
        resp = self._get(999996, 'zen@agents.shizuha.io')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.project.id, resp.data['project_ids'])

    def test_user_id_match_still_works_without_email(self):
        # The user_id path is unaffected by the email-variant change.
        ProjectMembership.objects.create(
            project=self.project, user_id=4242, user_email='', role='owner', is_active=True,
        )
        url = reverse('internal-user-project-memberships', kwargs={'user_id': 4242})
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member_strict',
            return_value=True,
        ):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.project.id, resp.data['project_ids'])
        self.assertEqual(resp.data['project_memberships'][str(self.project.id)], 'owner')

    @patch(
        'tasks.services.admin_service.admin_service.is_org_member_strict',
        side_effect=lambda _user_id, org_id, _email=None: org_id == 1,
    )
    def test_foreign_org_membership_row_is_not_exported(self, _is_org_member):
        foreign = Project.objects.create(
            name='Foreign',
            key='FOR',
            org_id=2,
            access_level='private',
            is_active=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=4242,
            user_email='zen@shizuha.com',
            role='member',
            is_active=True,
        )
        ProjectMembership.objects.create(
            project=foreign,
            user_id=4242,
            user_email='zen@shizuha.com',
            role='admin',
            is_active=True,
        )

        url = reverse(
            'internal-user-project-memberships',
            kwargs={'user_id': 4242},
        )
        response = self.client.get(url, {'email': 'zen@shizuha.com'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['project_ids'], [self.project.id])

    @patch(
        'tasks.services.admin_service.admin_service.is_org_member_strict',
        return_value=False,
    )
    def test_exact_org_team_agent_membership_is_exported(self, _is_org_member):
        self._seed('zen@agents.shizuha.io')
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'organization_id': self.project.org_id,
                'tiers': [['zen@shizuha.com']],
                'is_active': True,
            },
        )

        url = reverse(
            'internal-user-project-memberships',
            kwargs={'user_id': 4242},
        )
        response = self.client.get(url, {'email': 'zen@agents.shizuha.io'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['project_ids'], [self.project.id])

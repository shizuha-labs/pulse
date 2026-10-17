"""EVOL-44 aggregate audit output attribution tests."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks.models import AgentAvailability, Comment, Item, ItemActivity, Project, TeamRoster

User = get_user_model()


class AgentOutputAttributionAuditTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='auditor', email='auditor@example.test', password='x', is_staff=True,
        )
        self.client.force_authenticate(user=self.staff)
        self.org1 = Project.objects.create(org_id=1, key='EVT', name='Evolution')
        self.org2 = Project.objects.create(org_id=2, key='OTH', name='Other')
        self.item1 = Item.objects.create(project=self.org1, title='Org 1 task', created_by_id=1, workflow_name='simple')
        self.item2 = Item.objects.create(project=self.org2, title='Org 2 task', created_by_id=1, workflow_name='simple')
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [[
                'alice@shizuha.com',
                'sara@shizuha.com',
                'codex@shizuha.com',
                'zero@shizuha.com',
            ]]},
        )

    def add_comment(self, item, email, created_at=None):
        comment = Comment.objects.create(
            item=item,
            author_id=10,
            author_email=email,
            author_name=email.split('@')[0],
            content='output line',
        )
        if created_at:
            Comment.objects.filter(pk=comment.pk).update(created_at=created_at)
        return comment

    def add_transition(self, item, email, created_at=None):
        activity = ItemActivity.objects.create(
            item=item,
            action='status_changed',
            user_id=11,
            user_email=email,
            old_value='open',
            new_value='done',
        )
        if created_at:
            ItemActivity.objects.filter(pk=activity.pk).update(created_at=created_at)
        return activity

    def test_aggregates_comments_and_transitions_without_cross_org_leakage(self):
        self.add_comment(self.item1, 'alice@shizuha.com')
        self.add_transition(self.item1, 'alice@shizuha.com')
        self.add_transition(self.item1, 'codex@shizuha.com')
        self.add_comment(self.item2, 'mallory@shizuha.com')
        self.add_transition(self.item2, 'mallory@shizuha.com')

        response = self.client.get('/api/audit/agent-output-attribution/', {'organization_id': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {row['email']: row for row in response.data['agents']}
        self.assertEqual(rows['alice@shizuha.com']['counts']['comments'], 1)
        self.assertEqual(rows['alice@shizuha.com']['counts']['transitions'], 1)
        self.assertEqual(rows['alice@shizuha.com']['counts']['total_output'], 2)
        self.assertEqual(rows['codex@shizuha.com']['counts']['transitions'], 1)
        self.assertTrue(rows['codex@shizuha.com']['is_shared_service_identity'])
        self.assertEqual(rows['sara@shizuha.com']['counts']['total_output'], 0)
        self.assertIn('zero@shizuha.com', rows)
        self.assertNotIn('mallory@shizuha.com', rows)
        self.assertEqual(response.data['privacy'], 'aggregate counts only; no comment bodies or task descriptions')

    def test_requires_explicit_org_scope(self):
        response = self.client.get('/api/audit/agent-output-attribution/')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('organization_id', response.data['error'])

    def test_nonstaff_requires_audit_role_and_requested_org_membership(self):
        member = User.objects.create_user(username='member', email='member@example.test', password='x')
        member.organization_memberships = {1: 'member'}
        self.client.force_authenticate(user=member)

        member_denied = self.client.get('/api/audit/agent-output-attribution/', {'organization_id': 1})
        wrong_org_denied = self.client.get('/api/audit/agent-output-attribution/', {'organization_id': 2})

        self.assertEqual(member_denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(wrong_org_denied.status_code, status.HTTP_403_FORBIDDEN)

        auditor = User.objects.create_user(username='orgadmin', email='orgadmin@example.test', password='x')
        auditor.organization_memberships = {'1': 'audit'}
        self.client.force_authenticate(user=auditor)
        allowed = self.client.get('/api/audit/agent-output-attribution/', {'organization_id': 1})

        self.assertEqual(allowed.status_code, status.HTTP_200_OK)


    def test_org_local_auditor_cannot_enumerate_global_roster_zero_rows(self):
        auditor = User.objects.create_user(username='org2audit', email='org2audit@example.test', password='x')
        auditor.organization_memberships = {'2': 'audit'}
        self.client.force_authenticate(user=auditor)

        response = self.client.get('/api/audit/agent-output-attribution/', {'organization_id': 2})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = {row['email'] for row in response.data['agents']}
        self.assertNotIn('alice@shizuha.com', emails)
        self.assertNotIn('sara@shizuha.com', emails)
        self.assertNotIn('codex@shizuha.com', emails)
        self.assertNotIn('zero@shizuha.com', emails)

    def test_window_excludes_old_output_and_flags_inactive_roster_rows(self):
        old = timezone.now() - timedelta(hours=30)
        self.add_comment(self.item1, 'alice@shizuha.com', created_at=old)
        AgentAvailability.objects.create(
            email='zero@shizuha.com', active=False, status='inactive', reason='test inactive',
        )

        response = self.client.get(
            '/api/audit/agent-output-attribution/',
            {'organization_id': 1, 'window_hours': 24},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {row['email']: row for row in response.data['agents']}
        self.assertEqual(rows['alice@shizuha.com']['counts']['total_output'], 0)
        self.assertNotIn('zero@shizuha.com', rows)

    def test_explicit_since_cannot_exceed_max_audit_window(self):
        old = timezone.now() - timedelta(days=15)
        self.add_comment(self.item1, 'alice@shizuha.com', created_at=old)

        response = self.client.get(
            '/api/audit/agent-output-attribution/',
            {'organization_id': 1, 'since': '2000-01-01T00:00:00Z'},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('older than', response.data['error'])

    def test_malformed_explicit_since_returns_400(self):
        response = self.client.get(
            '/api/audit/agent-output-attribution/',
            {'organization_id': 1, 'since': 'not-a-date'},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('valid ISO-8601', response.data['error'])

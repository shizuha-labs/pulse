from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, ProjectMembership, TeamRoster


User = get_user_model()


class FederatedProjectCreatePermissionTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name='Pulse',
            key='PLS',
            org_id=1,
            access_level='limited',
        )
        self.qa_user = User.objects.create_user(
            username='kei',
            email='kei@shizuha.com',
            password='testpass123',
            is_staff=False,
        )
        # Mirrors the JWT claim on FederatedUser agents: org membership lives on
        # the authenticated user object, not in the local integer-id admin path.
        self.qa_user.organization_memberships = {1: 'member'}
        ProjectMembership.objects.create(
            project=self.project,
            user_email='kei@shizuha.com',
            role='member',
            can_create=True,
        )
        self.client.force_authenticate(user=self.qa_user)

    def test_project_member_can_create_item_using_user_org_claim_and_email_membership(self):
        response = self.client.post(
            '/api/items/',
            {
                'title': 'Disposable QA smoke task',
                'mode': 'task',
                'project': self.project.id,
                'workflow_name': 'simple',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        item = Item.objects.get(title='Disposable QA smoke task')
        self.assertEqual(item.project_id, self.project.id)
        self.assertEqual(item.created_by_email, 'kei@shizuha.com')


class ExplicitOrgHeaderMembershipTests(APITestCase):
    """HIVE-433: explicit wrong-org Pulse scopes must deny, not return 2xx."""

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name='Pulse',
            key='PLS',
            org_id=146,
            access_level='limited',
        )
        self.user = User.objects.create_user(
            username='actor-a',
            email='actor-a@shizuha.com',
            password='testpass123',
            is_staff=False,
        )
        self.user.organization_memberships = {146: 'member'}
        ProjectMembership.objects.create(
            project=self.project,
            user_email='actor-a@shizuha.com',
            role='member',
            can_create=True,
        )
        TeamRoster.objects.create(
            organization_id=146,
            organization_slug='test-org-146',
            team_name='engineering',
            tiers=[['engineer@test-org-146.example']],
        )
        self.client.force_authenticate(user=self.user)

    def test_wrong_org_item_list_is_forbidden(self):
        response = self.client.get('/api/items/', HTTP_X_ORGANIZATION_ID='147')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)

    def test_wrong_org_item_create_is_forbidden_and_does_not_write(self):
        response = self.client.post(
            '/api/items/',
            {
                'title': 'wrong-org should not persist',
                'mode': 'task',
                'project': self.project.id,
                'workflow_name': 'simple',
            },
            format='json',
            HTTP_X_ORGANIZATION_ID='147',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)
        self.assertFalse(Item.objects.filter(title='wrong-org should not persist').exists())

    def test_same_org_explicit_header_preserves_create_contract(self):
        response = self.client.post(
            '/api/items/',
            {
                'title': 'same-org persists',
                'mode': 'task',
                'project': self.project.id,
                'workflow_name': 'simple',
            },
            format='json',
            HTTP_X_ORGANIZATION_ID='146',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertTrue(Item.objects.filter(title='same-org persists').exists())

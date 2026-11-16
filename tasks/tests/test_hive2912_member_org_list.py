"""HIVE-2912: a fresh org with no Pulse project must still resolve for its member.

Hive's Open Pulse CTA deep-links ``?org=<slug>``. Deriving organizations only
from Pulse projects misses that org, and the Tasks page then hard-fails with
"Organization scope unavailable". The member list comes from the caller's JWT
memberships and does not require the Admin product.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

User = get_user_model()


class MemberOrganizationListTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='zen',
            email='zen@agents.shizuha.io',
            password='testpass123',
            is_staff=False,
        )
        self.user.organization_memberships = {77: 'owner'}
        self.client.force_authenticate(user=self.user)

    def test_lists_membership_without_a_pulse_project(self):
        with patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value={'id': 77, 'slug': 'qa-probe-hive1223', 'name': 'QA Probe'},
        ):
            response = self.client.get('/api/organizations/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [{
            'id': 77,
            'name': 'QA Probe',
            'slug': 'qa-probe-hive1223',
            'role': 'owner',
        }])

    def test_keeps_membership_when_admin_lookup_has_no_slug(self):
        with patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value=None,
        ):
            response = self.client.get('/api/organizations/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'][0]['id'], 77)
        self.assertEqual(response.data['results'][0]['slug'], 'org-77')

    def test_anonymous_is_rejected(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/organizations/')
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

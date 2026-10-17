from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken

from tasks.authentication import FederatedUser, ShizuhaIDAuthentication


class ShizuhaIDAuthenticationFallbackTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.auth = ShizuhaIDAuthentication()
        self.local_user = get_user_model().objects.create_user(
            id=42,
            username='local-collision',
            email='local-collision@pulse.local',
        )

    def _request_for_claims(self, **claims):
        token = AccessToken()
        for key, value in claims.items():
            token[key] = value
        return self.factory.get(
            '/api/items/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_email_claim_returns_federated_user_before_local_pk_lookup(self):
        request = self._request_for_claims(
            user_id=self.local_user.id,
            email='human@shizuha.com',
            username='human',
            organization_memberships={'1': 'member'},
        )

        user, _ = self.auth.authenticate(request)

        self.assertIsInstance(user, FederatedUser)
        self.assertEqual(user.id, self.local_user.id)
        self.assertEqual(user.email, 'human@shizuha.com')
        self.assertNotEqual(user.email, self.local_user.email)
        self.assertEqual(user.organization_memberships, {1: 'member'})

    def test_no_email_legacy_token_can_still_use_local_user(self):
        request = self._request_for_claims(
            user_id=self.local_user.id,
            username='legacy-local',
            organization_memberships={'1': 'admin'},
        )

        user, _ = self.auth.authenticate(request)

        self.assertEqual(user.pk, self.local_user.pk)
        self.assertEqual(user.email, self.local_user.email)
        self.assertEqual(user.organization_memberships, {1: 'admin'})

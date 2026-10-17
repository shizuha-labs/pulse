"""PLS-632: access-expiry mid-session refresh must work for federated ID users.

Pulse authenticates Shizuha-ID users as FederatedUser objects built from token
claims (tasks.authentication.ShizuhaJWTAuthentication); those users are NOT
guaranteed to exist in Pulse's local auth.User table, and shizuha-id user_ids
collide with local auto-increment PKs. The stock SimpleJWT TokenRefreshSerializer
performs ``get_user_model().objects.get(id=user_id)`` on refresh, which 401s
(inactive colliding local user) or 500s (no local row) for a perfectly valid
ID-issued refresh token. The federated refresh view must validate + rotate the
ID-issued refresh token WITHOUT a local user row, while preserving the
invalid/revoked-refresh fail-closed behavior.
"""
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken


class PLS632FederatedRefreshTests(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def _federated_refresh_token(self, user_id=999999):
        """Mint a refresh token for a user_id absent from the local DB.

        Mirrors the ID-issued refresh token shape: signed with the shared
        signing key, carries user_id/email/username claims, but the user does
        not exist in Pulse's local auth.User table (federated identity).
        """
        refresh = RefreshToken()
        refresh['user_id'] = user_id
        refresh['email'] = 'federated@example.com'
        refresh['username'] = 'federated-user'
        return str(refresh)

    def test_refresh_succeeds_for_federated_user_without_local_row(self):
        """A valid ID-issued refresh token must silently mint a new access token."""
        token = self._federated_refresh_token()
        response = self.client.post('/api/auth/refresh/', {'refresh': token}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertTrue(response.data['access'])

    def test_refresh_rotates_and_blacklists_old_token(self):
        """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION still apply."""
        token = self._federated_refresh_token()
        response = self.client.post('/api/auth/refresh/', {'refresh': token}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('refresh', response.data)
        # The old token must be blacklisted after rotation: a second use 401s.
        second = self.client.post('/api/auth/refresh/', {'refresh': token}, format='json')
        self.assertEqual(second.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_refresh_still_fails_closed(self):
        """Malformed/forged refresh tokens remain rejected (fail-closed)."""
        response = self.client.post(
            '/api/auth/refresh/', {'refresh': 'garbage.token.value'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_refresh_still_400(self):
        """Missing refresh field keeps the serializer's required-field behavior."""
        response = self.client.post('/api/auth/refresh/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inactive_local_user_refresh_denied(self):
        """Review finding: the offboarding control is restored conditionally —
        a deactivated LOCAL user holding a live refresh token must 401, not
        silently refresh until token expiry."""
        from django.contrib.auth import get_user_model
        user_model = get_user_model()
        row = user_model.objects.create_user(
            username='offboarded-local', email='offboarded@example.com', password='x',
        )
        row.is_active = False
        row.save(update_fields=['is_active'])
        refresh = RefreshToken()
        refresh['user_id'] = row.id
        refresh['email'] = row.email
        response = self.client.post('/api/auth/refresh/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_active_local_user_refresh_allowed(self):
        """The conditional form: an ACTIVE local user refreshes normally —
        the offboarding control must not over-deny active accounts."""
        from django.contrib.auth import get_user_model
        user_model = get_user_model()
        row = user_model.objects.create_user(
            username='active-local', email='active-local@example.com', password='x',
        )
        refresh = RefreshToken()
        refresh['user_id'] = row.id
        refresh['email'] = row.email
        response = self.client.post('/api/auth/refresh/', {'refresh': str(refresh)}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

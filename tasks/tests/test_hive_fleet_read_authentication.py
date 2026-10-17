"""Exercise Hive's real HTTP snapshot reads with signed M2M credentials."""

import time
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings
import jwt
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from tasks.fleet_read_authentication import CLIENT_ID, READ_SCOPE, HiveFleetReader
from tasks.models import TeamRoster
from tasks.token_revocation import _REVOCATION_CACHE
from tasks.views import OrgChartSnapshotView


@override_settings(PULSE_FLEET_READ_ISSUER='https://id.test', SHIZUHA_AUD_ENFORCE=False)
class HiveFleetReadAuthenticationTests(APITestCase):
    roster_url = '/api/admin/team-roster/'
    snapshot_url = '/api/admin/org-chart/snapshot/'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        _REVOCATION_CACHE.clear()
        self.addCleanup(_REVOCATION_CACHE.clear)
        self.jwks = patch('tasks.authentication._jwks_fetch_keys', return_value={
            'fleet-test': self.signing_key.public_key(),
        }).start()
        self.revocation = patch('tasks.token_revocation._lookup_revocation', return_value={
            'active': True,
        }).start()
        self.addCleanup(patch.stopall)
        patch.object(OrgChartSnapshotView, '_fetch_admin_teams', return_value=[]).start()
        patch.object(OrgChartSnapshotView, '_get_availability_map', return_value={}).start()
        patch('tasks.services.admin_service.admin_service.get_organization_by_slug',
              return_value=None).start()
        for slug, org_id in [('shizuha', 1), ('shizuha-trading', 3)]:
            TeamRoster.objects.update_or_create(
                team_name='fleet-read-test', organization_slug=slug,
                defaults={'organization_id': org_id, 'tiers': [], 'is_active': True},
            )

    def token(self, **overrides):
        now = int(time.time())
        claims = {
            'iss': 'https://id.test', 'aud': ['pulse'],
            'sub': f'service:{CLIENT_ID}', 'client_id': CLIENT_ID,
            'is_service': True, 'scope': READ_SCOPE, 'token_type': 'access',
            'iat': now, 'exp': now + 900, 'jti': uuid.uuid4().hex,
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, self.signing_key, algorithm='RS256',
                          headers={'kid': 'fleet-test'})

    def request(self, method='get', path=None, token=None, **claims):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + (token or self.token(**claims)))
        return getattr(self.client, method)(path or self.roster_url,
                                          {'organization_slug': 'all'}, format='json')

    def test_controller_reads_both_snapshots_across_organizations_without_staff(self):
        # Mirror the real controller's roster -> workload snapshot sequence.
        # Repeating with a freshly minted token proves renewal needs no user or
        # saved refresh token, and exercises the real authentication boundary.
        for _ in range(2):
            token = self.token()
            roster = self.request(token=token)
            self.assertEqual(roster.status_code, 200, roster.data)
            self.assertEqual({r['organization_slug'] for r in roster.data['teams']
                              if r['team_name'] == 'fleet-read-test'},
                             {'shizuha', 'shizuha-trading'})
            actor = roster.wsgi_request.user
            self.assertIsInstance(actor, HiveFleetReader)
            self.assertFalse(actor.is_staff)
            self.assertFalse(actor.is_superuser)
            snapshot = self.request(path=self.snapshot_url, token=token)
            self.assertEqual(snapshot.status_code, 200, snapshot.data)
            self.assertIn('workload', snapshot.data)
            self.assertIn('team_workload', snapshot.data)
            self.assertEqual({r['organization_slug'] for r in snapshot.data['teams']
                              if r['name'] == 'fleet-read-test'},
                             {'shizuha', 'shizuha-trading'})

    def test_expired_token_and_missing_required_claims_are_rejected(self):
        for claims in ({'exp': int(time.time()) - 1}, {'exp': None},
                       {'iat': None}, {'jti': ''}):
            with self.subTest(claims=claims):
                self.assertEqual(self.request(**claims).status_code, 401)

    def test_wrong_grant_issuer_audience_and_identity_are_rejected(self):
        for claims in (
            {'iss': 'https://other-id.test'}, {'iss': None},
            {'aud': ['hive']}, {'aud': '*'}, {'aud': ['pulse', 'hive']}, {'aud': None},
            {'scope': 'pulse:*'}, {'scope': READ_SCOPE + ' pulse:items.write'},
            {'scope': ''}, {'client_id': 'other-service'},
            {'sub': 'service:other-service'}, {'is_service': False},
            {'is_service': 'true'}, {'token_type': 'refresh'},
        ):
            with self.subTest(claims=claims):
                self.assertEqual(self.request(**claims).status_code, 401)

    def test_revoked_or_unavailable_revocation_lookup_fails_closed(self):
        for result in ({'active': False}, None):
            with self.subTest(result=result):
                self.revocation.return_value = result
                self.assertEqual(self.request().status_code, 401)

    def test_invalid_signature_and_missing_jwks_fail_closed(self):
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwks.return_value = {'fleet-test': wrong_key.public_key()}
        self.assertEqual(self.request().status_code, 401)
        self.jwks.return_value = {}
        self.assertEqual(self.request().status_code, 401)

    def test_hs256_service_token_is_rejected(self):
        token = AccessToken()
        token['is_service'] = True
        token['sub'] = f'service:{CLIENT_ID}'
        token['client_id'] = CLIENT_ID
        self.assertEqual(self.request(token=str(token)).status_code, 401)

    def test_service_cannot_mutate_roster_or_use_other_endpoints(self):
        before = list(TeamRoster.objects.values_list('pk', 'version'))
        for method, path in (
            ('post', self.roster_url), ('post', self.snapshot_url),
            ('patch', '/api/admin/team-roster/fleet-read-test/'),
            ('delete', '/api/admin/team-roster/fleet-read-test/'),
            ('get', '/api/admin/team-roster/members/'),
            ('post', '/api/items/'),
        ):
            with self.subTest(method=method, path=path):
                response = self.request(method=method, path=path,
                                        user_id=1, is_staff=True, is_superuser=True)
                self.assertIn(response.status_code, (401, 403), response.data)
        self.assertEqual(list(TeamRoster.objects.values_list('pk', 'version')), before)

    def test_existing_staff_and_nonstaff_user_authorization_is_preserved(self):
        for staff in (True, False):
            token = AccessToken()
            token['user_id'] = 990001
            token['email'] = 'snapshot-test@example.com'
            token['is_staff'] = staff
            token['organization_memberships'] = {}
            expected = 200 if staff else 403
            for path in (self.roster_url, self.snapshot_url):
                with self.subTest(staff=staff, path=path):
                    self.assertEqual(self.request(path=path, token=str(token)).status_code,
                                     expected)
            if staff:
                # An empty staff POST reaches existing payload validation;
                # service-only method restrictions have not disabled staff CRUD.
                self.assertEqual(self.request(method='post', token=str(token)).status_code, 400)

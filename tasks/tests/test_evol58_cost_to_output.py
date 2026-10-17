"""EVOL-58: per-agent cost-to-output ratio join in the daily attribution aggregate.

Two layers:
- module unit tests on tasks.cortex_cost (mint claims per STRIDE-lite D1, fail-soft
  D5, response parsing) — no DB/HTTP.
- integration tests on AgentOutputAttributionAuditView (LEFT-JOIN on user_id, ratio
  math, no-cost path, fail-soft-still-200) with the Cortex pull mocked.
"""
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks import cortex_cost
from tasks.models import Comment, Item, ItemActivity, Project, TeamRoster

User = get_user_model()


class _TokenResp:
    """Minimal ID token-endpoint response stub (client_credentials exchange)."""

    def __init__(self, token='id-issued-rs256-access-token', expires_in=900):
        self._token = token
        self._expires_in = expires_in
        self.content = b'{}'

    def raise_for_status(self):
        pass

    def json(self):
        return {'access_token': self._token, 'expires_in': self._expires_in}


class CortexCostClientUnitTests(APITestCase):
    CLIENT_ID = 'pulse-cortex-cost'
    CLIENT_SECRET = 'evol58-test-client-secret'

    def setUp(self):
        cortex_cost._TOKEN_CACHE.update({'token': None, 'expires_at': 0.0})

    @override_settings(
        SHIZUHA_ID_OAUTH_TOKEN_URL='http://id.example.test/api/oauth/token',
        PULSE_CORTEX_OAUTH_CLIENT_ID=CLIENT_ID,
        PULSE_CORTEX_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
    )
    def test_exchange_requests_scoped_client_credentials_token(self):
        # EVOL-64: the ONLY service-token path is the ID client_credentials
        # exchange — never a local HS256 mint. Assert the exact request shape.
        def _post(url, data=None, **kwargs):
            self.assertEqual(url, 'http://id.example.test/api/oauth/token')
            self.assertEqual(data['grant_type'], 'client_credentials')
            self.assertEqual(data['client_id'], self.CLIENT_ID)
            self.assertEqual(data['client_secret'], self.CLIENT_SECRET)
            self.assertEqual(data['scope'], 'cortex:usage-by-user.read')
            return _TokenResp('id-issued-rs256-access-token', 900)

        with patch.object(cortex_cost.requests, 'post', side_effect=_post) as mpost:
            token = cortex_cost._exchange_cortex_service_token()

        self.assertEqual(token, 'id-issued-rs256-access-token')
        self.assertEqual(mpost.call_count, 1)
        # cached within TTL -> second call does not re-exchange
        with patch.object(cortex_cost.requests, 'post', side_effect=_post) as mpost2:
            again = cortex_cost._exchange_cortex_service_token()
        self.assertEqual(again, 'id-issued-rs256-access-token')
        self.assertEqual(mpost2.call_count, 0)

    def test_exchange_fails_closed_when_client_secret_missing(self):
        with override_settings(
            PULSE_CORTEX_OAUTH_CLIENT_ID=self.CLIENT_ID,
            PULSE_CORTEX_OAUTH_CLIENT_SECRET='',
        ):
            with self.assertRaises(cortex_cost.CortexAuthUnavailable):
                cortex_cost._exchange_cortex_service_token()

    def test_no_hs256_mint_path_remains(self):
        # Structural guard: Pulse no longer SIGNS a JWT with the platform shared
        # key. The docstring may narrate the retired bridge, but the mint/
        # exchange implementation must contain no JWT-encode/signing-key API.
        import inspect
        source = inspect.getsource(cortex_cost._exchange_cortex_service_token)
        self.assertNotIn('pyjwt', source)
        self.assertNotIn('jwt.encode', source)
        self.assertNotIn('algorithm=', source)
        self.assertNotIn('SIGNING_KEY', source)
        self.assertNotIn('SIMPLE_JWT', source)
        # config reads never reach the shared JWT secrets
        self.assertNotIn('SECRET_KEY', source)

    @override_settings(
        PULSE_CORTEX_OAUTH_CLIENT_ID=CLIENT_ID,
        PULSE_CORTEX_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        TESTING=False,
    )
    def test_fetch_parses_by_user_keyed_by_int_user_id(self):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    'generated_at': '2026-07-20T00:00:00+00:00',
                    'since': '2026-07-19T00:00:00+00:00',
                    'by_user': [
                        {'user_id': 101, 'cost_microdollars': 500000, 'cost_usd': 0.5, 'requests': 10},
                        {'user_id': None, 'cost_microdollars': 1, 'cost_usd': 0.0, 'requests': 1},
                    ],
                }

        with patch.object(cortex_cost.requests, 'get', return_value=_Resp()) as mget, \
             patch.object(cortex_cost.requests, 'post', return_value=_TokenResp('id-issued-rs256-access-token', 900)):
            by_user, prov = cortex_cost.fetch_cortex_cost_by_user(7)
        self.assertEqual(set(by_user), {101})          # None user_id dropped
        self.assertEqual(by_user[101]['cost_usd'], 0.5)
        self.assertEqual(prov['window_days'], 7)
        self.assertEqual(prov['row_count'], 1)
        self.assertNotIn('error', prov)
        # days flows through to the request
        self.assertEqual(mget.call_args.kwargs['params'], {'days': 7})
        # the Cortex bearer is the ID-issued RS256 service token
        self.assertEqual(
            mget.call_args.kwargs['headers']['Authorization'],
            'Bearer id-issued-rs256-access-token',
        )

    @override_settings(
        PULSE_CORTEX_OAUTH_CLIENT_ID=CLIENT_ID,
        PULSE_CORTEX_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        TESTING=False,
    )
    def test_fetch_is_fail_soft_on_transport_error(self):
        with patch.object(cortex_cost.requests, 'get', side_effect=requests.ConnectionError('down')), \
             patch.object(cortex_cost.requests, 'post', return_value=_TokenResp('t', 900)):
            by_user, prov = cortex_cost.fetch_cortex_cost_by_user(1)
        self.assertEqual(by_user, {})
        self.assertIn('error', prov)          # D5: never raises
        # the failure is at the Cortex fetch boundary, not token mint.
        self.assertEqual(prov['error_stage'], 'cortex_fetch')

    @override_settings(
        PULSE_CORTEX_OAUTH_CLIENT_ID=CLIENT_ID,
        PULSE_CORTEX_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        TESTING=False,
    )
    def test_cortex_401_is_distinct_cortex_fetch_stage(self):
        # A wrong-scope/audience 401 from Cortex is a fetch failure, NOT a
        # token-mint failure — provenance must tell them apart.
        def _unauthorized(*args, **kwargs):
            raise requests.HTTPError('401 Client Error: Unauthorized')

        with patch.object(cortex_cost.requests, 'get', side_effect=_unauthorized), \
             patch.object(cortex_cost.requests, 'post', return_value=_TokenResp('t', 900)):
            _by_user, prov = cortex_cost.fetch_cortex_cost_by_user(7)
        self.assertEqual(prov['error_stage'], 'cortex_fetch')
        self.assertIn('401', prov['error'])

    @override_settings(
        PULSE_CORTEX_OAUTH_CLIENT_ID=CLIENT_ID,
        PULSE_CORTEX_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        TESTING=False,
    )
    def test_token_mint_failure_is_distinct_token_mint_stage(self):
        # ID unreachable/bad credentials -> token_mint stage, fail-soft.
        with patch.object(cortex_cost.requests, 'post', side_effect=requests.ConnectionError('id down')):
            _by_user, prov = cortex_cost.fetch_cortex_cost_by_user(7)
        self.assertEqual(prov['error_stage'], 'token_mint')
        self.assertIn('error', prov)
        # the client secret must never leak into fail-soft metadata
        self.assertNotIn(self.CLIENT_SECRET, str(prov))

    def test_settings_binds_cortex_env_vars(self):
        # Regression PLAT-5014 + EVOL-64: cortex_cost reads its config off
        # Django settings (getattr). If settings never binds them from
        # os.environ, the deploy env vars are a no-op and cost stays null.
        from django.conf import settings as dj_settings
        self.assertTrue(hasattr(dj_settings, 'SHIZUHA_CORTEX_API_URL'))
        self.assertTrue(dj_settings.SHIZUHA_CORTEX_API_URL)
        self.assertTrue(hasattr(dj_settings, 'SHIZUHA_ID_OAUTH_TOKEN_URL'))
        self.assertTrue(hasattr(dj_settings, 'PULSE_CORTEX_OAUTH_CLIENT_ID'))
        self.assertTrue(hasattr(dj_settings, 'PULSE_CORTEX_OAUTH_CLIENT_SECRET'))
        # the retired shared-secret bridge config is gone
        self.assertFalse(hasattr(dj_settings, 'SHIZUHA_CORTEX_SERVICE_USER_ID'))

    @override_settings(SHIZUHA_CORTEX_API_URL='http://cortex.example.test:8040')
    def test_fetch_targets_configured_cortex_url(self):
        # The configured URL must reach the actual call target: provenance records
        # `cortex_url` from settings before the hermetic-TESTING short-circuit, so a
        # misconfigured/unbound URL would surface here as the wrong base.
        _by_user, prov = cortex_cost.fetch_cortex_cost_by_user(7)
        self.assertEqual(prov['cortex_url'], 'http://cortex.example.test:8040')


@override_settings(SIMPLE_JWT={'SIGNING_KEY': 'evol58-test-signing-key'})
class CostToOutputJoinIntegrationTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='auditor', email='auditor@example.test', password='x', is_staff=True,
        )
        self.client.force_authenticate(user=self.staff)
        self.project = Project.objects.create(org_id=1, key='EVT', name='Evolution')
        self.item = Item.objects.create(project=self.project, title='t', created_by_id=1, workflow_name='simple')
        TeamRoster.objects.update_or_create(
            team_name='engineering', defaults={'tiers': [['alice@shizuha.com']]},
        )
        # alice: user_id 101, 1 comment + 1 transition = total_output 2.
        Comment.objects.create(item=self.item, author_id=101, author_email='alice@shizuha.com',
                               author_name='alice', content='x')
        ItemActivity.objects.create(item=self.item, action='status_changed', user_id=101,
                                    user_email='alice@shizuha.com', old_value='open', new_value='done')
        # bob: user_id 202, 1 comment = total_output 1, but Cortex has no cost row for bob.
        Comment.objects.create(item=self.item, author_id=202, author_email='bob@shizuha.com',
                               author_name='bob', content='y')

    def _get(self):
        return self.client.get('/api/audit/agent-output-attribution/', {'organization_id': 1})

    def test_joins_cost_and_computes_ratio(self):
        cost = ({101: {'user_id': 101, 'cost_microdollars': 500000, 'cost_usd': 0.5, 'requests': 10}},
                {'source': 'cortex.usage.by_user', 'window_days': 1, 'generated_at': 'ts'})
        with patch('tasks.cortex_cost.fetch_cortex_cost_by_user', return_value=cost):
            resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = {r['email']: r for r in resp.data['agents']}
        # alice: cost joined on user_id, ratio = 0.5 / 2 = 0.25
        self.assertEqual(rows['alice@shizuha.com']['user_id'], 101)
        self.assertEqual(rows['alice@shizuha.com']['cost']['cost_usd'], 0.5)
        self.assertEqual(rows['alice@shizuha.com']['cost_to_output_ratio'], 0.25)
        # bob: no Cortex cost row -> cost null, ratio null (no invented estimate)
        self.assertIsNone(rows['bob@shizuha.com']['cost'])
        self.assertIsNone(rows['bob@shizuha.com']['cost_to_output_ratio'])
        # provenance surfaced
        self.assertEqual(resp.data['cost_provenance']['source'], 'cortex.usage.by_user')

    def test_fail_soft_cortex_error_still_returns_200_with_null_cost(self):
        cost = ({}, {'source': 'cortex.usage.by_user', 'window_days': 1, 'error': 'timeout'})
        with patch('tasks.cortex_cost.fetch_cortex_cost_by_user', return_value=cost):
            resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)   # D5: aggregate never 500s
        rows = {r['email']: r for r in resp.data['agents']}
        self.assertIsNone(rows['alice@shizuha.com']['cost'])
        self.assertIsNone(rows['alice@shizuha.com']['cost_to_output_ratio'])
        self.assertEqual(resp.data['cost_provenance']['error'], 'timeout')

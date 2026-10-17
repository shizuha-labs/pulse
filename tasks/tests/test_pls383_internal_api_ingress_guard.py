"""Internal ingress and workload-identity probe regression tests.

Verifies that /api/internal/* endpoints return 404 for ingress-proxied
requests (X-Forwarded-For / X-Real-IP present) and pass through normally
for in-cluster direct calls (no forwarding headers).
"""
import base64
import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve

from tasks.middleware import InternalApiIngressGuardMiddleware
from tasks.probe_views import (
    EXPECTED_AUDIENCE,
    EXPECTED_USERNAME,
    InvalidProbeToken,
    ProbeAuthUnavailable,
    _requests_by_source,
    _reset_probe_state_for_tests,
    _source_is_rate_limited,
    _token_review as perform_token_review,
    _validate_bounded_pod_claims,
    process_liveness_probe,
    render_probe_auth_metrics,
)


def _make_request(path, x_forwarded_for=None, x_real_ip=None):
    request = MagicMock()
    request.path = path
    request.META = {}
    if x_forwarded_for:
        request.META['HTTP_X_FORWARDED_FOR'] = x_forwarded_for
    if x_real_ip:
        request.META['HTTP_X_REAL_IP'] = x_real_ip
    return request


class InternalApiIngressGuardUnitTests(SimpleTestCase):
    """Unit tests for the middleware guard — no DB required."""

    def _guard(self, path, x_forwarded_for=None, x_real_ip=None):
        request = _make_request(path, x_forwarded_for=x_forwarded_for, x_real_ip=x_real_ip)
        sentinel = object()
        get_response = MagicMock(return_value=sentinel)
        mw = InternalApiIngressGuardMiddleware(get_response)
        result = mw(request)
        return result, get_response

    def test_x_forwarded_for_on_internal_path_gets_404(self):
        result, get_response = self._guard(
            '/api/internal/user-lifecycle-events/',
            x_forwarded_for='203.0.113.1',
        )
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 404)
        get_response.assert_not_called()

    def test_x_real_ip_on_internal_path_gets_404(self):
        result, get_response = self._guard(
            '/api/internal/user-lifecycle-events/',
            x_real_ip='203.0.113.1',
        )
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 404)
        get_response.assert_not_called()

    def test_pulse_prefix_internal_path_gets_404(self):
        result, get_response = self._guard(
            '/shizuha-pulse/api/internal/users/42/unassign/',
            x_forwarded_for='10.0.0.1',
        )
        self.assertIsInstance(result, JsonResponse)
        self.assertEqual(result.status_code, 404)
        get_response.assert_not_called()

    def test_in_cluster_request_without_forwarding_headers_passes_guard(self):
        """No forwarding headers → guard passes; downstream view handles auth."""
        result, get_response = self._guard('/api/internal/user-lifecycle-events/')
        get_response.assert_called_once()

    def test_non_internal_path_with_forwarding_headers_passes_guard(self):
        """Public API path with X-Forwarded-For must not be blocked."""
        result, get_response = self._guard(
            '/api/items/',
            x_forwarded_for='203.0.113.1',
        )
        get_response.assert_called_once()

    def test_both_forwarding_headers_on_internal_path_gets_404(self):
        result, get_response = self._guard(
            '/api/internal/user-lifecycle-events/',
            x_forwarded_for='10.0.0.5',
            x_real_ip='10.0.0.5',
        )
        self.assertEqual(result.status_code, 404)
        get_response.assert_not_called()


class ProbeListenerBootTests(SimpleTestCase):
    def test_isolated_probe_settings_boot_in_fresh_process(self):
        env = dict(os.environ)
        env['DJANGO_SETTINGS_MODULE'] = 'taskmanager.probe_settings'
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                (
                    'import django; django.setup(); '
                    'from django.conf import settings; '
                    'assert settings.PULSE_BOOTSTRAP_PERIODIC_SCHEDULES is False'
                ),
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


def _probe_token(*, lifetime=600, expires_offset=600, namespace='monitoring', service_account='prod-ui-probe', pod=True):
    now = int(time.time())
    kubernetes_claims = {
        'namespace': namespace,
        'serviceaccount': {'name': service_account, 'uid': 'sa-uid'},
    }
    if pod:
        kubernetes_claims['pod'] = {'name': 'prod-ui-probe-123', 'uid': 'pod-uid'}
    payload = {
        'iat': now + expires_offset - lifetime,
        'exp': now + expires_offset,
        # Intentionally not used for authorization; TokenReview's returned
        # compatible audience is the authority.
        'aud': [EXPECTED_AUDIENCE],
        'kubernetes.io': kubernetes_claims,
    }

    def encode(value):
        raw = json.dumps(value, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

    return f'{encode({"alg": "none"})}.{encode(payload)}.signature'


@override_settings(ROOT_URLCONF='taskmanager.probe_urls', FORCE_SCRIPT_NAME=None)
class WorkloadIdentityProbeTests(TestCase):
    """PLAT-4601 exact identity/audience/expiry/failure contract."""

    def setUp(self):
        _reset_probe_state_for_tests()
        self.token = _probe_token()
        self.token_review_status = {
            'authenticated': True,
            'audiences': [EXPECTED_AUDIENCE],
            'user': {'username': EXPECTED_USERNAME},
        }

    def _get(self, token=None, **extra):
        return self.client.get(
            '/api/internal/probes/db-readiness/',
            HTTP_AUTHORIZATION=f'Bearer {token or self.token}',
            REMOTE_ADDR='10.42.1.20',
            **extra,
        )

    @patch('tasks.probe_views._token_review')
    def test_exact_identity_performs_bounded_db_read_and_returns_no_business_data(self, token_review):
        token_review.return_value = self.token_review_status
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'status': 'ok', 'database': 'ok', 'auth': 'workload_identity'},
        )
        token_review.assert_called_once_with(self.token)

    @patch('tasks.probe_views._token_review')
    def test_wrong_authenticated_identity_is_forbidden(self, token_review):
        token_review.return_value = {
            **self.token_review_status,
            'user': {'username': 'system:serviceaccount:monitoring:someone-else'},
        }

        response = self._get()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'probe_identity_forbidden')

    @patch('tasks.probe_views._token_review')
    def test_missing_compatible_returned_audience_is_forbidden(self, token_review):
        token_review.return_value = {**self.token_review_status, 'audiences': ['kubernetes']}

        response = self._get()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'probe_identity_forbidden')

    @patch('tasks.probe_views._token_review')
    def test_tokenreview_rejection_after_pod_deletion_is_unauthorized(self, token_review):
        # Kubernetes invalidates Pod-bound tokens after its documented bounded
        # deletion grace. The verifier must honor that authoritative false.
        token_review.return_value = {'authenticated': False}

        response = self._get()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error'], 'invalid_probe_token')

    @patch('tasks.probe_views._token_review')
    def test_expired_token_is_rejected_before_token_review(self, token_review):
        response = self._get(_probe_token(lifetime=600, expires_offset=-1))

        self.assertEqual(response.status_code, 401)
        token_review.assert_not_called()

    @patch('tasks.probe_views._token_review')
    def test_effective_token_lifetime_is_bounded_from_claims(self, token_review):
        response = self._get(_probe_token(lifetime=3600, expires_offset=600))

        self.assertEqual(response.status_code, 401)
        token_review.assert_not_called()

    @patch('tasks.probe_views._token_review')
    def test_token_must_be_pod_bound(self, token_review):
        response = self._get(_probe_token(pod=False))

        self.assertEqual(response.status_code, 401)
        token_review.assert_not_called()

    @patch('tasks.probe_views._token_review')
    def test_unapproved_local_namespace_or_service_account_rejects_before_token_review(self, token_review):
        wrong_namespace = self._get(_probe_token(namespace='someone-else'))
        wrong_service_account = self._get(_probe_token(service_account='someone-else'))

        self.assertEqual(wrong_namespace.status_code, 401)
        self.assertEqual(wrong_service_account.status_code, 401)
        token_review.assert_not_called()

    @patch('tasks.probe_views._token_review', side_effect=ProbeAuthUnavailable)
    def test_token_review_outage_is_typed_distinctly(self, _token_review):
        response = self._get()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'probe_auth_unavailable')

    @patch('tasks.probe_views._bounded_db_read', side_effect=RuntimeError('db-secret-detail'))
    @patch('tasks.probe_views._token_review')
    def test_database_failure_is_typed_and_sanitized(self, token_review, _db_read):
        token_review.return_value = self.token_review_status

        response = self._get()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'database_unavailable')
        self.assertNotIn(b'db-secret-detail', response.content)

    @patch('tasks.probe_views._token_review')
    def test_method_body_and_oversized_header_reject_before_token_review(self, token_review):
        post = self.client.post(
            '/api/internal/probes/db-readiness/',
            data=b'{}',
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )
        body = self._get(CONTENT_LENGTH='1')
        header = self._get(token='x' * 8200)

        self.assertEqual(post.status_code, 405)
        self.assertEqual(body.status_code, 400)
        self.assertEqual(header.status_code, 431)
        token_review.assert_not_called()

    @patch('tasks.probe_views._token_review')
    @patch('tasks.probe_views.RATE_LIMIT_REQUESTS', 1)
    def test_rate_limit_rejects_before_a_second_token_review(self, token_review):
        token_review.return_value = self.token_review_status

        first = self._get()
        second = self._get()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        token_review.assert_called_once()

    @patch('tasks.probe_views.RATE_LIMIT_REQUESTS', 2)
    @patch('tasks.probe_views.RATE_LIMIT_WINDOW_SECONDS', 60)
    def test_rate_limit_is_source_isolated_and_clock_evicts_stale_pod_ips(self):
        self.assertFalse(_source_is_rate_limited('10.42.1.20', now=100))
        self.assertFalse(_source_is_rate_limited('10.42.1.20', now=101))
        self.assertTrue(_source_is_rate_limited('10.42.1.20', now=102))

        self.assertFalse(_source_is_rate_limited('10.42.1.21', now=103))
        self.assertFalse(_source_is_rate_limited('10.42.1.21', now=104))
        self.assertEqual({key: list(value) for key, value in _requests_by_source.items()}, {
            '10.42.1.20': [100, 101],
            '10.42.1.21': [103, 104],
        })

        self.assertFalse(_source_is_rate_limited('10.42.99.99', now=165))
        self.assertEqual({key: list(value) for key, value in _requests_by_source.items()}, {
            '10.42.99.99': [165],
        })

    @patch('tasks.probe_views.MAX_RATE_LIMIT_SOURCES', 2)
    @patch('tasks.probe_views.RATE_LIMIT_REQUESTS', 2)
    @patch('tasks.probe_views.RATE_LIMIT_WINDOW_SECONDS', 60)
    def test_rate_limit_source_map_is_bounded_lru(self):
        self.assertFalse(_source_is_rate_limited('10.42.1.20', now=100))
        self.assertFalse(_source_is_rate_limited('10.42.1.21', now=101))
        self.assertFalse(_source_is_rate_limited('10.42.1.22', now=102))

        self.assertEqual(list(_requests_by_source), ['10.42.1.21', '10.42.1.22'])

    @patch('tasks.probe_views._token_review')
    @patch('tasks.probe_views._concurrency.acquire', return_value=False)
    def test_concurrency_limit_rejects_before_token_review(self, _acquire, token_review):
        response = self._get()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'probe_busy')
        token_review.assert_not_called()

    @patch('tasks.probe_views._token_review')
    def test_bearer_is_replayable_only_within_bounded_validity(self, token_review):
        token_review.return_value = self.token_review_status

        self.assertEqual(self._get().status_code, 200)
        self.assertEqual(self._get().status_code, 200)
        with self.assertRaises(InvalidProbeToken):
            _validate_bounded_pod_claims(_probe_token(expires_offset=-1))

    @patch('tasks.probe_views._token_review')
    def test_metrics_have_fixed_results_and_never_expose_token(self, token_review):
        token_review.return_value = {'authenticated': False}
        self._get()

        metrics = render_probe_auth_metrics()
        self.assertIn('result="authentication_rejected"', metrics)
        self.assertNotIn(self.token, metrics)
        self.assertNotIn('10.42.1.20', metrics)

    @patch('tasks.probe_views.ssl.create_default_context')
    @patch('tasks.probe_views._read_bounded', return_value='verifier-token')
    @patch('tasks.probe_views.urllib.request.urlopen')
    def test_tokenreview_request_uses_exact_audience_and_sanitizes_transport_errors(
        self,
        urlopen,
        _read_verifier_token,
        _ssl_context,
    ):
        response = MagicMock()
        response.status = 201
        response.read.return_value = json.dumps({'status': self.token_review_status}).encode()
        response.__enter__.return_value = response
        urlopen.return_value = response

        status = perform_token_review(self.token)

        self.assertEqual(status, self.token_review_status)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload['spec']['audiences'], [EXPECTED_AUDIENCE])
        self.assertEqual(payload['spec']['token'], self.token)

        urlopen.side_effect = OSError(f'transport echoed {self.token}')
        with self.assertRaises(ProbeAuthUnavailable) as raised:
            perform_token_review(self.token)
        self.assertNotIn(self.token, str(raised.exception))


class ProbeListenerIsolationTests(SimpleTestCase):
    def test_liveness_path_is_absent_from_public_urlconf(self):
        with self.assertRaises(Resolver404):
            resolve('/livez/', urlconf='taskmanager.urls')

    def test_liveness_path_exists_only_on_dedicated_urlconf(self):
        match = resolve('/livez/', urlconf='taskmanager.probe_urls')
        self.assertEqual(match.url_name, 'probe-process-liveness')

    def test_liveness_probe_is_static_and_rejects_non_get(self):
        factory = RequestFactory()
        get_response = process_liveness_probe(factory.get('/livez/'))
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(
            json.loads(get_response.content),
            {'status': 'ok', 'listener': 'probe'},
        )

        post_response = process_liveness_probe(factory.post('/livez/'))
        self.assertEqual(post_response.status_code, 405)
        self.assertEqual(post_response['Allow'], 'GET')

    def test_probe_path_is_absent_from_public_urlconf(self):
        with self.assertRaises(Resolver404):
            resolve('/api/internal/probes/db-readiness/', urlconf='taskmanager.urls')

    def test_probe_path_exists_only_on_dedicated_urlconf(self):
        match = resolve('/api/internal/probes/db-readiness/', urlconf='taskmanager.probe_urls')
        self.assertEqual(match.url_name, 'probe-db-readiness')

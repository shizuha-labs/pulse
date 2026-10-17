"""PLAT-9491 — dispatch-path scope-manifest assertion stamps (unit tests).

Covers the acceptance triad: success, skip-on-timeout, divergence-stamp —
plus the inert-by-default contract (evidence, never a gate). SimpleTestCase:
no DB writes anywhere in the stamp path (the reservation persistence is a
plain AddField migration + create() kwarg exercised by CI's WIP-admit lane).
"""
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import SimpleTestCase

from tasks.services import scope_assertion as sa
from tasks.services.workflow_service import WorkflowService

ENABLED_ENV = {
    'PULSE_SCOPE_ASSERTION_ENABLED': '1',
    'PULSE_HIVE_SCOPE_ASSERTION_URL': 'http://hive.internal/scope-assertion/',
    'PULSE_HIVE_SCOPE_ASSERTION_TOKEN': 'tok',
}


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {'result': 'ok', 'tags': {}}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _fake_requests(post_side_effect=None, response=None):
    """A requests-module double: post() → response or raises; Timeout class present."""
    class _Timeout(Exception):
        pass

    class _Exceptions:
        Timeout = _Timeout

    def post(*args, **kwargs):
        if post_side_effect is not None:
            if isinstance(post_side_effect, Exception):
                raise post_side_effect
            return post_side_effect
        return response

    mod = mock.Mock()
    mod.exceptions = _Exceptions()
    mod.Timeout = _Timeout
    mod.post.side_effect = post
    return mod


class ScopeAssertionStampTests(SimpleTestCase):
    TAGS = {'github:merge', 'ci-logs'}

    def test_disabled_by_default_is_inert(self):
        stamp = sa.build_scope_assertion_stamp('jun@shizuha.com', self.TAGS, environ={})
        self.assertEqual(stamp['status'], sa.STAMP_SKIPPED)
        self.assertEqual(stamp['reason'], 'disabled')

    def test_success_stamp(self):
        mod = _fake_requests(response=_FakeResponse(200, {
            'result': 'ok',
            'tags': {'github:merge': {'advertised_verbs': ['write:repository'],
                                      'actual_scopes': ['write:repository'],
                                      'gap_class': ''}},
        }))
        now = datetime(2026, 9, 21, 4, 0, 0, tzinfo=dt_timezone.utc)
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV,
            requests_mod=mod, now=now)
        self.assertEqual(stamp['status'], sa.STAMP_OK)
        self.assertEqual(stamp['schema'], sa.ASSERTION_SCHEMA)
        self.assertEqual(stamp['tags'], ['ci-logs', 'github:merge'])
        self.assertIn('2026-09-21T04:00:00', stamp['asserted_at'])
        # contract shape on the wire
        _, kwargs = mod.post.call_args
        self.assertEqual(kwargs['json'], {'seat_email': 'jun@shizuha.com',
                                          'tags': ['ci-logs', 'github:merge']})
        self.assertTrue(kwargs['headers']['Authorization'].startswith('Bearer '))
        self.assertLessEqual(kwargs['timeout'], 2)

    def test_divergence_stamp_is_visible_not_fatal(self):
        mod = _fake_requests(response=_FakeResponse(200, {
            'result': 'divergence',
            'tags': {'github:merge': {'advertised_verbs': ['write:repository'],
                                      'actual_scopes': ['read:repository'],
                                      'gap_class': 'mint_without_scope_check'}},
        }))
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV, requests_mod=mod)
        self.assertEqual(stamp['status'], sa.STAMP_DIVERGENCE)
        self.assertEqual(
            stamp['verdicts']['github:merge']['gap_class'],
            'mint_without_scope_check')

    def test_timeout_resolves_to_skipped_and_never_raises(self):
        # One fake instance for both the module double and the raised exception:
        # each _fake_requests() call defines its own _Timeout class, and the
        # module must catch the class exposed on the SAME double it was given.
        mod = _fake_requests()
        with mock.patch.object(mod, 'post', side_effect=mod.Timeout('boom')):
            stamp = sa.build_scope_assertion_stamp(
                'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV, requests_mod=mod)
        self.assertEqual(stamp['status'], sa.STAMP_SKIPPED)
        self.assertEqual(stamp['reason'], 'timeout')

    def test_404_means_child1_not_landed(self):
        mod = _fake_requests(response=_FakeResponse(404))
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV, requests_mod=mod)
        self.assertEqual((stamp['status'], stamp['reason']),
                         (sa.STAMP_SKIPPED, 'no_endpoint'))

    def test_producer_5xx_is_unknown_not_skip(self):
        mod = _fake_requests(response=_FakeResponse(503, {'detail': 'x'}))
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV, requests_mod=mod)
        self.assertEqual(stamp['status'], sa.STAMP_UNKNOWN)
        self.assertEqual(stamp['reason'], 'producer_status_503')

    def test_malformed_body_is_unknown(self):
        mod = _fake_requests(response=_FakeResponse(200, {'unexpected': True}))
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV, requests_mod=mod)
        self.assertEqual((stamp['status'], stamp['reason']),
                         (sa.STAMP_UNKNOWN, 'malformed_result'))

    def test_unconfigured_url_skips(self):
        env = {'PULSE_SCOPE_ASSERTION_ENABLED': '1'}
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=env,
            requests_mod=_fake_requests(response=_FakeResponse(200)))
        self.assertEqual((stamp['status'], stamp['reason']),
                         (sa.STAMP_SKIPPED, 'unconfigured'))

    def test_transport_error_is_skipped(self):
        mod = _fake_requests(post_side_effect=ConnectionError('refused'))
        stamp = sa.build_scope_assertion_stamp(
            'jun@shizuha.com', self.TAGS, environ=ENABLED_ENV, requests_mod=mod)
        self.assertEqual(stamp['reason'], 'transport_error')


class DispatchWiringTests(SimpleTestCase):
    """The stamp rides positive legacy verdicts only; divergence never gates."""

    CAP = 'github:merge'  # non-substrate: the legacy/TeamCapabilityProfile path

    def _run(self, *, effective, required=frozenset({CAP}), env=None, builder=None):
        item = mock.Mock()
        item._scope_assertion_stamp = None
        with mock.patch.dict(sa_os := __import__('os').environ, env or {}, clear=False), \
             mock.patch.object(WorkflowService, '_agent_effective_capabilities',
                               return_value=set(effective)), \
             mock.patch.object(WorkflowService, '_email_conferred_capabilities',
                               return_value=set(effective)):
            if builder is not None:
                with mock.patch('tasks.services.scope_assertion.'
                                'build_scope_assertion_stamp', side_effect=builder):
                    result = WorkflowService._seat_satisfies_required_capability(
                        'jun@shizuha.com', required, item=item)
            else:
                result = WorkflowService._seat_satisfies_required_capability(
                    'jun@shizuha.com', required, item=item)
        return result, item

    def test_positive_verdict_attaches_stamp(self):
        result, item = self._run(
            effective={self.CAP},
            env={'PULSE_SCOPE_ASSERTION_ENABLED': '1'})
        self.assertTrue(result)
        stamp = item._scope_assertion_stamp
        self.assertIsNotNone(stamp)
        # default env has no producer URL → falls back to the evidence URL env
        # (absent here) → skipped/unconfigured; the ATTACH is what's under test.
        self.assertIn(stamp['status'], (sa.STAMP_SKIPPED, sa.STAMP_UNKNOWN))

    def test_divergence_stamp_does_not_flip_verdict(self):
        result, item = self._run(
            effective={self.CAP},
            env={'PULSE_SCOPE_ASSERTION_ENABLED': '1'},
            builder=lambda email, tags, **kw: {
                'status': sa.STAMP_DIVERGENCE, 'reason': '',
                'tags': sorted(tags), 'verdicts': {}})
        self.assertTrue(result)  # evidence, never a gate
        self.assertEqual(item._scope_assertion_stamp['status'], sa.STAMP_DIVERGENCE)

    def test_denied_seat_does_not_stamp(self):
        result, item = self._run(
            effective=set(),  # seat lacks the required tag
            env={'PULSE_SCOPE_ASSERTION_ENABLED': '1'},
            builder=lambda email, tags, **kw: {'status': sa.STAMP_OK})
        self.assertFalse(result)
        self.assertFalse(item._scope_assertion_stamp)

    def test_builder_receives_conferred_intersection(self):
        seen = {}

        def capture(email, tags, **kw):
            seen['email'] = email
            seen['tags'] = set(tags)
            return {'status': sa.STAMP_SKIPPED, 'reason': 'disabled',
                    'tags': sorted(tags)}

        result, _ = self._run(
            effective={self.CAP, 'kubeconfig'},  # kubeconfig not required here
            env={'PULSE_SCOPE_ASSERTION_ENABLED': '1',
                 'PULSE_HIVE_SCOPE_ASSERTION_URL': 'http://hive.internal/x/',
                 'PULSE_HIVE_SCOPE_ASSERTION_TOKEN': 't'},
            builder=capture)
        self.assertTrue(result)
        self.assertEqual(seen['tags'], {self.CAP})


if __name__ == '__main__':
    unittest.main()

"""PLS-253: a runtime-healthy agent whose Pulse SERVICE grant is revoked
(every request 403s `service_access_denied`) must be treated as unavailable for
routing — the resolver/rebalancer skip it and its in-flight work is drained —
instead of silently accumulating high-priority work on a locked-out seat (the
nova incident, 2026-07-03).

The signal source is ServiceAccessMiddleware: it already 403s the exact identity
whose JWT `enabled_services` lacks `pulse`, so on each denial it records the
email into a Redis hash. The entry stays fresh while the alive-but-locked-out
agent keeps heartbeating and denials keep firing, and goes stale (auto-recover)
once the grant is restored and denials stop.
"""
import time
from unittest.mock import MagicMock, patch

from django.test import TestCase

from tasks.middleware import ServiceAccessMiddleware
from tasks.services.workflow_service import WorkflowService


def _reset_caches():
    WorkflowService._health_avail_cache = None
    WorkflowService._disabled_emails_cache = None
    WorkflowService._db_avail_cache = None
    WorkflowService._service_denied_cache = None


def _redis_with_hash(mapping):
    """A fake Redis client whose hgetall returns `mapping` (bytes k/v)."""
    client = MagicMock()
    client.hgetall.return_value = {
        k.encode(): str(v).encode() for k, v in mapping.items()
    }
    return client


class ServiceDeniedSignalTests(TestCase):
    def setUp(self):
        super().setUp()
        _reset_caches()

    def test_fresh_denial_is_included(self):
        now = int(time.time())
        with patch.object(WorkflowService, '_redis_client',
                          return_value=_redis_with_hash({'nova@shizuha.com': now})):
            self.assertIn('nova@shizuha.com', WorkflowService._service_denied_agent_emails())

    def test_stale_denial_is_excluded_autorecovery(self):
        # A denial older than the freshness window means denials stopped (grant
        # restored) — the seat must auto-recover and no longer read denied.
        old = int(time.time()) - (WorkflowService.SERVICE_DENIED_STALE_SECONDS + 60)
        with patch.object(WorkflowService, '_redis_client',
                          return_value=_redis_with_hash({'nova@shizuha.com': old})):
            self.assertNotIn('nova@shizuha.com', WorkflowService._service_denied_agent_emails())

    def test_redis_outage_fails_open_empty(self):
        client = MagicMock()
        client.hgetall.side_effect = RuntimeError('redis down')
        with patch.object(WorkflowService, '_redis_client', return_value=client), \
                self.assertLogs('tasks.services.workflow_service', level='ERROR') as logs:
            self.assertEqual(WorkflowService._service_denied_agent_emails(), set())
        self.assertTrue(any('service-denied routing signal read failed' in msg
                            for msg in logs.output))

    def test_mark_agent_service_denied_writes_hash(self):
        client = MagicMock()
        with patch.object(WorkflowService, '_redis_client', return_value=client):
            WorkflowService.mark_agent_service_denied('Nova@Shizuha.com')
        client.hset.assert_called_once()
        args = client.hset.call_args.args
        self.assertEqual(args[0], WorkflowService._SERVICE_DENIED_REDIS_KEY)
        self.assertEqual(args[1], 'nova@shizuha.com')  # normalized lowercase

    def test_mark_is_best_effort_on_redis_error(self):
        client = MagicMock()
        client.hset.side_effect = RuntimeError('redis down')
        with patch.object(WorkflowService, '_redis_client', return_value=client), \
                self.assertLogs('tasks.services.workflow_service', level='ERROR') as logs:
            # Must not raise — observability write cannot break the request path.
            WorkflowService.mark_agent_service_denied('nova@shizuha.com')
        self.assertTrue(any('service-denied routing signal write failed' in msg
                            for msg in logs.output))

    def test_blank_email_is_ignored(self):
        client = MagicMock()
        with patch.object(WorkflowService, '_redis_client', return_value=client):
            WorkflowService.mark_agent_service_denied('')
            WorkflowService.mark_agent_service_denied(None)
        client.hset.assert_not_called()


class ServiceDeniedRoutingGateTests(TestCase):
    """The denial signal must make the seat unavailable AND drain-eligible."""

    def setUp(self):
        super().setUp()
        _reset_caches()

    def test_service_denied_seat_is_unavailable_for_assignment(self):
        with patch.object(WorkflowService, '_service_denied_agent_emails',
                          return_value={'nova@shizuha.com'}), \
                patch.object(WorkflowService, '_health_availability_map', return_value={}), \
                patch.object(WorkflowService, '_redis_client') as rc:
            rc.return_value.get.return_value = None
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment('nova@shizuha.com'))
            # The @agents alias is the same identity — same verdict.
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment('nova@agents.shizuha.io'))

    def test_service_denied_unioned_into_disabled_for_drain(self):
        # _disabled_agent_emails is what the rebalance sweep drains (PLAT-350), so
        # a service-denied seat's in-flight work is drained, not accumulated.
        with patch.object(WorkflowService, '_service_denied_agent_emails',
                          return_value={'nova@shizuha.com'}), \
                patch.object(WorkflowService, '_redis_client') as rc:
            rc.return_value.get.return_value = None
            self.assertIn('nova@shizuha.com', WorkflowService._disabled_agent_emails())

    def test_healthy_seat_without_denial_stays_available(self):
        with patch.object(WorkflowService, '_service_denied_agent_emails', return_value=set()), \
                patch.object(WorkflowService, '_health_availability_map', return_value={}), \
                patch.object(WorkflowService, '_redis_client') as rc:
            rc.return_value.get.return_value = None
            # PLAT-319 fail-open preserved for a genuinely-unknown healthy agent.
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment('kai@shizuha.com'))


class ServiceAccessMiddlewareRecordsDenialTests(TestCase):
    def setUp(self):
        super().setUp()
        _reset_caches()

    def _run_denied(self, payload):
        mw = ServiceAccessMiddleware(get_response=lambda req: MagicMock(name='response'))
        request = MagicMock()
        request.path = '/pulse/api/items/'
        request.META = {'HTTP_AUTHORIZATION': 'Bearer faketoken'}
        with patch('tasks.middleware.is_public_api_path', return_value=True), \
                patch('tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
                      return_value=payload), \
                patch.object(WorkflowService, 'mark_agent_service_denied') as mark:
            resp = mw(request)
        return resp, mark

    def test_denial_records_email_from_payload(self):
        payload = {'email': 'nova@shizuha.com', 'enabled_services': ['id', 'wiki']}
        resp, mark = self._run_denied(payload)
        self.assertEqual(resp.status_code, 403)
        mark.assert_called_once_with('nova@shizuha.com')

    def test_denial_falls_back_to_username_when_no_email(self):
        payload = {'username': 'nova', 'enabled_services': ['id']}
        resp, mark = self._run_denied(payload)
        self.assertEqual(resp.status_code, 403)
        mark.assert_called_once_with('nova@shizuha.com')

    def test_granted_agent_is_not_recorded(self):
        payload = {'email': 'nova@shizuha.com', 'enabled_services': ['pulse', 'id']}
        mw = ServiceAccessMiddleware(get_response=lambda req: MagicMock(status_code=200))
        request = MagicMock()
        request.path = '/pulse/api/items/'
        request.META = {'HTTP_AUTHORIZATION': 'Bearer faketoken'}
        with patch('tasks.middleware.is_public_api_path', return_value=True), \
                patch('tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
                      return_value=payload), \
                patch.object(WorkflowService, 'mark_agent_service_denied') as mark:
            mw(request)
        mark.assert_not_called()

    def test_staff_bypass_is_not_recorded(self):
        payload = {'email': 'admin@shizuha.com', 'is_staff': True, 'enabled_services': ['id']}
        mw = ServiceAccessMiddleware(get_response=lambda req: MagicMock(status_code=200))
        request = MagicMock()
        request.path = '/pulse/api/items/'
        request.META = {'HTTP_AUTHORIZATION': 'Bearer faketoken'}
        with patch('tasks.middleware.is_public_api_path', return_value=True), \
                patch('tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
                      return_value=payload), \
                patch.object(WorkflowService, 'mark_agent_service_denied') as mark:
            mw(request)
        mark.assert_not_called()

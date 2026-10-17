"""Stale self-latches must not drive critical assignment decisions.

Operator 2026-07-28: UI may use ~30s-stale caches; Pulse must not treat leftover
``self:claude-token-pool-exhausted`` rows as a kill-switch for a seat Hive says
is a live Cortex/GLM gateway agent. Live exporter health UP beats non-operator
durable OFF.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService
from tasks.views import AgentSelfAvailabilityView

NOVA = 'nova@shizuha.com'


class StaleSelfAvailabilityRoutingTests(TestCase):
    def tearDown(self):
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None

    def test_live_health_up_ignores_stale_self_claude_latch(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='inactive',
            reason='claude-token-pool-exhausted',
            updated_by=f'self:{NOVA}',
        )
        with mock.patch.object(
            WorkflowService,
            '_health_availability_map',
            return_value={NOVA: True},
        ):
            WorkflowService._db_avail_cache = None
            WorkflowService._disabled_emails_cache = None
            self.assertIsNone(WorkflowService._db_agent_availability(NOVA))
            self.assertNotIn(NOVA, WorkflowService._disabled_agent_emails())
            self.assertTrue(WorkflowService._is_agent_available_for_assignment(NOVA))

    def test_operator_force_off_still_blocks_when_health_up(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='on_leave',
            reason='operator parked',
            updated_by='operator',
        )
        with mock.patch.object(
            WorkflowService,
            '_health_availability_map',
            return_value={NOVA: True},
        ):
            WorkflowService._db_avail_cache = None
            WorkflowService._disabled_emails_cache = None
            self.assertFalse(WorkflowService._db_agent_availability(NOVA))
            self.assertIn(NOVA, WorkflowService._disabled_agent_emails())
            self.assertFalse(WorkflowService._is_agent_available_for_assignment(NOVA))

    def test_self_off_still_blocks_when_live_health_down(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='inactive',
            reason='claude-token-pool-exhausted',
            updated_by=f'self:{NOVA}',
        )
        with mock.patch.object(
            WorkflowService,
            '_health_availability_map',
            return_value={NOVA: False},
        ):
            WorkflowService._db_avail_cache = None
            self.assertFalse(WorkflowService._db_agent_availability(NOVA))
            self.assertFalse(WorkflowService._is_agent_available_for_assignment(NOVA))


class CrossProviderSelfDisableRejectedTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _patch(self, payload, headers=None):
        request = self.factory.patch(
            '/api/agent-availability/self/',
            payload,
            format='json',
            **(headers or {}),
        )
        force_authenticate(
            request,
            user=SimpleNamespace(email=NOVA, is_authenticated=True),
        )
        return AgentSelfAvailabilityView.as_view()(request)

    def test_rejects_claude_pool_latch_for_shizuha_gateway_seat(self):
        response = self._patch({
            'active': False,
            'reason': 'claude-token-pool-exhausted',
            'execution_method': 'shizuha',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get('code'), 'cross_provider_self_disable_rejected')
        self.assertFalse(AgentAvailability.objects.filter(email=NOVA).exists())

    def test_allows_claude_pool_latch_for_claude_seat(self):
        response = self._patch({
            'active': False,
            'reason': 'claude-token-pool-exhausted',
            'execution_method': 'claude_code_server',
        })
        self.assertEqual(response.status_code, 200)
        row = AgentAvailability.objects.get(email=NOVA)
        self.assertFalse(row.active)
        self.assertEqual(row.reason, 'claude-token-pool-exhausted')

"""PLS-734: stale foreign AgentAvailability deactivation must auto-clear on a
healthy claude-bridge seat.

The 2026-06-27 ni/nori un-sticking fix made the reconciler
(``_reconcile_health_managed_availability``) auto-clear stale non-operator
deactivations, but every recovery predicate is gated on a codex-specific
capability signal (``shizuha_agent_empty_turn_streak``) that claude bridges
never emit. A FOREIGN row (``updated_by`` neither ``operator`` nor
``health-sweep`` — e.g. ``ichi`` / ``shizuha-steward``) therefore pinned the
seat unavailable FOREVER even after it recovered.

Fix: a current live UP health sample (``health.get(email) is True``) is
recovery evidence for a foreign row, mirroring what ``_db_agent_availability``
already does for health-sweep rows. Guards preserved: operator force-OFF stays
sacred; a ghost/retired agent with no health sample (``None``, not ``True``)
is NOT reactivated.

Acceptance (from the task):
1. Foreign inactive row on a healthy claude-bridge seat is auto-cleared.
2. ``updated_by='operator'`` row is NOT cleared.
3. No-health-sample (ghost/retired) seat is NOT reactivated.
4. ``updated_by`` comparisons are case-insensitive + semantically consistent.
5. Regression in production caller order: seed foreign inactive row, run the
   sweep, assert active + routable; run the sweep a second time, assert it
   stays cleared. Fixture emits NO empty_turn_streak sample and stamps the row
   ``updated_by='ichi'``.
"""
from unittest.mock import patch, MagicMock

from django.test import TestCase

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reconcile_health_managed_availability, HEALTH_SWEEP_ACTOR

NOW = '2026-08-26T12:00:00Z'
THRESHOLD = 3


def _reset_availability_caches():
    WorkflowService._health_avail_cache = None
    WorkflowService._disabled_emails_cache = None
    WorkflowService._db_avail_cache = None


class ForeignAvailabilityRecoveryTests(TestCase):
    """Layer 1: the reconciler clears a foreign row on a live-UP sample."""

    def test_foreign_row_cleared_when_healthy(self):
        """A foreign (agent-name-stamped) inactive row on a healthy seat is cleared."""
        AgentAvailability.objects.create(
            email='claude1@shizuha.com', active=False, status='inactive',
            reason='codex agent DOWN 2026-06-15', updated_by='ichi')
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, health={'claude1@shizuha.com': True})
        self.assertEqual(react, ['claude1@shizuha.com'])
        row = AgentAvailability.objects.get(email='claude1@shizuha.com')
        self.assertTrue(row.active)
        self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)

    def test_foreign_row_not_cleared_when_down(self):
        """A foreign row on a currently-DOWN seat is NOT cleared."""
        AgentAvailability.objects.create(
            email='claude1@shizuha.com', active=False, status='inactive',
            reason='codex agent DOWN 2026-06-15', updated_by='ichi')
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, health={'claude1@shizuha.com': False})
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='claude1@shizuha.com').active)

    def test_foreign_row_not_cleared_when_no_health_sample(self):
        """A ghost/retired seat with no health sample is NOT reactivated (None != True)."""
        AgentAvailability.objects.create(
            email='ghost@shizuha.com', active=False, status='inactive',
            reason='retired experiment', updated_by='shizuha-steward')
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, health={})  # no sample for ghost
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='ghost@shizuha.com').active)

    def test_operator_row_never_cleared(self):
        """An operator force-OFF row is NEVER auto-reactivated."""
        AgentAvailability.objects.create(
            email='op@shizuha.com', active=False, status='inactive',
            reason='operator disable', updated_by='operator')
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, health={'op@shizuha.com': True})
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='op@shizuha.com').active)

    def test_health_sweep_row_still_capability_gated(self):
        """A health-sweep row is NOT cleared by a live-UP sample alone (PLS-748)."""
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=False, status='inactive',
            reason=f'auto: provider unavailable — provider-health signal @ {NOW}',
            updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, health={'kei@shizuha.com': True})
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='kei@shizuha.com').active)

    def test_foreign_row_case_insensitive_updated_by(self):
        """updated_by comparison is case-insensitive (Health-Sweep != foreign)."""
        AgentAvailability.objects.create(
            email='claude2@shizuha.com', active=False, status='inactive',
            reason='stale latch', updated_by='Health-Sweep')
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, health={'claude2@shizuha.com': True})
        # 'Health-Sweep' is semantically a health-sweep row (case-insensitive),
        # so it must NOT be cleared by the foreign-row live-UP path.
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='claude2@shizuha.com').active)


class ForeignAvailabilityRoutingTests(TestCase):
    """Layer 2: production caller order — sweep clears the row and routing follows.

    The fixture emits NO empty_turn_streak sample (claude-bridge seat) and
    stamps the row updated_by='ichi' so the fix is the only thing that can
    clear it.
    """

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def _run_sweep(self):
        from tasks import tasks as T
        fake = _FakeRedis()
        rget = MagicMock()
        # Exporter scrape: claude1 is process-up + enabled + provider-healthy,
        # but emits NO empty_turn_streak (claude-bridge never emits it).
        rget.text = "\n".join([
            'shizuha_agent_process_up{agent="claude1",backend="claude"} 1',
            'shizuha_agent_enabled{agent="claude1"} 1',
            'shizuha_agent_provider_healthy{agent="claude1"} 1',
        ])
        rget.status_code = 200
        rget.raise_for_status.return_value = None
        envd = {
            'PULSE_HEALTH_EXPORTER_URL': 'http://x/metrics',
            'PULSE_AUTO_DEACTIVATE_ENABLED': '1',
            'PULSE_AUTO_DEACTIVATE_STREAK': str(THRESHOLD),
            'PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED': '0',
        }
        with patch.dict('os.environ', envd, clear=False), \
             patch('tasks.services.workflow_service.WorkflowService._redis_client',
                   return_value=fake), \
             patch('requests.get', return_value=rget):
            T.sweep_agent_availability.run()
        return fake

    def test_sweep_clears_foreign_row_and_routing_follows(self):
        AgentAvailability.objects.create(
            email='claude1@shizuha.com', active=False, status='inactive',
            reason='codex agent DOWN 2026-06-15', updated_by='ichi')

        self._run_sweep()

        row = AgentAvailability.objects.get(email='claude1@shizuha.com')
        self.assertTrue(row.active, "sweep must clear the foreign row on a live-UP seat")
        self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)

        # Routing must now treat the seat as available.
        self.assertTrue(
            WorkflowService._is_agent_available_for_assignment(
                'claude1@shizuha.com', require_live_evidence=False))

        # Second sweep: stays cleared (no flip-flop).
        self._run_sweep()
        row.refresh_from_db()
        self.assertTrue(row.active)
        self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)


class _FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get(self, k):
        v = self.store.get(k)
        return v.encode() if isinstance(v, str) else v

    def setex(self, k, ttl, v):
        self.store[k] = v

    def delete(self, *ks):
        for k in ks:
            self.store.pop(k, None)

    def scan_iter(self, *a, **k):
        return iter(())

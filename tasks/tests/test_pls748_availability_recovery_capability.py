"""PLS-748 (2026-08-18): availability recovery must observe CAPABILITY, not just
reachability.

Live incident 2026-07-27: 4 merge seats flipped ``active=True`` on
'provider-health signal available' / 'shizuha_agent_enabled=1 and available'
while their broker sidecar refused model tokens (coordinator HTTP 404) — ~120
merge items were routed into seats that cannot mint a model token. The recovery
predicates observed provider reachability + the enablement flag, never whether
the seat can actually serve a turn.

Fix (two layers):
1. Reconciler (``_reconcile_health_managed_availability``): every recovery path
   is gated on a CAPABILITY signal — ``shizuha_agent_empty_turn_streak == 0``
   (codex producing output again). Provider reachability / enablement alone no
   longer clears a seat.
2. Routing (``_availability_off_is_routing_kill``): a health-sweep row
   deactivated on a capability signal (``codex empty-turn streak`` exhaustion)
   is a routing kill even when the live health map samples the seat UP —
   process-up/provider-healthy are reachability, not capability.

These tests drive the REAL reconciler path (``sweep_agent_availability`` with a
mocked exporter scrape) and assert on ROUTING OUTCOME, not only the row value.
"""
import json
from unittest.mock import patch, MagicMock

from django.test import TestCase

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reconcile_health_managed_availability, HEALTH_SWEEP_ACTOR

NOW = '2026-08-18T12:00:00Z'
THRESHOLD = 3


def _reset_availability_caches():
    WorkflowService._health_avail_cache = None
    WorkflowService._disabled_emails_cache = None
    WorkflowService._db_avail_cache = None


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


def _metrics(kei_streak):
    """Exporter scrape: kei is process-up + enabled + provider-healthy, but its
    empty-turn streak is `kei_streak` (>= threshold when the broker cannot mint
    a model token, so every turn is empty)."""
    return "\n".join([
        'shizuha_agent_process_up{agent="kei",backend="codex"} 1',
        'shizuha_agent_enabled{agent="kei"} 1',
        'shizuha_agent_provider_healthy{agent="kei"} 1',
        f'shizuha_agent_empty_turn_streak{{agent="kei"}} {kei_streak}',
    ])


class CapabilityGatedRecoveryTests(TestCase):
    """Layer 1: the reconciler's recovery predicates observe capability."""

    def test_provider_row_does_not_recover_without_capability_signal(self):
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=False, status='inactive',
            reason=f'auto: provider unavailable — provider-health signal @ {NOW}',
            updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up={'kei@shizuha.com'}, provider_down=set())
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='kei@shizuha.com').active)

    def test_provider_row_recovers_when_capability_observed(self):
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=False, status='inactive',
            reason=f'auto: provider unavailable — provider-health signal @ {NOW}',
            updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability(
            {'kei@shizuha.com': 0}, THRESHOLD, NOW,
            enabled_up={'kei@shizuha.com'}, provider_down=set())
        self.assertEqual(react, ['kei@shizuha.com'])
        self.assertTrue(AgentAvailability.objects.get(email='kei@shizuha.com').active)

    def test_reenabled_row_does_not_recover_without_capability_signal(self):
        AgentAvailability.objects.create(
            email='aoi@shizuha.com', active=False, status='inactive',
            reason='auto: agent stopped/disabled — shizuha_agent_enabled=0 @ x',
            updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up={'aoi@shizuha.com'})
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='aoi@shizuha.com').active)


class RoutingKillCarveOutTests(TestCase):
    """Layer 2: a capability-based deactivation kills routing even when live
    health samples the seat UP."""

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def test_empty_turn_streak_deactivation_kills_routing_despite_live_up(self):
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=False, status='inactive',
            reason=('auto: provider exhausted — codex empty-turn streak 4 '
                    f'(>= {THRESHOLD}) @ {NOW}'),
            updated_by=HEALTH_SWEEP_ACTOR)
        # Live health map says UP (process-up + provider-healthy) — the exact
        # PLS-748 trap. The capability deactivation must still kill routing.
        with patch.object(WorkflowService, '_health_availability_map',
                          return_value={'kei@shizuha.com': True}):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment('kei@shizuha.com'))

    def test_plain_provider_health_row_still_ignored_when_live_up(self):
        """The Nova/GLM split-brain doctrine is preserved: a provider-health
        deactivation (NOT capability-based) is still overridden by live-health-UP."""
        AgentAvailability.objects.create(
            email='nagi@shizuha.com', active=False, status='inactive',
            reason='auto: provider unavailable — provider-health signal @ x',
            updated_by=HEALTH_SWEEP_ACTOR)
        with patch.object(WorkflowService, '_health_availability_map',
                          return_value={'nagi@shizuha.com': True}):
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment('nagi@shizuha.com'))


class SweepRoutingOutcomeTests(TestCase):
    """AC4: drive the REAL reconciler path (sweep_agent_availability with a mocked
    exporter) and assert on routing outcome — a seat that looks healthy
    (process-up + enabled + provider-healthy) but cannot mint a model token
    (empty-turn streak >= threshold) must NOT be routed work."""

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def _run_sweep(self, kei_streak):
        from tasks import tasks as T
        fake = _FakeRedis()
        rget = MagicMock()
        rget.text = _metrics(kei_streak)
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

    def test_healthy_looking_but_token_mint_404_seat_not_routed(self):
        """The PLS-748 repro: kei is process-up + enabled + provider-healthy but
        the broker cannot mint a model token (empty-turn streak 4 >= 3). The
        sweep must NOT falsely recover the seat, and routing must NOT assign it
        work."""
        fake = self._run_sweep(kei_streak=4)

        row = AgentAvailability.objects.get(email='kei@shizuha.com')
        self.assertFalse(row.active)
        self.assertIn('codex empty-turn streak', row.reason)

        # Routing outcome: the seat must not receive work even though the live
        # health map (written by this same sweep) samples it UP.
        health_map = json.loads(fake.store['pulse:agent_availability'])
        self.assertIs(health_map.get('kei@shizuha.com'), True)
        self.assertFalse(
            WorkflowService._is_agent_available_for_assignment('kei@shizuha.com'))

    def test_producing_seat_recovers_and_is_routed(self):
        """Positive control: a previously-deactivated seat that is once again
        observed producing output (streak 0) is recovered by the sweep and
        routing assigns it work."""
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=False, status='inactive',
            reason=('auto: provider exhausted — codex empty-turn streak 4 '
                    f'(>= {THRESHOLD}) @ {NOW}'),
            updated_by=HEALTH_SWEEP_ACTOR)
        fake = self._run_sweep(kei_streak=0)

        row = AgentAvailability.objects.get(email='kei@shizuha.com')
        self.assertTrue(row.active)
        health_map = json.loads(fake.store['pulse:agent_availability'])
        self.assertIs(health_map.get('kei@shizuha.com'), True)
        self.assertTrue(
            WorkflowService._is_agent_available_for_assignment('kei@shizuha.com'))

"""PLS-1187: lane-health must not hang on a degraded Redis (circuit breaker).

Live defect (PLS-1033 S1, 09-04): ``GET /api/lane-health/`` hung 10s+ on
authenticated requests while the rest of the API served fine. Root cause
(measured, repro in the task record): every ``_redis_client()`` call built a
FRESH redis client, so a degraded/unreachable Redis burned the full
``socket_connect_timeout`` (2s) per helper call; the 5s memo windows on the
availability helpers (``_health_availability_map``, ``_disabled_agent_emails``,
``_wakeable_agent_emails``, hibernated set) expired mid-loop because the
per-lane work itself exceeded 5s under that stall — so every lane re-paid
~3-4 connect timeouts. Measured 27.25s for 3 lanes (55x the healthy path);
production lane counts hang past any client ceiling.

Fix: process-level Redis client singleton (pooled connections) + a circuit
breaker (``_redis_get``) that bounds a degraded Redis to ONE connect-timeout
per 30s window for the whole process, failing open to the helpers' existing
fail-open defaults.

Regression contract pinned here:
- under a persistently failing Redis, ``all_lanes_health()`` still returns
  every lane (fail-open semantics preserved);
- the whole aggregation attempts AT MOST ONE Redis GET (the circuit opens on
  the first failure — the old code attempted one per helper per memo window);
- a healthy Redis path is unaffected (client reused, no circuit trips).
"""
from unittest import mock

from django.test import TestCase

from tasks.models import TeamRoster
from tasks.services.lane_health import all_lanes_health
from tasks.services import workflow_service as ws


ROSTER = {
    'engineering': [['nagi@shizuha.com'], ['aoi@shizuha.com']],
    'review': [['revi@shizuha.com']],
    'merge': [['kai2574@shizuha.com']],
}


class LaneHealthRedisCircuitTests(TestCase):
    def _seed_lanes(self):
        for team in ('engineering', 'review', 'merge'):
            TeamRoster.objects.get_or_create(
                team_name=team, organization_slug='shizuha',
                defaults={'is_active': True, 'version': 1},
            )

    def _reset_process_state(self):
        ws.WorkflowService._health_avail_cache = None
        ws.WorkflowService._disabled_emails_cache = None
        ws.WorkflowService._wakeable_emails_cache = None
        ws.WorkflowService._redis_circuit_until = 0.0
        ws.WorkflowService._redis_client_cache = None

    def _roster_mocks(self):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            ws.WorkflowService, '_pod_roster', return_value=ROSTER))
        stack.enter_context(mock.patch.object(
            ws.WorkflowService, 'assignable_teams', return_value=ROSTER))
        stack.enter_context(mock.patch.object(
            ws.WorkflowService, '_agent_email_aliases',
            side_effect=lambda e: {str(e).strip().lower()}))
        return stack

    def test_degraded_redis_returns_all_lanes_with_single_get_attempt(self):
        """Hang regression: persistently failing Redis -> one GET attempt total.

        The old code built a fresh client per helper call and attempted a GET
        per helper per memo-window expiry (>= 3 attempts here, unbounded across
        lanes with real connect timeouts). The circuit breaker must open on the
        first failure and serve every lane from the fail-open defaults.
        """
        self._seed_lanes()
        self._reset_process_state()
        attempts = []

        class DeadRedis:
            def get(self, key):
                attempts.append(key)
                import redis
                raise redis.exceptions.ConnectionError('degraded redis (simulated)')

        with self._roster_mocks(), \
             mock.patch.object(ws.WorkflowService, '_redis_client', return_value=DeadRedis()):
            lanes = all_lanes_health()

        names = {l['team'] for l in lanes}
        self.assertTrue({'engineering', 'review', 'merge'} <= names,
                        f'every seeded lane must still be returned; got {sorted(names)}')
        self.assertGreaterEqual(
            sum(len(l['seats']) for l in lanes), 4,
            'seat verdicts must still be produced (fail-open, not skipped)',
        )
        self.assertLessEqual(
            len(attempts), 1,
            f'circuit breaker must cap Redis GET attempts at 1 for the whole '
            f'aggregation; got {len(attempts)} (old code re-paid per helper/lane)',
        )

    def test_healthy_redis_path_unaffected(self):
        """The singleton client is reused and no circuit opens on success."""
        self._seed_lanes()
        self._reset_process_state()

        class GoodRedis:
            def __init__(self):
                self.gets = []

            def get(self, key):
                self.gets.append(key)
                return None

        good = GoodRedis()
        with self._roster_mocks(), \
             mock.patch.object(ws.WorkflowService, '_redis_client', return_value=good):
            lanes = all_lanes_health()

        names = {l['team'] for l in lanes}
        self.assertTrue({'engineering', 'review', 'merge'} <= names)
        self.assertGreaterEqual(len(good.gets), 1, 'healthy path must still read Redis')
        self.assertEqual(
            ws.WorkflowService._redis_circuit_until, 0.0,
            'a healthy Redis must never trip the circuit',
        )

    def test_circuit_reopens_after_window(self):
        """After the circuit window expires, exactly one new attempt is made."""
        import time
        self._reset_process_state()

        class DeadRedis:
            def get(self, key):
                import redis
                raise redis.exceptions.ConnectionError('still degraded')

        with mock.patch.object(ws.WorkflowService, '_redis_client', return_value=DeadRedis()):
            self.assertIsNone(ws.WorkflowService._redis_get('k1'))
            self.assertIsNone(ws.WorkflowService._redis_get('k2'),
                              'circuit open -> no second attempt inside the window')
            # Expire the window manually (no real 30s sleep in tests).
            ws.WorkflowService._redis_circuit_until = time.time() - 0.01
            self.assertIsNone(ws.WorkflowService._redis_get('k3'),
                              'one fresh attempt after the window expires')
            self.assertIsNone(ws.WorkflowService._redis_get('k4'),
                              'circuit re-opens after the fresh failure')

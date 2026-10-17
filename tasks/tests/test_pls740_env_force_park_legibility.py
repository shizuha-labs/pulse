"""PLS-740: an env force-park (PULSE_UNAVAILABLE_AGENT_EMAILS) must be LEGIBLE.

`_configured_agent_availability` rejects a seat on the env list AFTER the health
map has already returned True, and previously wrote NOTHING to the reason
surface — so a deliberately-parked seat read exactly like a healthy routable one
on every instrument (the 2026-06-13 46-task silent starvation). The fix folds
the env-parked set into `pulse:agent_unavailability_reason` with a distinct
`force_parked` reason. Legibility only — the parking verdict itself is unchanged.
"""
import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from tasks.services.workflow_service import WorkflowService


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


def _metrics(process_up, enabled=None):
    lines = [
        f'shizuha_agent_process_up{{agent="{a}",backend="codex"}} {v}'
        for a, v in process_up.items()
    ]
    enabled = enabled if enabled is not None else {a: 1 for a in process_up}
    lines += [
        f'shizuha_agent_enabled{{agent="{a}"}} {v}' for a, v in enabled.items()
    ]
    return "\n".join(lines)


def _run_sweep(process_up, env=None, prev_map=None):
    from tasks import tasks as T
    fake = _FakeRedis({'pulse:agent_availability': json.dumps(prev_map or {})})
    rget = MagicMock()
    rget.text = _metrics(process_up)
    rget.status_code = 200
    rget.raise_for_status.return_value = None
    envd = {'PULSE_HEALTH_EXPORTER_URL': 'http://x/metrics'}
    envd.update(env or {})
    with patch.dict('os.environ', envd, clear=False), \
         patch('tasks.services.workflow_service.WorkflowService._redis_client', return_value=fake), \
         patch('requests.get', return_value=rget):
        T.sweep_agent_availability.run()
    reasons = json.loads(fake.store['pulse:agent_unavailability_reason'])
    return reasons, fake


class PLS740EnvForceParkLegibilityTests(TestCase):
    def test_env_force_parked_healthy_seat_gets_force_parked_reason(self):
        # akira: process_up=1, enabled=1, health True (in avail) — but env-parked.
        reasons, _ = _run_sweep(
            {'akira': 1},
            env={'PULSE_UNAVAILABLE_AGENT_EMAILS': 'akira@shizuha.com'},
        )
        self.assertEqual(reasons.get('akira@shizuha.com'), 'force_parked')

    def test_force_parked_reason_distinct_from_health_parked(self):
        # akira env-parked (process_up=1), ren health-parked (process_up=0).
        reasons, _ = _run_sweep(
            {'akira': 1, 'ren': 0},
            env={'PULSE_UNAVAILABLE_AGENT_EMAILS': 'akira@shizuha.com'},
        )
        self.assertEqual(reasons.get('akira@shizuha.com'), 'force_parked')
        self.assertNotEqual(reasons.get('ren@shizuha.com'), 'force_parked')
        self.assertIn(reasons.get('ren@shizuha.com'), ('offline', 'provider_down'))

    def test_force_parked_seat_with_no_exporter_sample_still_surfaced(self):
        # A decommissioned seat on the env list has no exporter sample at all —
        # it must still appear in the reason census.
        reasons, _ = _run_sweep(
            {'ren': 1},
            env={'PULSE_UNAVAILABLE_AGENT_EMAILS': 'akira@shizuha.com'},
        )
        self.assertEqual(reasons.get('akira@shizuha.com'), 'force_parked')

    def test_env_force_parked_seat_is_not_routable(self):
        # Production caller order: env-parked + health True + no DB row -> not routable.
        with patch.dict('os.environ', {
            'PULSE_UNAVAILABLE_AGENT_EMAILS': 'akira@shizuha.com',
        }, clear=False), \
             patch.object(WorkflowService, '_health_availability_map',
                          return_value={'akira@shizuha.com': True}), \
             patch.object(WorkflowService, '_disabled_agent_emails',
                          return_value=set()), \
             patch.object(WorkflowService, '_db_agent_availability',
                          return_value=None):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment('akira@shizuha.com'))

    def test_no_env_park_no_force_parked_reason(self):
        reasons, _ = _run_sweep({'ren': 1})
        self.assertNotIn('ren@shizuha.com', reasons)
        self.assertNotIn('force_parked', reasons.values())

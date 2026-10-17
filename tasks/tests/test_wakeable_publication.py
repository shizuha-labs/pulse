"""Wakeable-set publication (operator directive 2026-09-16, one truth).

The resolver's typed wakeable-seat admission (`_is_agent_wakeable_for_new_assignment`)
reads `pulse:agent_wakeable`. That set was computed only from the Hive
lifecycle-regex source (`hibernated_all`); when that source finds nothing
(agents.json format drift) the `_hive_hibernated_agents()` fallback still
publishes the hibernated set — but `wakeable` stayed permanently empty, making
assignment-to-hibernated dead code. The sweep must derive wakeable from the
authoritative hibernation set (whichever source produced it) minus
provider-down / wedged / disabled seats.

Regression tests:
  1. Fallback-sourced hibernation (empty regex side) still yields wakeable.
  2. Provider-down / disabled seats are excluded from wakeable.
"""
import json
from unittest import mock

from django.test import TestCase


class _FakeRedis:
    def __init__(self, seed=None):
        self._d = dict(seed or {})

    def get(self, k):
        return self._d.get(k)

    def setex(self, k, ttl, v):
        self._d[k] = v

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self._d:
            return False
        self._d[k] = v
        return True

    def delete(self, k):
        self._d.pop(k, None)

    def hgetall(self, k):
        return {}

    def hset(self, *a, **k):
        pass

    def expire(self, *a, **k):
        pass


def _metrics(enabled=1.0):
    lines = [f'shizuha_agent_process_up{{agent="{a}"}} {enabled}' for a in ('nagi', 'saki')]
    return '\n'.join(lines)


class WakeablePublicationTests(TestCase):
    def _run(self, seed, hive_hibernated=None):
        from tasks import tasks as T
        fake = _FakeRedis(seed)
        resp = mock.MagicMock()
        resp.read.return_value = _metrics().encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda s, *a: False
        rget = mock.MagicMock()
        rget.text = _metrics()
        rget.status_code = 200
        rget.raise_for_status.return_value = None
        envd = {'PULSE_HEALTH_EXPORTER_URL': 'http://x/metrics'}
        with mock.patch.dict('os.environ', envd, clear=False), \
             mock.patch('tasks.services.workflow_service.WorkflowService._redis_client', return_value=fake), \
             mock.patch('tasks.services.workflow_service.WorkflowService._redis_get', return_value=None), \
             mock.patch('requests.get', return_value=rget), \
             mock.patch('urllib.request.urlopen', return_value=resp), \
             mock.patch.object(T, '_hive_hibernated_agents',
                               return_value=(hive_hibernated or [], True)):
            T.sweep_agent_availability.run()
        return fake

    def test_fallback_hibernation_yields_wakeable(self):
        # Regex-side lifecycle states absent; the Hive fallback reports nagi
        # hibernated. The published wakeable set must include nagi.
        fake = self._run({}, hive_hibernated=[
            'nagi@shizuha.com', 'nagi@agents.shizuha.io'])
        wakeable = json.loads(fake._d.get('pulse:agent_wakeable') or '[]')
        self.assertTrue(any('nagi' in w for w in wakeable),
                        f'wakeable empty despite Hive hibernation: {wakeable}')

    def test_provider_down_and_disabled_excluded(self):
        fake = self._run({}, hive_hibernated=[
            'nagi@shizuha.com', 'nagi@agents.shizuha.io'])
        reasons = json.loads(fake._d.get('pulse:agent_unavailability_reason') or '{}')
        wakeable = json.loads(fake._d.get('pulse:agent_wakeable') or '[]')
        for email, reason in reasons.items():
            if reason in ('provider_down', 'disabled', 'force_parked'):
                self.assertNotIn(email, wakeable)

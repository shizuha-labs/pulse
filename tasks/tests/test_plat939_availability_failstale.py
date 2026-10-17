"""PLAT-939: availability map must survive a daemon-control-plane outage.

When the daemon control API is down, the agent-health exporter emits process_up=0
en masse for still-running agents. sweep_agent_availability must NOT mass-fail-over
those agents on a bare (uncorroborated) process_up=0 flip — it preserves last-known-good
(fail-stale) and publishes the availability_source_degraded marker. A genuinely-down
agent (with a corroborating wedge/provider/toggle signal) must STILL fail over.
"""
import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings


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


def _metrics(process_up):
    # process_up: {agent: 0|1}
    return "\n".join(
        f'shizuha_agent_process_up{{agent="{a}",backend="codex"}} {v}'
        for a, v in process_up.items())


@override_settings()
class AvailabilityFailStaleTests(TestCase):
    def _run(self, prev_map, scrape_up, env=None):
        from tasks import tasks as T
        fake = _FakeRedis({'pulse:agent_availability': json.dumps(prev_map)})
        resp = MagicMock()
        resp.read.return_value = _metrics(scrape_up).encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda s, *a: False
        # The exporter scrape migrated urllib.request.urlopen -> requests.get; mock
        # both (other endpoints still use urllib).
        rget = MagicMock()
        rget.text = _metrics(scrape_up)
        rget.status_code = 200
        rget.raise_for_status.return_value = None
        envd = {'PULSE_HEALTH_EXPORTER_URL': 'http://x/metrics'}
        envd.update(env or {})
        with patch.dict('os.environ', envd, clear=False), \
             patch('tasks.services.workflow_service.WorkflowService._redis_client', return_value=fake), \
             patch('requests.get', return_value=rget), \
             patch('urllib.request.urlopen', return_value=resp):
            # bound celery task (self unused by the body); .run() executes the body.
            out = T.sweep_agent_availability.run()
        written = json.loads(fake.store['pulse:agent_availability'])
        return out, written, fake

    def test_mass_bare_flip_is_failstale(self):
        # 10 agents up last sweep; daemon down -> exporter reports all process_up=0.
        prev = {f"a{i}@shizuha.com": True for i in range(10)}
        scrape = {f"a{i}": 0 for i in range(10)}
        out, written, fake = self._run(prev, scrape)
        self.assertTrue(out.get('source_degraded'), out)
        # all 10 preserved UP (fail-stale), not mass-failed-over
        self.assertTrue(all(written.get(f"a{i}@shizuha.com") for i in range(10)), written)
        self.assertIn('pulse:availability_source_degraded', fake.store)

    def test_small_real_outage_still_fails_over(self):
        # 10 up; only 1 drops -> below mass-flip threshold -> normal (agent marked down).
        prev = {f"a{i}@shizuha.com": True for i in range(10)}
        scrape = {f"a{i}": (0 if i == 0 else 1) for i in range(10)}
        out, written, fake = self._run(prev, scrape)
        self.assertFalse(out.get('source_degraded'), out)
        self.assertFalse(written.get("a0@shizuha.com"), written)
        self.assertNotIn('pulse:availability_source_degraded', fake.store)

    def test_failstale_disabled_by_flag(self):
        prev = {f"a{i}@shizuha.com": True for i in range(10)}
        scrape = {f"a{i}": 0 for i in range(10)}
        out, written, fake = self._run(prev, scrape, env={'PULSE_AVAILABILITY_FAILSTALE_ENABLED': '0'})
        self.assertFalse(out.get('source_degraded'), out)
        # with guard off, the mass-down is applied (agents marked down)
        self.assertFalse(any(written.get(f"a{i}@shizuha.com") for i in range(10)), written)

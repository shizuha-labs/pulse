"""PLAT-9318 regression: a live seat must never be verdict-disabled by a
stale compatibility-file fold.

The sweep's ``shizuha_agent_enabled`` fold (operator-toggle visibility,
PLAT-291) trusts the exporter's file-sourced enabled gauge. When the typed
``shizuha_agent_lifecycle_state`` sample says ``enabled`` while the file fold
says ``enabled=0``, the file sample is provably contradicted (stale exporter
gather / not-yet-rewritten status field) and the old code verdict-disabled a
demonstrably live seat — kei/haru misreads on 09-19 (heartbeats + merges all
session while clear comments cited ``status=disabled``), fueling the
PLS-524/204 clear -> PLS-188 re-materialize cycle and the CTX-577/626
auto-failover churn. The fix fails OPEN toward available on the contradiction;
a genuine operator stop flips the typed lifecycle to ``operator_stopped``,
which still fails closed.
"""
import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_agent_availability


def _urlopen_returning(text):
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _requests_get_returning(text):
    resp = MagicMock()
    resp.text = text
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return MagicMock(return_value=resp)


def _metrics(enabled_val, lifecycle_state=None):
    """Exporter scrape for kei: process-up, provider-healthy, the given
    enabled gauge, and an optional typed lifecycle sample."""
    lines = [
        'shizuha_agent_process_up{agent="kei",backend="codex"} 1',
        f'shizuha_agent_enabled{{agent="kei"}} {enabled_val}',
        'shizuha_agent_provider_healthy{agent="kei"} 1',
        'shizuha_agent_empty_turn_streak{agent="kei"} 0',
    ]
    if lifecycle_state:
        lines.append(
            f'shizuha_agent_lifecycle_state{{agent="kei",state="{lifecycle_state}"}} 1')
    return "\n".join(lines)


class Plat9318LiveSeatNotDisabledTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None

    def _run_sweep(self, metrics_text):
        captured = {}
        redis_mock = MagicMock()
        redis_mock.get.return_value = None

        def _setex(key, ttl, val):
            if key == 'pulse:agent_availability':
                captured["map"] = json.loads(val)

        redis_mock.setex.side_effect = _setex

        with patch("urllib.request.urlopen", _urlopen_returning(metrics_text)), \
             patch("requests.get", _requests_get_returning(metrics_text)), \
             patch(
                 "tasks.services.workflow_service.WorkflowService._redis_client",
                 return_value=redis_mock,
             ), \
             patch.dict(
                 "os.environ",
                 {
                     "PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics",
                     "PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED": "0",
                 },
             ):
            sweep_agent_availability.run()
        return captured.get("map", {})

    def test_live_seat_with_contradicted_file_fold_stays_available(self):
        """lifecycle=enabled (typed SoT) + enabled=0 (stale file fold) +
        process_up=1 -> the seat stays available (fail-open)."""
        avail_map = self._run_sweep(_metrics(0, lifecycle_state='enabled'))
        self.assertTrue(avail_map.get('kei@shizuha.com'))

    def test_operator_stop_still_fails_closed(self):
        """lifecycle=operator_stopped + enabled=0 -> unavailable (PLAT-291
        preserved; the typed SoT wins in the stop direction)."""
        avail_map = self._run_sweep(_metrics(0, lifecycle_state='operator_stopped'))
        self.assertFalse(avail_map.get('kei@shizuha.com'))

    def test_file_fold_without_lifecycle_sample_still_applies(self):
        """No lifecycle sample (older exporter) + enabled=0 -> unavailable
        (forward-compatible: the file fold keeps its old behavior)."""
        avail_map = self._run_sweep(_metrics(0, lifecycle_state=None))
        self.assertFalse(avail_map.get('kei@shizuha.com'))

    def test_enabled_seat_unaffected(self):
        """lifecycle=enabled + enabled=1 -> available (baseline sanity)."""
        avail_map = self._run_sweep(_metrics(1, lifecycle_state='enabled'))
        self.assertTrue(avail_map.get('kei@shizuha.com'))

"""PLAT-291 regression: the availability sweep must consult operator toggle
state (status=disabled/paused via toggle_agent), not just process_up.

A toggled-OFF agent whose container is still up reports process_up=1, so before
this fix the resolver kept it "available" and the rebalance sweep assigned it
work it could never pick up -> assign->failover churn loop. The exporter now
emits shizuha_agent_enabled{agent=...} (1=active, 0=disabled/paused) and
sweep_agent_availability folds it into pulse:agent_availability exactly like the
wedge/provider folds. These tests prove a disabled roster member is excluded,
and that a missing metric (older exporter) changes nothing (forward-compatible).
"""
import json
from datetime import timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    sweep_agent_availability, sweep_org_health,
    _org_health_fresh_agent_sessions, _org_health_throughput_window_minutes,
    _org_health_capacity_starve_grace_hours, _apply_provider_down_cooldown,
)


def _urlopen_returning(text):
    """Build a urlopen() replacement whose context manager yields `text`."""
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _requests_get_returning(text):
    """Build a requests.get() replacement whose response yields `text`.

    The exporter scrape in sweep_agent_availability migrated from
    urllib.request.urlopen to requests.get; the connect-telemetry and
    runtime-fleet fetches still go through urllib (directly / via safe_urlopen),
    so tests patch both. This covers the exporter side.
    """
    resp = MagicMock()
    resp.text = text
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return MagicMock(return_value=resp)


class OrgHealthCapacityStarveGraceTests(SimpleTestCase):
    def test_capacity_starve_grace_defaults_to_thirty_minutes(self):
        with patch.dict("os.environ", {"PULSE_SUBSTRATE_CAP_MODE": "off"}, clear=True):
            self.assertEqual(_org_health_capacity_starve_grace_hours(), 0.5)

    def test_capacity_starve_grace_honors_rebalance_interval_floor(self):
        with patch.dict("os.environ", {
            "PULSE_REBALANCE_SWEEP_INTERVAL_MINUTES": "45",
        }, clear=True):
            self.assertEqual(_org_health_capacity_starve_grace_hours(), 0.75)

    def test_capacity_starve_grace_allows_explicit_override(self):
        with patch.dict("os.environ", {
            "PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES": "5",
            "PULSE_REBALANCE_SWEEP_INTERVAL_MINUTES": "45",
        }, clear=True):
            self.assertEqual(_org_health_capacity_starve_grace_hours(), 5 / 60)


class ProviderDownCooldownTests(SimpleTestCase):
    def test_provider_down_cooldown_keeps_flapping_agent_unavailable(self):
        """A one-scrape recovery must not make rebalance keep work on a flapping
        provider-down agent."""
        avail = {
            "kai@shizuha.com": True,
            "nagi@shizuha.com": True,
        }
        provider_down = []

        sticky, next_until = _apply_provider_down_cooldown(
            avail,
            provider_down,
            {"kai@shizuha.com": 1000},
            now_ts=500,
            cooldown_seconds=900,
        )

        self.assertEqual(sticky, ["kai@shizuha.com"])
        self.assertFalse(avail["kai@shizuha.com"])
        self.assertTrue(avail["nagi@shizuha.com"])
        self.assertIn("kai@shizuha.com", provider_down)
        self.assertEqual(next_until["kai@shizuha.com"], 1000)

    def test_provider_down_cooldown_extends_on_fresh_down_sample(self):
        avail = {"kai@shizuha.com": False}
        provider_down = ["kai@shizuha.com"]

        sticky, next_until = _apply_provider_down_cooldown(
            avail,
            provider_down,
            {"kai@shizuha.com": 1000},
            now_ts=500,
            cooldown_seconds=900,
        )

        self.assertEqual(sticky, [])
        self.assertFalse(avail["kai@shizuha.com"])
        self.assertEqual(provider_down, ["kai@shizuha.com"])
        self.assertEqual(next_until["kai@shizuha.com"], 1400)


class Plat291ToggleAvailabilityTests(TestCase):
    def setUp(self):
        super().setUp()
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None

    def _run_sweep(self, metrics_text):
        """Run the sweep against `metrics_text`; return (result, written_map)."""
        captured = {}
        redis_mock = MagicMock()
        redis_mock.get.return_value = None

        def _setex(key, ttl, val):
            captured["key"] = key
            # PLAT-350: sweep_agent_availability now also writes pulse:agent_disabled;
            # capture ONLY the availability map so that write doesn't clobber it.
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
            result = sweep_agent_availability.run()
        return result, captured.get("map", {})

    def test_runtime_health_down_overrides_stale_db_active_row(self):
        """Claude quota/provider exhaustion must route around an agent even when
        an old durable availability row still says active=True."""
        AgentAvailability.objects.create(
            email="shizuha@shizuha.com",
            active=True,
            status="active",
            reason="stale force-active row",
            updated_by="test",
        )

        with patch(
            "tasks.services.workflow_service.WorkflowService._health_availability_map",
            return_value={"shizuha@shizuha.com": False},
        ), patch(
            "tasks.services.workflow_service.WorkflowService._disabled_agent_emails",
            return_value=set(),
        ):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment("shizuha@shizuha.com")
            )

    def test_live_health_overrides_stale_health_sweep_inactive_row(self):
        """Health-sweep-managed inactive rows persist outage state, but they must
        not permanently block an agent after the runtime health map recovers."""
        AgentAvailability.objects.create(
            email="scout@shizuha.com",
            active=False,
            status="inactive",
            reason="provider exhausted",
            updated_by="health-sweep",
        )

        with patch(
            "tasks.services.workflow_service.WorkflowService._health_availability_map",
            return_value={"scout@shizuha.com": True},
        ), patch(
            "tasks.services.workflow_service.WorkflowService._redis_client",
        ) as redis_client:
            redis_client.return_value.get.return_value = None
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment("scout@shizuha.com")
            )

    def test_operator_inactive_row_still_overrides_live_health(self):
        """Only health-managed rows auto-clear. Operator/on-leave inactive rows
        remain durable even if a process heartbeat says the agent is reachable."""
        AgentAvailability.objects.create(
            email="operator-disabled@shizuha.com",
            active=False,
            status="on_leave",
            reason="operator hold",
            updated_by="operator",
        )

        with patch(
            "tasks.services.workflow_service.WorkflowService._health_availability_map",
            return_value={"operator-disabled@shizuha.com": True},
        ), patch(
            "tasks.services.workflow_service.WorkflowService._redis_client",
        ) as redis_client:
            redis_client.return_value.get.return_value = None
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment("operator-disabled@shizuha.com")
            )

    def test_unknown_agent_is_unavailable_when_health_map_is_live(self):
        """A populated health map means the fleet sweep is working. Roster emails
        with no primary or @agents alias sample are ghost identities, not
        assignable peers."""
        with patch(
            "tasks.services.workflow_service.WorkflowService._health_availability_map",
            return_value={"shion@shizuha.com": True},
        ), patch(
            "tasks.services.workflow_service.WorkflowService._disabled_agent_emails",
            return_value=set(),
        ):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment("sumi@shizuha.com")
            )

    def test_unknown_agent_still_fails_open_when_health_map_is_down(self):
        """If the health map is empty because Redis/sweep is down, preserve the
        legacy fail-open behavior so routing does not freeze the whole org."""
        with patch(
            "tasks.services.workflow_service.WorkflowService._health_availability_map",
            return_value={},
        ), patch(
            "tasks.services.workflow_service.WorkflowService._disabled_agent_emails",
            return_value=set(),
        ):
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment("sumi@shizuha.com")
            )

    def test_connect_pool_unavailable_demotes_only_reporting_agent(self):
        """One exhausted Claude credential pool must not disable healthy pools."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="shizuha",backend="claude"} 1',
            'shizuha_agent_process_up{agent="shion",backend="codex"} 1',
            'shizuha_agent_enabled{agent="shizuha",backend="claude"} 1',
            'shizuha_agent_enabled{agent="shion",backend="codex"} 1',
        ]) + "\n"
        connect_payload = {
            "agents": {
                "cora": {
                    "runtime": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-6",
                        "harness": "claude-bridge",
                    },
                    "health": {
                        "token_pool_unavailable": True,
                        "provider_unavailable": True,
                    },
                },
                "shion": {
                    "runtime": {
                        "provider": "openai",
                        "model": "gpt-5.5",
                        "harness": "codex-bridge",
                    },
                    "health": {"ok": True},
                },
            },
        }
        captured = {}
        redis_mock = MagicMock()
        redis_mock.scan_iter.return_value = []
        redis_mock.get.return_value = None

        def _setex(key, ttl, val):
            if key == "pulse:agent_availability":
                captured["map"] = json.loads(val)

        redis_mock.setex.side_effect = _setex

        def _urlopen(url, timeout=10):
            if url == "http://exporter.test/metrics":
                body = metrics
            elif url == "http://connect.test/api/internal/agents/telemetry/":
                body = json.dumps(connect_payload)
            else:
                raise AssertionError(f"unexpected urlopen target: {url}")
            resp = MagicMock()
            resp.read.return_value = body.encode("utf-8")
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        def _rget(url, timeout=10, **kw):
            # exporter scrape migrated to requests.get; other endpoints still
            # use urllib (patched above via _urlopen).
            if url == "http://exporter.test/metrics":
                resp = MagicMock()
                resp.text = metrics
                resp.status_code = 200
                resp.raise_for_status.return_value = None
                return resp
            raise AssertionError(f"unexpected requests.get target: {url}")

        with patch("urllib.request.urlopen", side_effect=_urlopen), \
             patch("requests.get", side_effect=_rget), \
             patch(
                 "tasks.services.workflow_service.WorkflowService._redis_client",
                 return_value=redis_mock,
             ), \
             patch.dict(
                 "os.environ",
                 {
                     "PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics",
                     "PULSE_CONNECT_API_URL": "http://connect.test/api",
                     # Instant demotion for this unit test; production defaults
                     # duration-gate Connect health bad samples.
                     "PULSE_HEALTH_DEMOTE_AFTER_SEC": "0",
                     "PULSE_HEALTH_RECOVER_AFTER_SEC": "0",
                     "PULSE_PROVIDER_DOWN_COOLDOWN_SECONDS": "0",
                },
             ):
            result = sweep_agent_availability.run()

        self.assertTrue(result["ok"])
        self.assertTrue(captured["map"]["shizuha@shizuha.com"])
        self.assertTrue(captured["map"]["shion@shizuha.com"])
        self.assertFalse(captured["map"]["cora@shizuha.com"])
        self.assertIn("cora@shizuha.com", result["provider_down"])
        self.assertIn("cora@shizuha.com", result["down"])
        self.assertNotIn("shizuha@shizuha.com", result["down"])
        self.assertNotIn("shion@shizuha.com", result["down"])

    def test_disabled_agent_marked_unavailable(self):
        """A roster member toggled off (enabled=0) is excluded from the
        availability map even though its bridge process is up (process_up=1) —
        the exact assign->failover churn scenario from the bug report."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="ryo",backend="claude"} 1',
            'shizuha_agent_process_up{agent="aoi",backend="claude"} 1',
            'shizuha_agent_process_up{agent="sora",backend="codex"} 1',
            'shizuha_agent_enabled{agent="ryo",backend="claude"} 1',
            'shizuha_agent_enabled{agent="aoi",backend="claude"} 1',
            'shizuha_agent_enabled{agent="sora",backend="codex"} 0',
        ]) + "\n"

        result, avail = self._run_sweep(metrics)

        self.assertTrue(result["ok"])
        # sora is process_up but toggled OFF -> must be unavailable.
        self.assertIn("sora@shizuha.com", result["disabled"])
        self.assertFalse(avail["sora@shizuha.com"])
        self.assertIn("sora@shizuha.com", result["down"])
        # active peers stay available.
        self.assertTrue(avail["ryo@shizuha.com"])
        self.assertTrue(avail["aoi@shizuha.com"])
        self.assertNotIn("ryo@shizuha.com", result["disabled"])

    def test_missing_enabled_metric_is_noop(self):
        """Forward-compatibility: an older exporter that emits no
        shizuha_agent_enabled samples must not mark anyone disabled."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="ryo",backend="claude"} 1',
            'shizuha_agent_process_up{agent="sora",backend="codex"} 1',
        ]) + "\n"

        result, avail = self._run_sweep(metrics)

        self.assertTrue(result["ok"])
        self.assertEqual(result["disabled"], [])
        self.assertTrue(avail["ryo@shizuha.com"])
        self.assertTrue(avail["sora@shizuha.com"])

    def test_enabled_one_keeps_agent_available(self):
        """enabled=1 must not flip an otherwise-up agent to unavailable."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="ryo",backend="claude"} 1',
            'shizuha_agent_enabled{agent="ryo",backend="claude"} 1',
        ]) + "\n"

        result, avail = self._run_sweep(metrics)

        self.assertTrue(result["ok"])
        self.assertEqual(result["disabled"], [])
        self.assertTrue(avail["ryo@shizuha.com"])

    def test_external_liveness_marks_remote_agent_available(self):
        """PLAT-694: a remote/BYO agent with recent platform activity but no
        local Docker process sample is still available for routing."""
        metrics = "\n".join([
            'shizuha_agent_external_liveness{agent="miya",source="pulse_activity"} 1',
        ]) + "\n"

        result, avail = self._run_sweep(metrics)

        self.assertTrue(result["ok"])
        self.assertEqual(result["external_live"], ["miya@shizuha.com"])
        self.assertTrue(avail["miya@shizuha.com"])
        self.assertNotIn("miya@shizuha.com", result["down"])

    def test_disabled_metric_overrides_external_liveness(self):
        """Operator toggles remain authoritative even if a disabled remote
        runtime emits a recent platform-activity liveness signal."""
        metrics = "\n".join([
            'shizuha_agent_external_liveness{agent="taki",source="pulse_activity"} 1',
            'shizuha_agent_enabled{agent="taki",backend="remote"} 0',
        ]) + "\n"

        result, avail = self._run_sweep(metrics)

        self.assertTrue(result["ok"])
        self.assertEqual(result["external_live"], ["taki@shizuha.com"])
        self.assertIn("taki@shizuha.com", result["disabled"])
        self.assertFalse(avail["taki@shizuha.com"])
        self.assertIn("taki@shizuha.com", result["down"])

    def test_disabled_metric_overrides_pulse_heartbeat(self):
        """A recent Pulse heartbeat only proves the agent reached Pulse. The
        daemon/operator disabled signal remains authoritative, including for
        stopped agents with no process_up sample when the lifecycle sample is
        current or has no event-time marker."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="shion",backend="codex"} 1',
            'shizuha_agent_enabled{agent="shion",backend="codex"} 1',
            'shizuha_agent_enabled{agent="sara",backend="claude"} 0',
        ]) + "\n"
        captured = {}
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        redis_mock.scan_iter.return_value = [
            b"pulse:agent_hb:sara@shizuha.com",
        ]

        def _setex(key, ttl, val):
            if key == "pulse:agent_availability":
                captured["map"] = json.loads(val)
            elif key == "pulse:agent_disabled":
                captured["disabled"] = json.loads(val)

        redis_mock.setex.side_effect = _setex

        with patch("urllib.request.urlopen", _urlopen_returning(metrics)), \
             patch("requests.get", _requests_get_returning(metrics)), \
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
            result = sweep_agent_availability.run()

        self.assertTrue(result["ok"])
        self.assertTrue(captured["map"]["shion@shizuha.com"])
        self.assertFalse(captured["map"]["sara@shizuha.com"])
        self.assertIn("sara@shizuha.com", result["disabled"])
        self.assertIn("sara@shizuha.com", result["down"])
        self.assertIn("sara@shizuha.com", captured["disabled"])

    def test_newer_heartbeat_rejects_cached_pre_start_disabled_sample(self):
        """A native Hive start must not be undone by an older cached scrape."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="kane",backend="codex"} 0',
            'shizuha_agent_enabled{agent="kane",backend="codex"} 0',
            'shizuha_agent_enabled_snapshot_timestamp_seconds 100',
        ]) + "\n"
        captured = {}
        redis_mock = MagicMock()
        redis_mock.scan_iter.return_value = [
            b"pulse:agent_hb:kane@shizuha.com",
        ]

        def _get(key):
            if key == b"pulse:agent_hb:kane@shizuha.com":
                return b"101"
            return None

        def _setex(key, ttl, val):
            if key == "pulse:agent_availability":
                captured["map"] = json.loads(val)
            elif key == "pulse:agent_disabled":
                captured["disabled"] = json.loads(val)

        redis_mock.get.side_effect = _get
        redis_mock.setex.side_effect = _setex

        with patch("requests.get", _requests_get_returning(metrics)), \
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
            result = sweep_agent_availability.run()

        self.assertTrue(result["ok"])
        self.assertTrue(captured["map"]["kane@shizuha.com"])
        self.assertNotIn("kane@shizuha.com", captured["disabled"])
        self.assertEqual(
            result["stale_disabled_ignored"],
            ["kane@shizuha.com"],
        )
        self.assertFalse(
            AgentAvailability.objects.filter(email="kane@shizuha.com").exists()
        )

    def test_newer_heartbeat_does_not_override_disabled_live_process(self):
        """A host/operator stop remains authoritative while its process exits."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="kane",backend="codex"} 1',
            'shizuha_agent_enabled{agent="kane",backend="codex"} 0',
            'shizuha_agent_enabled_snapshot_timestamp_seconds 100',
        ]) + "\n"
        captured = {}
        redis_mock = MagicMock()
        redis_mock.scan_iter.return_value = [
            b"pulse:agent_hb:kane@shizuha.com",
        ]

        def _get(key):
            if key == b"pulse:agent_hb:kane@shizuha.com":
                return b"101"
            return None

        def _setex(key, ttl, val):
            if key == "pulse:agent_availability":
                captured["map"] = json.loads(val)
            elif key == "pulse:agent_disabled":
                captured["disabled"] = json.loads(val)

        redis_mock.get.side_effect = _get
        redis_mock.setex.side_effect = _setex

        with patch("requests.get", _requests_get_returning(metrics)), \
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
            result = sweep_agent_availability.run()

        self.assertTrue(result["ok"])
        self.assertFalse(captured["map"]["kane@shizuha.com"])
        self.assertIn("kane@shizuha.com", captured["disabled"])
        self.assertEqual(result["stale_disabled_ignored"], [])
        self.assertFalse(
            AgentAvailability.objects.get(email="kane@shizuha.com").active
        )

    def test_newer_disabled_sample_still_overrides_older_heartbeat(self):
        """Event ordering must not weaken an intentional operator stop."""
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="kane",backend="codex"} 1',
            'shizuha_agent_enabled{agent="kane",backend="codex"} 0',
            'shizuha_agent_enabled_snapshot_timestamp_seconds 102',
        ]) + "\n"
        captured = {}
        redis_mock = MagicMock()
        redis_mock.scan_iter.return_value = [
            b"pulse:agent_hb:kane@shizuha.com",
        ]

        def _get(key):
            if key == b"pulse:agent_hb:kane@shizuha.com":
                return b"101"
            return None

        def _setex(key, ttl, val):
            if key == "pulse:agent_availability":
                captured["map"] = json.loads(val)
            elif key == "pulse:agent_disabled":
                captured["disabled"] = json.loads(val)

        redis_mock.get.side_effect = _get
        redis_mock.setex.side_effect = _setex

        with patch("requests.get", _requests_get_returning(metrics)), \
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
            result = sweep_agent_availability.run()

        self.assertTrue(result["ok"])
        self.assertFalse(captured["map"]["kane@shizuha.com"])
        self.assertIn("kane@shizuha.com", captured["disabled"])
        self.assertEqual(result["stale_disabled_ignored"], [])
        self.assertFalse(
            AgentAvailability.objects.get(email="kane@shizuha.com").active
        )

class Pls33OrgHealthFlatlineTests(SimpleTestCase):
    def test_fresh_agent_session_parser_counts_recent_enabled_processes(self):
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="ryo",backend="claude"} 1',
            'shizuha_agent_last_activity_seconds{agent="ryo",backend="claude"} 120',
            'shizuha_agent_enabled{agent="ryo",backend="claude"} 1',
            'shizuha_agent_process_up{agent="sara",backend="claude"} 1',
            'shizuha_agent_last_activity_seconds{agent="sara",backend="claude"} 60',
            'shizuha_agent_enabled{agent="sara",backend="claude"} 0',
            'shizuha_agent_process_up{agent="old",backend="claude"} 1',
            'shizuha_agent_last_activity_seconds{agent="old",backend="claude"} 4000',
        ]) + "\n"

        with patch("urllib.request.urlopen", _urlopen_returning(metrics)), \
             patch("requests.get", _requests_get_returning(metrics)), \
             patch.dict("os.environ", {"PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics"}):
            result = _org_health_fresh_agent_sessions(max_age_seconds=1800)

        self.assertEqual(result["fresh_count"], 1)
        self.assertEqual(result["fresh_agents"], ["ryo@shizuha.com"])

    def test_fresh_agent_session_parser_requires_same_backend_up_and_recent(self):
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="ryo",backend="claude"} 1',
            'shizuha_agent_last_activity_seconds{agent="ryo",backend="claude"} 4000',
            'shizuha_agent_enabled{agent="ryo",backend="claude"} 1',
            'shizuha_agent_process_up{agent="ryo",backend="codex"} 0',
            'shizuha_agent_last_activity_seconds{agent="ryo",backend="codex"} 60',
            'shizuha_agent_enabled{agent="ryo",backend="codex"} 1',
        ]) + "\n"

        with patch("urllib.request.urlopen", _urlopen_returning(metrics)), \
             patch("requests.get", _requests_get_returning(metrics)), \
             patch.dict("os.environ", {"PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics"}):
            result = _org_health_fresh_agent_sessions(max_age_seconds=1800)

        self.assertEqual(result["fresh_count"], 0)
        self.assertEqual(result["fresh_agents"], [])

    def test_fresh_agent_session_parser_excludes_wedged_or_provider_down_backends(self):
        metrics = "\n".join([
            'shizuha_agent_process_up{agent="wedged",backend="claude"} 1',
            'shizuha_agent_last_activity_seconds{agent="wedged",backend="claude"} 60',
            'shizuha_agent_enabled{agent="wedged",backend="claude"} 1',
            'shizuha_agent_wedge_errors{agent="wedged",backend="claude"} 20',
            'shizuha_agent_process_up{agent="providerdown",backend="glm"} 1',
            'shizuha_agent_last_activity_seconds{agent="providerdown",backend="glm"} 60',
            'shizuha_agent_enabled{agent="providerdown",backend="glm"} 1',
            'shizuha_agent_provider_healthy{agent="providerdown",backend="glm"} 0',
            'shizuha_agent_process_up{agent="healthy",backend="claude"} 1',
            'shizuha_agent_last_activity_seconds{agent="healthy",backend="claude"} 60',
            'shizuha_agent_enabled{agent="healthy",backend="claude"} 1',
            'shizuha_agent_provider_healthy{agent="healthy",backend="claude"} 1',
        ]) + "\n"

        with patch("urllib.request.urlopen", _urlopen_returning(metrics)), \
             patch("requests.get", _requests_get_returning(metrics)), \
             patch.dict("os.environ", {
                 "PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics",
                 "PULSE_WEDGE_THRESHOLD": "10",
             }):
            result = _org_health_fresh_agent_sessions(max_age_seconds=1800)

        self.assertEqual(result["fresh_count"], 1)
        self.assertEqual(result["fresh_agents"], ["healthy@shizuha.com"])

    def test_throughput_window_widens_during_ist_overnight(self):
        with patch.dict("os.environ", {
            "PULSE_THROUGHPUT_WINDOW_MIN": "20",
            "PULSE_THROUGHPUT_OVERNIGHT_WINDOW_MIN": "60",
            "PULSE_THROUGHPUT_OVERNIGHT_START_IST_HOUR": "22",
            "PULSE_THROUGHPUT_OVERNIGHT_END_IST_HOUR": "9",
        }):
            # 18:00 UTC = 23:30 IST, inside the overnight lull window.
            self.assertEqual(
                _org_health_throughput_window_minutes(timezone.datetime(2026, 6, 13, 18, 0, tzinfo=dt_timezone.utc)),
                60,
            )
            # 04:30 UTC = 10:00 IST, outside the overnight window.
            self.assertEqual(
                _org_health_throughput_window_minutes(timezone.datetime(2026, 6, 14, 4, 30, tzinfo=dt_timezone.utc)),
                20,
            )

    def test_org_health_suppresses_flatline_when_sessions_fresh_and_work_in_progress(self):
        class FakeQS(list):
            def exclude(self, *args, **kwargs):
                return self
            def count(self):
                return len(self)
            def first(self):
                return self[0] if self else None
            def order_by(self, *args):
                return self

        class FakeItemManager:
            def filter(self, **kwargs):
                if kwargs.get("assignment_group") == "program-management":
                    return FakeQS([])
                statuses = kwargs.get("status__in")
                if statuses is not None:
                    # The actionable-depth query and the heads-down working-depth query
                    # both return enough work to suppress, but the legacy exact
                    # status='in_progress' path below intentionally returns zero.
                    if set(statuses) == {"open", "todo", "in_progress", "in_review"}:
                        return FakeQS([object()] * 10)
                    if set(statuses) == set(WorkflowService._WORKING_STATUSES):
                        return FakeQS([object()] * 10)
                if kwargs.get("status") == "in_progress":
                    return FakeQS([])
                return FakeQS([])

        class FakeActivityManager:
            def filter(self, **kwargs):
                return FakeQS([])

        class FakeProjectManager:
            def filter(self, **kwargs):
                return FakeQS([])
            def order_by(self, *args):
                return FakeQS([])

        FakeItem = type("FakeItem", (), {"objects": FakeItemManager()})
        FakeItemActivity = type("FakeItemActivity", (), {"objects": FakeActivityManager()})
        FakeProject = type("FakeProject", (), {"objects": FakeProjectManager()})
        FakeComment = type("FakeComment", (), {"objects": object()})

        with patch("tasks.models.Item", FakeItem), \
             patch("tasks.models.ItemActivity", FakeItemActivity), \
             patch("tasks.models.Project", FakeProject), \
             patch("tasks.models.Comment", FakeComment), \
             patch("tasks.tasks._run_workflow_stale_timers", return_value=0), \
             patch("tasks.tasks._org_health_exact_routing_lanes", return_value=[]), \
             patch.object(WorkflowService, "_POD_ROSTER", {"program-management": []}), \
             patch("tasks.tasks._org_health_throughput_window_minutes", return_value=20), \
             patch("tasks.tasks._org_health_fresh_agent_sessions", return_value={
                 "fresh_count": 3,
                 "fresh_agents": ["a@shizuha.com", "b@shizuha.com", "c@shizuha.com"],
             }), \
             patch.dict("os.environ", {
                 "PULSE_THROUGHPUT_MIN_ACTIONABLE": "10",
                 "PULSE_THROUGHPUT_FRESH_AGENTS_MIN": "3",
                 "PULSE_THROUGHPUT_IN_PROGRESS_MIN": "10",
             }):
            result = sweep_org_health.run()

        self.assertEqual(result["findings"], 0)
        self.assertEqual(result["filed"], 0)


    def test_org_health_capacity_starve_grace_suppresses_fresh_queue_items(self):
        """PLAT-1246: a brand-new queued item must get a rebalance grace window.

        Even if an under-cap available member is visible, org-health should not
        file `capacity-starve:<team>` before the rebalance sweep has had a full
        chance to assign/reroute the task.
        """
        class FakeQS(list):
            def exclude(self, *args, **kwargs):
                return self
            def count(self):
                return len(self)
            def first(self):
                return self[0] if self else None
            def order_by(self, *args):
                return self

        class Queued:
            item_key = 'PLAT-FRESH'
            created_at = timezone.now() - timezone.timedelta(minutes=5)

        created = []

        class FakeItemManager:
            def filter(self, **kwargs):
                if (kwargs.get('mode') == 'task'
                        and kwargs.get('item_type') == 'qa.cadence'):
                    return FakeQS([])
                if kwargs.get('assignment_group') == 'program-management':
                    return FakeQS([])
                if kwargs.get('assignment_group') == 'admin-ops':
                    return FakeQS([])
                if kwargs.get('assignee_id') == 123:
                    return FakeQS([])
                if kwargs.get('status__in') is not None:
                    return FakeQS([])
                return FakeQS([])
            def create(self, **kwargs):
                created.append(kwargs)
                return type(
                    'Created', (),
                    {'item_key': 'PLAT-NEW', 'assign': lambda self, *a, **k: None},
                )()

        class FakeProjectManager:
            def filter(self, **kwargs):
                return FakeQS([
                    type('Project', (), {'get_next_item_key': lambda self: 'PLAT-NEW'})(),
                ])
            def order_by(self, *args):
                return FakeQS([])

        FakeItem = type('FakeItem', (), {'objects': FakeItemManager()})
        FakeItemActivity = type(
            'FakeItemActivity', (),
            {'objects': type('Mgr', (), {'filter': lambda self, **kwargs: FakeQS([])})()},
        )
        FakeProject = type('FakeProject', (), {'objects': FakeProjectManager()})
        FakeComment = type(
            'FakeComment', (),
            {'objects': type('Mgr', (), {'create': lambda self, **kwargs: None})()},
        )

        with patch('tasks.models.Item', FakeItem), \
             patch('tasks.models.ItemActivity', FakeItemActivity), \
             patch('tasks.models.Project', FakeProject), \
             patch('tasks.models.Comment', FakeComment), \
             patch('tasks.tasks._run_workflow_stale_timers', return_value=0), \
             patch('tasks.tasks._org_health_exporter_fresh', return_value=True), \
             patch(
                 'tasks.tasks._org_health_candidate_availability',
                 return_value={'kai@shizuha.com': ['available']},
             ), \
             patch('tasks.tasks._org_health_exact_routing_lanes', return_value=[{
                 'organization_id': 1,
                 'organization_slug': 'shizuha',
                 'team_key': 'engineering',
                 'source': 'team_roster_compat',
                 'source_version': 'fixture',
                 'members': ['kai@shizuha.com'],
                 'candidates': [{'email': 'kai@shizuha.com'}],
                 'queued': [Queued()],
             }]), \
             patch.object(WorkflowService, '_pod_roster', return_value={
                 'engineering': ['kai@shizuha.com'],
                 'program-management': [],
             }), \
             patch.object(
                 WorkflowService, '_queued_tasks_for_team',
                 return_value=FakeQS([Queued()]),
             ), \
             patch.object(WorkflowService, '_flat_roster', return_value=['kai@shizuha.com']), \
             patch.object(WorkflowService, '_disabled_agent_emails', return_value=set()), \
             patch.object(
                 WorkflowService, '_is_agent_available_for_assignment', return_value=True,
             ), \
             patch(
                 'tasks.user_service.user_service.get_user_by_email',
                 return_value={'id': '123'},
             ), \
             patch.object(
                 WorkflowService, '_resolve_team_assignee', return_value=(None, None),
             ), \
             patch.dict('os.environ', {
                 'PULSE_QUEUE_AGE_SLA_HOURS': '96',
                 'PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES': '30',
             }):
            result = sweep_org_health.run()

        self.assertEqual(result['findings'], 0)
        self.assertEqual(result['filed'], 0)
        self.assertEqual(created, [])


    def test_org_health_deferred_signal_suppresses_capacity_starve_refile(self):
        """PLAT-756: deferred PM canonicals still count as existing signals.

        `deferred` is terminal for normal queueing, but an org-health signal that
        was intentionally parked must suppress re-fires for the same stable
        `(team, condition)` key; otherwise count-bearing titles create one new
        Program Mgmt task every sweep.
        """
        class FakeQS(list):
            def exclude(self, *args, **kwargs):
                return self
            def count(self):
                return len(self)
            def first(self):
                return self[0] if self else None
            def order_by(self, *args):
                return self

        class Existing:
            status = 'deferred'
            metadata = {'org_health_signal': 'capacity-starve:1:engineering'}
            item_key = 'PLAT-752'

        class Queued:
            item_key = 'PLAT-999'
            created_at = timezone.now() - timezone.timedelta(hours=200)

        created = []

        class FakeItemManager:
            def filter(self, **kwargs):
                if (kwargs.get('mode') == 'task'
                        and kwargs.get('item_type') == 'qa.cadence'):
                    return FakeQS([])
                if kwargs.get('assignment_group') == 'program-management':
                    return FakeQS([Existing()])
                if kwargs.get('assignment_group') == 'admin-ops':
                    return FakeQS([])
                return FakeQS([])
            def create(self, **kwargs):
                created.append(kwargs)
                return type('Created', (), {'item_key': 'PLAT-NEW', 'assign': lambda self, *a, **k: None})()

        class FakeProjectManager:
            def filter(self, **kwargs):
                return FakeQS([type('Project', (), {'get_next_item_key': lambda self: 'PLAT-NEW'})()])
            def order_by(self, *args):
                return FakeQS([])

        FakeItem = type('FakeItem', (), {'objects': FakeItemManager()})
        FakeItemActivity = type(
            'FakeItemActivity', (),
            {'objects': type('Mgr', (), {'filter': lambda self, **kwargs: FakeQS([])})()},
        )
        FakeProject = type('FakeProject', (), {'objects': FakeProjectManager()})
        FakeComment = type('FakeComment', (), {'objects': type('Mgr', (), {'create': lambda self, **kwargs: None})()})

        with patch('tasks.models.Item', FakeItem), \
             patch('tasks.models.ItemActivity', FakeItemActivity), \
             patch('tasks.models.Project', FakeProject), \
             patch('tasks.models.Comment', FakeComment), \
             patch('tasks.tasks._run_workflow_stale_timers', return_value=0), \
             patch('tasks.tasks._org_health_exporter_fresh', return_value=False), \
             patch('tasks.tasks._org_health_exact_routing_lanes', return_value=[{
                 'organization_id': 1,
                 'organization_slug': 'shizuha',
                 'team_key': 'engineering',
                 'source': 'team_roster_compat',
                 'source_version': 'fixture',
                 'members': ['kai@shizuha.com'],
                 'candidates': [{'email': 'kai@shizuha.com'}],
                 'queued': [Queued()],
             }]), \
             patch.object(WorkflowService, '_pod_roster', return_value={'engineering': ['kai@shizuha.com'], 'program-management': []}), \
             patch.object(WorkflowService, '_queued_tasks_for_team', return_value=FakeQS([Queued()])), \
             patch.object(WorkflowService, '_flat_roster', return_value=['kai@shizuha.com']), \
             patch.object(WorkflowService, '_disabled_agent_emails', return_value=set()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=False), \
             patch.object(WorkflowService, '_resolve_team_assignee', return_value=(None, None)), \
             patch.dict('os.environ', {'PULSE_QUEUE_AGE_SLA_HOURS': '96'}):
            result = sweep_org_health.run()

        self.assertEqual(result['findings'], 1)
        self.assertEqual(result['filed'], 0)
        self.assertEqual(created, [])

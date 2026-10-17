"""PLS-483 (option-b) in-boundary runner regression.

Covers the behaviours review required of the in-process replacement for the
standalone exporter: Redis-authoritative single-source metric state, singleton /
non-re-entrant lock, FAIL-CLOSED without Redis, the hard bounded timeout with the
typed `timeout` reason (lock held while a timed-out op may be live), and the
bounded /metrics render contract (omit-when-absent so the Missing watchdog fires).
"""
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from tasks.services import attachment_canary_runner as runner


class _FakeRedis:
    """Minimal shared Redis stand-in supporting the runner's set(nx,ex)/get/delete.

    A single instance is shared across simulated replicas so cross-pod
    authoritative state and lock-hold semantics can be exercised for real.
    """
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):  # noqa: ARG002 — ex unused in test
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


class _StateFileMixin:
    def setUp(self):
        super().setUp()
        self._dir = tempfile.mkdtemp()
        path = os.path.join(self._dir, "canary-metrics.json")
        # Redis is now the MANDATORY authoritative store (the tick fails closed
        # without it and the metric renders from Redis only). Default every test
        # to a per-test shared FakeRedis; tests that need a different Redis
        # (or its absence) patch _get_redis inside their own `with` block.
        self._redis = _FakeRedis()
        self._patches = [
            patch.object(runner, "_STATE_PATH", path),
            patch.object(runner, "_STATE_LOCK_PATH", path + ".lock"),
            patch.object(runner, "_get_redis", return_value=self._redis),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()


class MetricRenderTests(_StateFileMixin, SimpleTestCase):
    def test_render_roundtrip_success(self):
        runner._write_state({
            "last_run": 1000.0, "last_success": 1000.0, "success": 1,
            "phase_success": {p: 1 for p in runner.PHASES},
            "failure_phase": "none", "failure_reason": "none", "stale_removed": 3,
        })
        out = runner.render_attachment_canary_metrics()
        self.assertIn("pulse_attachment_storage_canary_success 1", out)
        self.assertIn('pulse_attachment_storage_canary_phase_success{phase="create"} 1', out)
        self.assertIn("pulse_attachment_storage_canary_stale_objects_removed 3", out)
        # no object keys, tokens, or credentials ever reach a label
        self.assertNotIn("YOUR_", out)
        self.assertNotIn(".bin", out)

    def test_render_omits_family_when_no_authoritative_state(self):
        # No run yet / no Redis state -> OMIT the family so the Missing watchdog
        # fires (never a synthetic success=0 that reads like a real result).
        out = runner.render_attachment_canary_metrics()
        self.assertEqual(out, "")

    def test_unbounded_failure_reason_collapses(self):
        runner._write_state({
            "last_run": 1.0, "last_success": 0.0, "success": 0,
            "phase_success": {p: 0 for p in runner.PHASES},
            "failure_phase": "create", "failure_reason": "s3-raw-boto-error-xyz",
            "stale_removed": 0,
        })
        out = runner.render_attachment_canary_metrics()
        self.assertIn('reason="unexpected"', out)
        self.assertNotIn("s3-raw-boto-error-xyz", out)

    def test_topology_mismatch_reasons_remain_typed(self):
        for reason in ("endpoint_mismatch", "bucket_mismatch", "region_mismatch"):
            runner._write_state({
                "last_run": 1.0, "last_success": 0.0, "success": 0,
                "phase_success": {p: 0 for p in runner.PHASES},
                "failure_phase": "topology", "failure_reason": reason,
                "stale_removed": 0,
            })
            out = runner.render_attachment_canary_metrics()
            self.assertIn(f'reason="{reason}"', out)


class TickTests(_StateFileMixin, SimpleTestCase):
    @override_settings(ATTACHMENT_CANARY_FAULT_MODE='signature')
    def test_git_owned_signature_fault_mode_reaches_probe(self):
        with patch.object(runner, "run_attachment_storage_canary",
                          return_value={"ok": True,
                                        "phases": {p: True for p in runner.PHASES},
                                        "stale_objects_removed": 0}) as run:
            result = runner.attachment_storage_canary_tick()
        self.assertTrue(result["ok"])
        run.assert_called_once_with(fault_mode="signature")

    @override_settings(ATTACHMENT_CANARY_ENABLED=False)
    def test_disabled_skips(self):
        self.assertEqual(runner.attachment_storage_canary_tick(), {"skipped": "disabled"})

    def test_redis_unavailable_fails_closed(self):
        # review P1 #1: without the cross-replica Redis singleton the tick must
        # NOT run (which would let every HPA pod run unlocked) — fail closed.
        with patch.object(runner, "_get_redis", return_value=None):
            self.assertEqual(runner.attachment_storage_canary_tick(),
                             {"skipped": "redis_unavailable"})

    def test_singleton_lock_skips_when_held(self):
        # SET NX returns False => another replica/tick already holds the lock.
        redis = MagicMock()
        redis.set.return_value = False
        with patch.object(runner, "_get_redis", return_value=redis):
            self.assertEqual(runner.attachment_storage_canary_tick(), {"skipped": "locked"})
        redis.delete.assert_not_called()  # never release a lock we did not take

    def test_success_records_authoritative_state(self):
        with patch.object(runner, "run_attachment_storage_canary",
                          return_value={"ok": True, "phases": {p: True for p in runner.PHASES},
                                        "stale_objects_removed": 0}):
            result = runner.attachment_storage_canary_tick()
        self.assertTrue(result["ok"])
        self.assertEqual(runner._redis_read_state()["success"], 1)

    @override_settings(ATTACHMENT_CANARY_RUN_TIMEOUT_SECONDS=0.2)
    def test_timeout_yields_typed_reason(self):
        def _slow(*args, **kwargs):
            time.sleep(1.0)
            return {"ok": True, "phases": {}}

        with patch.object(runner, "run_attachment_storage_canary", side_effect=_slow):
            runner.attachment_storage_canary_tick()
        state = runner._redis_read_state()
        self.assertEqual(state["failure_reason"], "timeout")
        self.assertEqual(state["success"], 0)


class SharedStateAcrossReplicasTests(_StateFileMixin, SimpleTestCase):
    """review P1 #2: the metric is AUTHORITATIVE and shared across the 2-5 HPA
    replicas — a non-writer pod renders the same values as the writer, so no
    per-pod snapshot can diverge and no cross-instance aggregation is needed."""

    def test_non_writer_replica_renders_shared_authoritative_state(self):
        shared = _FakeRedis()
        with patch.object(runner, "_get_redis", return_value=shared), \
                patch.object(runner, "run_attachment_storage_canary",
                             return_value={"ok": True,
                                           "phases": {p: True for p in runner.PHASES},
                                           "stale_objects_removed": 0}):
            self.assertTrue(runner.attachment_storage_canary_tick()["ok"])
            # A different replica (no local file) renders the SAME shared state.
            other = tempfile.mkdtemp()
            with patch.object(runner, "_STATE_PATH", os.path.join(other, "c.json")), \
                    patch.object(runner, "_STATE_LOCK_PATH", os.path.join(other, "c.json.lock")):
                out = runner.render_attachment_canary_metrics()
        self.assertIn("pulse_attachment_storage_canary_success 1", out)
        self.assertNotIn(
            "pulse_attachment_storage_canary_last_run_timestamp_seconds 0.000", out,
            "a non-writer replica must not emit last_run=0 for healthy production")

    def test_render_omits_when_redis_unreadable(self):
        # Redis outage -> no authoritative state -> OMIT (Missing watchdog fires),
        # NOT a stale local snapshot that could mask a real failure.
        with patch.object(runner, "_get_redis", return_value=None):
            self.assertEqual(runner.render_attachment_canary_metrics(), "")


class TimeoutLeaseTests(_StateFileMixin, SimpleTestCase):
    """review P1 #1: on timeout the run-lock is HELD (not released in finally),
    so a second production-order tick cannot start a concurrent storage
    transaction while the timed-out one may still be live; a clean run releases.
    (The canary's bounded HTTP client makes the live op abort within seconds.)"""

    @override_settings(ATTACHMENT_CANARY_RUN_TIMEOUT_SECONDS=0.2)
    def test_timeout_holds_lock_no_overlapping_probe(self):
        shared = _FakeRedis()
        starts = []

        def _slow(*args, **kwargs):
            starts.append(time.time())
            time.sleep(1.0)
            return {"ok": True, "phases": {}}

        with patch.object(runner, "_get_redis", return_value=shared), \
                patch.object(runner, "run_attachment_storage_canary", side_effect=_slow):
            runner.attachment_storage_canary_tick()  # times out at 0.2s
            self.assertIn(runner._LOCK_KEY, shared.store,
                          "a timed-out run must keep the singleton lock")
            second = runner.attachment_storage_canary_tick()
        self.assertEqual(second, {"skipped": "locked"},
                         "no second tick may run while the timed-out op is live")
        self.assertEqual(len(starts), 1, "exactly one live storage transaction")

    def test_clean_run_releases_lock(self):
        shared = _FakeRedis()
        with patch.object(runner, "_get_redis", return_value=shared), \
                patch.object(runner, "run_attachment_storage_canary",
                             return_value={"ok": True, "phases": {}, "stale_objects_removed": 0}):
            runner.attachment_storage_canary_tick()
        self.assertNotIn(runner._LOCK_KEY, shared.store,
                         "a clean run must release the lock for the next tick")


class DisabledOmitsMetricTests(_StateFileMixin, SimpleTestCase):
    """A disabled/unregistered producer must OMIT the family so the Prometheus
    absent() Missing alert fires — never a stale healthy gauge."""

    @override_settings(ATTACHMENT_CANARY_ENABLED=False)
    def test_disabled_producer_omits_family(self):
        shared = _FakeRedis()
        with patch.object(runner, "_get_redis", return_value=shared):
            runner._write_state({
                "last_run": 1000.0, "last_success": 1000.0, "success": 1,
                "phase_success": {p: 1 for p in runner.PHASES},
                "failure_phase": "none", "failure_reason": "none", "stale_removed": 0,
            })
            out = runner.render_attachment_canary_metrics()
        self.assertEqual(out, "", "a disabled producer must omit the whole family")
        self.assertNotIn(
            "pulse_attachment_storage_canary_last_run_timestamp_seconds", out)

    @override_settings(ATTACHMENT_CANARY_ENABLED=True)
    def test_enabled_producer_emits_family(self):
        runner._write_state({
            "last_run": 5.0, "last_success": 5.0, "success": 1,
            "phase_success": {p: 1 for p in runner.PHASES},
            "failure_phase": "none", "failure_reason": "none", "stale_removed": 0,
        })
        out = runner.render_attachment_canary_metrics()
        self.assertIn("pulse_attachment_storage_canary_last_run_timestamp_seconds", out)

"""PLS-778: an all-null /statusz must say WHY it is all-null.

Regression for the defect recorded on PLS-778 (a PLS-766/PLS-647 follow-on).
``read_heartbeats`` swallowed every liveness-store failure at ``logger.debug``
and returned a dict byte-identical to a genuinely never-fired set, which
``StatuszView`` served verbatim. So an all-null reading meant EITHER "no
mechanism has fired" OR "the web process cannot reach the liveness store", with
no way for a reader — human or the mechanism-liveness http-json-age probe — to
tell which, and no operator-visible log line either. That inverted the module's
own purpose: it exists to expose a silently-dead MECHANISM on a healthy pod, and
in this failure mode it reported a silently-dead STORE as dead mechanisms.

These tests mirror the recorded reproduction posted on PLS-778 (run live in pod
shizuha-pulse-6b489d8d6f-7ssss against image shizuha-tasks-89031ca), which
compared a reachable store against a blackhole client and observed identical
output. Case (B) below is that comparison, made assertable.
"""
import logging
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from tasks import liveness


class DictRedis:
    """Reachable store: get/set round-trip, like the real client on success."""

    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)


class UnreachableRedis:
    """Store that fails the way a connect timeout / auth failure does."""

    def get(self, *a, **k):
        raise ConnectionError('Error 111 connecting to redis:6379. Connection refused.')

    def set(self, *a, **k):
        raise ConnectionError('Error 111 connecting to redis:6379. Connection refused.')


class LivenessStoreAmbiguityTests(TestCase):
    """The two all-null cases must be distinguishable at the helper layer."""

    def test_never_fired_reads_all_null_with_no_error(self):
        # (A) Store reachable, nothing has fired: all-null is the TRUTH, and
        # the absence of an error is what makes it readable as such.
        values, error = liveness.read_heartbeats_status(client=DictRedis())
        self.assertTrue(all(v is None for v in values.values()))
        self.assertIsNone(error)

    def test_unreachable_store_reads_all_null_WITH_an_error(self):
        # (B) The reproduction: same keys, same nulls — but now carrying the
        # reason. Before PLS-778 this was indistinguishable from (A).
        values, error = liveness.read_heartbeats_status(client=UnreachableRedis())
        self.assertTrue(all(v is None for v in values.values()))
        self.assertIsNotNone(error)
        self.assertIn('ConnectionError', error)

    def test_the_two_cases_differ_only_in_the_error(self):
        # The defect stated precisely: the timestamp dicts ARE identical, so
        # nothing but the error channel can carry the distinction. If a future
        # change drops the error, this is what silently regresses.
        never_fired, no_error = liveness.read_heartbeats_status(client=DictRedis())
        unreachable, error = liveness.read_heartbeats_status(client=UnreachableRedis())
        self.assertEqual(never_fired, unreachable)
        self.assertNotEqual(no_error, error)

    def test_partial_store_is_not_reported_as_a_store_failure(self):
        # A store that answers, with only some mechanisms fired, is healthy.
        client = DictRedis()
        liveness.record_heartbeat(liveness.STALE_SWEEP, client=client, now=1785708666.242)
        values, error = liveness.read_heartbeats_status(client=client)
        self.assertEqual(values[liveness.STALE_SWEEP], 1785708666.242)
        self.assertIsNone(values[liveness.RECURRING_ANDON])
        self.assertIsNone(error)

    def test_read_failure_logs_at_warning_not_debug(self):
        # The failure must be visible at default log level. `logger.debug` is
        # what made the live reproduction produce no operator-visible trace.
        with self.assertLogs('tasks.liveness', level=logging.WARNING) as captured:
            liveness.read_heartbeats_status(client=UnreachableRedis())
        self.assertTrue(
            any('liveness heartbeat read failed' in line for line in captured.output),
            captured.output,
        )

    def test_write_failure_logs_at_warning_not_debug(self):
        with self.assertLogs('tasks.liveness', level=logging.WARNING) as captured:
            ts = liveness.record_heartbeat(
                liveness.STALE_SWEEP, client=UnreachableRedis(), now=5.0,
            )
        self.assertEqual(ts, 5.0)  # still never raises — the mechanism is unharmed
        self.assertTrue(
            any('liveness heartbeat write failed' in line for line in captured.output),
            captured.output,
        )

    def test_legacy_read_heartbeats_contract_is_unchanged(self):
        # Callers that only want timestamps keep working, and still never raise.
        got = liveness.read_heartbeats(client=UnreachableRedis())
        self.assertEqual(set(got), set(liveness.MECHANISMS))
        self.assertTrue(all(v is None for v in got.values()))


class StatuszStoreHealthTests(TestCase):
    """The distinction must survive out to the endpoint the probe reads."""

    def _get(self):
        resp = self.client.get(reverse('mechanism-liveness-statusz'))
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_statusz_marks_store_unreachable(self):
        with mock.patch('tasks.liveness._redis_client', return_value=UnreachableRedis()):
            body = self._get()
        # Same nulls the Jul-30 audit saw...
        self.assertIsNone(body['stale_sweep_last_run_ts'])
        self.assertIsNone(body['recurring_andon_last_run_ts'])
        # ...now attributable at the moment the reading is taken.
        self.assertFalse(body['liveness_store_ok'])
        self.assertIn('ConnectionError', body['liveness_store_error'])

    def test_statusz_marks_store_ok_when_nothing_has_fired(self):
        with mock.patch('tasks.liveness._redis_client', return_value=DictRedis()):
            body = self._get()
        self.assertIsNone(body['stale_sweep_last_run_ts'])
        self.assertTrue(body['liveness_store_ok'])
        self.assertIsNone(body['liveness_store_error'])

    def test_statusz_still_serves_timestamps_when_store_is_healthy(self):
        client = DictRedis()
        liveness.record_heartbeat(liveness.STALE_SWEEP, client=client, now=1785708666.242)
        with mock.patch('tasks.liveness._redis_client', return_value=client):
            body = self._get()
        self.assertEqual(body['stale_sweep_last_run_ts'], 1785708666.242)
        self.assertTrue(body['liveness_store_ok'])
        self.assertIsNone(body['liveness_store_error'])

"""PLAT-1295: real mechanism-liveness heartbeats for shizuha-pulse.

Proves: (1) the liveness helper round-trips a per-mechanism timestamp through
Redis; (2) the recurring-engine scheduler loop emits its heartbeat and throttles
it; (3) the write-time invariant validator emits its heartbeat on every
validate_transition call; (4) /statusz exposes mechanism timestamps for an
http-json-age liveness signal. Together these replace the k8s-availability
proxy that could not detect a silently-dead mechanism on a healthy pod (PLS-210).
"""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from tasks import liveness
from tasks.scheduler import SchedulerLoop


class DictRedis:
    """Minimal get/set fake for the liveness store."""

    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)


class LivenessHelperTests(TestCase):
    def test_record_then_read_round_trips(self):
        client = DictRedis()
        ts = liveness.record_heartbeat(liveness.RECURRING_ANDON, client=client, now=1000.5)
        self.assertEqual(ts, 1000.5)
        got = liveness.read_heartbeats(client=client)
        self.assertEqual(got[liveness.RECURRING_ANDON], 1000.5)
        # The mechanism that never fired reads as None, not stale/zero.
        self.assertIsNone(got[liveness.INVARIANT_VALIDATOR])

    def test_record_never_raises_on_store_failure(self):
        class Boom:
            def set(self, *a, **k):
                raise RuntimeError('redis down')
        # Must not propagate — a liveness hiccup can't break the mechanism.
        ts = liveness.record_heartbeat(liveness.RECURRING_ANDON, client=Boom(), now=5.0)
        self.assertEqual(ts, 5.0)

    def test_read_never_raises_on_store_failure(self):
        class Boom:
            def get(self, *a, **k):
                raise RuntimeError('redis down')
        got = liveness.read_heartbeats(client=Boom())
        self.assertEqual(got, {name: None for name in liveness.MECHANISMS})


class SchedulerHeartbeatTests(TestCase):
    def _bare_scheduler(self):
        # Bypass __init__ (which opens a real Redis) — inject the fields the
        # heartbeat path touches.
        sched = SchedulerLoop.__new__(SchedulerLoop)
        sched._redis = DictRedis()
        sched._last_liveness_ts = 0.0
        return sched

    def test_poll_records_recurring_heartbeat(self):
        sched = self._bare_scheduler()
        with mock.patch('tasks.liveness.record_heartbeat',
                        wraps=liveness.record_heartbeat) as rec:
            sched._record_liveness()
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], liveness.RECURRING_ANDON)
        # The heartbeat actually landed in the injected store.
        self.assertIn(liveness._key(liveness.RECURRING_ANDON), sched._redis.store)

    def test_recurring_heartbeat_is_throttled(self):
        sched = self._bare_scheduler()
        with mock.patch('tasks.liveness.record_heartbeat') as rec:
            sched._record_liveness()       # first — fires
            sched._record_liveness()       # immediately after — throttled
            self.assertEqual(rec.call_count, 1)
            # Simulate the throttle window elapsing.
            sched._last_liveness_ts -= (SchedulerLoop.LIVENESS_THROTTLE + 1)
            sched._record_liveness()       # fires again
            self.assertEqual(rec.call_count, 2)


class ValidatorHeartbeatTests(TestCase):
    def test_validate_transition_records_validator_heartbeat(self):
        from tasks.services.workflow_service import WorkflowService

        item = mock.Mock()
        item.status = 'todo'
        item.workflow_status = ''
        with mock.patch('tasks.liveness.record_heartbeat') as rec:
            # current == new → returns early (True) after the heartbeat.
            result = WorkflowService.validate_transition(item, 'todo')
        self.assertTrue(result)
        rec.assert_called_once_with(liveness.INVARIANT_VALIDATOR)


class StatuszViewTests(TestCase):
    def test_statusz_exposes_mechanism_timestamps(self):
        with mock.patch('tasks.liveness.read_heartbeats_status', return_value=({
            liveness.RECURRING_ANDON: 1234.0,
            liveness.INVARIANT_VALIDATOR: 5678.0,
            liveness.DEPLOY_VERIFY_GATE: 9012.0,
            liveness.DEPLOY_VERIFY_MERGE_EVENT: 1111.0,
            liveness.STALE_SWEEP: 3456.0,
            liveness.OPERATOR_WAIT: 7890.0,
            liveness.WIP_ADMISSION: 7891.0,
            liveness.ROSTER_RECONCILIATION: 2468.0,
            liveness.ZERO_MEMBER_TEAM_DETECTOR: 1357.0,
        }, None)), mock.patch(
            'tasks.liveness.read_merge_event_retry_failure', return_value=None,
        ):
            resp = self.client.get(reverse('mechanism-liveness-statusz'))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['recurring_andon_last_run_ts'], 1234.0)
        self.assertEqual(body['invariant_validator_last_fired_ts'], 5678.0)
        self.assertEqual(body['deploy_verify_gate_last_fired_ts'], 9012.0)
        self.assertEqual(
            body['deploy_verify_merge_event_last_success_ts'], 1111.0,
        )
        self.assertIsNone(body['deploy_verify_merge_event_last_failure'])
        self.assertEqual(body['stale_sweep_last_run_ts'], 3456.0)
        self.assertEqual(body['operator_wait_last_run_ts'], 7890.0)
        self.assertEqual(body['wip_admission_last_fired_ts'], 7891.0)
        self.assertEqual(body['roster_reconciliation_last_success_ts'], 2468.0)
        self.assertEqual(
            body['zero_member_team_detector_last_success_ts'], 1357.0,
        )
        self.assertIn('now', body)
        # PLS-778: a healthy read must say so explicitly.
        self.assertTrue(body['liveness_store_ok'])
        self.assertIsNone(body['liveness_store_error'])

    def test_statusz_reports_null_before_first_fire(self):
        with mock.patch('tasks.liveness.read_heartbeats_status', return_value=({
            name: None for name in liveness.MECHANISMS
        }, None)), mock.patch(
            'tasks.liveness.read_merge_event_retry_failure', return_value=None,
        ):
            resp = self.client.get(reverse('mechanism-liveness-statusz'))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body['recurring_andon_last_run_ts'])
        self.assertIsNone(body['invariant_validator_last_fired_ts'])
        self.assertIsNone(body['deploy_verify_gate_last_fired_ts'])
        self.assertIsNone(body['deploy_verify_merge_event_last_success_ts'])
        self.assertIsNone(body['deploy_verify_merge_event_last_failure'])
        self.assertIsNone(body['stale_sweep_last_run_ts'])
        self.assertIsNone(body['operator_wait_last_run_ts'])
        self.assertIsNone(body['wip_admission_last_fired_ts'])
        self.assertIsNone(body['roster_reconciliation_last_success_ts'])
        self.assertIsNone(
            body['zero_member_team_detector_last_success_ts']
        )
        # PLS-778: all-null WITH a healthy store means "nothing has fired yet",
        # and the body must say so rather than leaving the reader to guess.
        self.assertTrue(body['liveness_store_ok'])
        self.assertIsNone(body['liveness_store_error'])

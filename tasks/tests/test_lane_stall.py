"""PLS-1037 (S3): lane-stall detection + classification + alert tests.

The failure-mode classification lives in S1 (``lane_health.classify_stall``);
these tests cover the S3 debounce + alert layer and the shared classification
shape.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.services import lane_stall
from tasks.services.lane_health import (
    cache,
    classify_stall,
    hold_override_events,
    maybe_record_hold_override_from_merge,
    assigned_not_pulled,
    cache,
    classify_stall,
    previous_assignee_from_comment,
)
from tasks.services.lane_stall import STALL_DEBOUNCE_SECONDS, check_lane_stall


def _snapshot(team, *, routable=0, demand=3, seats=None, version='1'):
    seats = seats if seats is not None else [
        {'email': 'a@shizuha.com', 'live': False, 'reasons': ['seat_not_live']},
    ]
    return {
        'team': team,
        'roster': {'version': version},
        'resolver': {'routable_seats': routable},
        'queue_depth': {'total_active': demand},
        'seats': seats,
    }


class ClassifyStallTests(TestCase):
    def tearDown(self):
        cache.delete('pulse:lane_stall:healthy_roster:lane')

    def test_no_stall_when_routable_seat_exists(self):
        snap = _snapshot('lane', routable=1, demand=3)
        v = classify_stall(snap)
        self.assertFalse(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'none')

    def test_no_stall_when_demand_empty(self):
        snap = _snapshot('lane', routable=0, demand=0)
        v = classify_stall(snap)
        self.assertFalse(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'none')

    def test_seat_not_live_classification(self):
        snap = _snapshot(
            'lane', routable=0, demand=3,
            seats=[{'email': 'a@shizuha.com', 'live': False, 'reasons': ['seat_not_live']}],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'seat_not_live')

    def test_over_capacity_classification(self):
        snap = _snapshot(
            'lane', routable=0, demand=3,
            seats=[{'email': 'a@shizuha.com', 'live': False, 'reasons': ['over_capacity']}],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'over_capacity')

    def test_over_capacity_from_wip_accounting_live_seat(self):
        # A LIVE seat over its WIP cap → capacity family (WIP accounting).
        snap = _snapshot(
            'lane', routable=0, demand=3,
            seats=[{
                'email': 'a@shizuha.com', 'live': True, 'reasons': [],
                'capacity': {'over_capacity': True},
            }],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'over_capacity')

    def test_mixed_classification(self):
        snap = _snapshot(
            'lane', routable=0, demand=3,
            seats=[
                {'email': 'a@shizuha.com', 'live': False, 'reasons': ['seat_not_live']},
                {'email': 'b@shizuha.com', 'live': True, 'reasons': [],
                 'capacity': {'over_capacity': True}},
            ],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'mixed')

    def test_authority_drift_precedence(self):
        # Roster version advanced since last healthy probe → authority_drift
        # wins even when seats also look down.
        cache.set('pulse:lane_stall:healthy_roster:lane', '1', timeout=86400)
        snap = _snapshot(
            'lane', routable=0, demand=3, version='2',
            seats=[{'email': 'a@shizuha.com', 'live': False, 'reasons': ['seat_not_live']}],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'authority_drift')

    def test_assigned_not_pulled_is_not_a_failure_mode(self):
        old = (timezone.now() - timedelta(hours=5)).isoformat()
        snap = _snapshot(
            'lane', routable=1, demand=1,
            seats=[{'email': 'kai@shizuha.com', 'live': True, 'reasons': []}],
        )
        snap['queue_depth'] = {
            'total_active': 1,
            'oldest_item_key': 'PLAT-6969',
            'oldest_assigned_to': 'kai@shizuha.com',
            'oldest_state': 'todo',
            'oldest_assigned_at': old,
        }
        snap['last_assignment'] = {
            'kind': 'failover',
            'assignee': 'kai@shizuha.com',
            'item_key': 'PLAT-6969',
        }
        v = classify_stall(snap)
        self.assertFalse(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'none')
        sig = assigned_not_pulled(snap)
        self.assertTrue(sig['detected'])
        self.assertEqual(sig['item_key'], 'PLAT-6969')

    def test_assigned_not_pulled_within_threshold_is_false(self):
        recent = (timezone.now() - timedelta(minutes=10)).isoformat()
        snap = _snapshot(
            'lane', routable=1, demand=1,
            seats=[{'email': 'kai@shizuha.com', 'live': True, 'reasons': []}],
        )
        snap['queue_depth'] = {
            'total_active': 1,
            'oldest_item_key': 'PLAT-6969',
            'oldest_assigned_to': 'kai@shizuha.com',
            'oldest_state': 'todo',
            'oldest_assigned_at': recent,
        }
        snap['last_assignment'] = {'kind': 'failover', 'assignee': 'kai@shizuha.com'}
        self.assertFalse(assigned_not_pulled(snap)['detected'])

    def test_previous_assignee_from_failover_comment(self):
        comment = (
            'Auto-failover to **reika@shizuha.com** — primary '
            '**rui@shizuha.com** is unavailable.'
        )
        self.assertEqual(
            previous_assignee_from_comment(comment),
            'rui@shizuha.com',
        )
    def test_rollout_window_classification(self):
        # PLS-1061: a fleet digest change within the window → rollout_window
        # (not seat_not_live), checked before the per-seat family scan.
        from tasks.services.lane_health import _FLEET_RUNTIME_DIGEST_CACHE_KEY
        cache.set(
            _FLEET_RUNTIME_DIGEST_CACHE_KEY,
            {
                'digest': '8878023b',
                'changed_at': (timezone.now() - timedelta(minutes=2)).isoformat(),
            },
            timeout=86400,
        )
        self.addCleanup(cache.delete, _FLEET_RUNTIME_DIGEST_CACHE_KEY)
        snap = _snapshot(
            'lane', routable=0, demand=3,
            seats=[{'email': 'a@shizuha.com', 'live': False, 'reasons': ['seat_not_live']}],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'rollout_window')
        self.assertIn('8878023b', v['evidence'])

    def test_rollout_window_does_not_mask_authority_drift(self):
        # authority-drift is checked FIRST: a digest change must not mask a
        # roster authority move.
        from tasks.services.lane_health import _FLEET_RUNTIME_DIGEST_CACHE_KEY
        cache.set('pulse:lane_stall:healthy_roster:lane', '1', timeout=86400)
        cache.set(
            _FLEET_RUNTIME_DIGEST_CACHE_KEY,
            {
                'digest': '8878023b',
                'changed_at': (timezone.now() - timedelta(minutes=2)).isoformat(),
            },
            timeout=86400,
        )
        self.addCleanup(cache.delete, _FLEET_RUNTIME_DIGEST_CACHE_KEY)
        snap = _snapshot(
            'lane', routable=0, demand=3, version='2',
            seats=[{'email': 'a@shizuha.com', 'live': False, 'reasons': ['seat_not_live']}],
        )
        v = classify_stall(snap)
        self.assertTrue(v['is_stalled'])
        self.assertEqual(v['failure_mode'], 'authority_drift')


class CheckLaneStallTests(TestCase):
    def tearDown(self):
        lane_stall._clear_stall_since('lane')
        cache.delete('pulse:lane_stall:healthy_roster:lane')

    @mock.patch('tasks.services.lane_stall.lane_health')
    @mock.patch('tasks.services.lane_stall._submit_stall_alert')
    def test_stall_fires_after_debounce(self, mock_submit, mock_lh):
        mock_lh.return_value = _snapshot('lane', routable=0, demand=3)
        # First pass starts debounce.
        r1 = check_lane_stall('lane')
        self.assertEqual(r1['action'], 'debouncing')
        mock_submit.assert_not_called()
        # Advance the debounce clock past the window.
        cache.set(
            'pulse:lane_stall:since:lane',
            (timezone.now() - timedelta(seconds=STALL_DEBOUNCE_SECONDS + 10)).isoformat(),
            timeout=86400,
        )
        r2 = check_lane_stall('lane')
        self.assertEqual(r2['action'], 'fired')
        mock_submit.assert_called_once()
        args = mock_submit.call_args[0]
        self.assertEqual(args[0], 'lane')
        self.assertEqual(args[1]['failure_mode'], 'seat_not_live')

    @mock.patch('tasks.services.lane_stall.lane_health')
    @mock.patch('tasks.services.lane_stall._submit_stall_alert')
    def test_no_alert_when_demand_empty(self, mock_submit, mock_lh):
        mock_lh.return_value = _snapshot('lane', routable=0, demand=0)
        r = check_lane_stall('lane')
        self.assertEqual(r['action'], 'none')
        mock_submit.assert_not_called()

    @mock.patch('tasks.services.lane_stall.lane_health')
    @mock.patch('tasks.services.lane_stall._submit_stall_alert')
    def test_assigned_not_pulled_fires_with_routable_seats(self, mock_submit, mock_lh):
        old = (timezone.now() - timedelta(hours=5)).isoformat()
        snap = _snapshot(
            'lane', routable=1, demand=1,
            seats=[{'email': 'kai@shizuha.com', 'live': True, 'reasons': []}],
        )
        snap['queue_depth'] = {
            'total_active': 1,
            'oldest_item_key': 'PLAT-6969',
            'oldest_assigned_to': 'kai@shizuha.com',
            'oldest_state': 'todo',
            'oldest_assigned_at': old,
        }
        snap['last_assignment'] = {'kind': 'failover', 'assignee': 'kai@shizuha.com'}
        snap['stall'] = {
            'is_stalled': False,
            'failure_mode': 'none',
            'assigned_not_pulled': assigned_not_pulled(snap),
        }
        mock_lh.return_value = snap
        r = check_lane_stall('lane')
        self.assertEqual(r['action'], 'fired')
        self.assertEqual(r['failure_mode'], 'none')
        mock_submit.assert_called_once()
        self.assertEqual(mock_submit.call_args[0][1]['failure_mode'], 'assigned_not_pulled')

    @mock.patch('tasks.services.lane_stall.lane_health')
    @mock.patch('tasks.services.lane_stall._submit_stall_alert')
    def test_resolves_when_seat_becomes_routable(self, mock_submit, mock_lh):
        # Stalled first (debounce started), then a seat becomes routable.
        mock_lh.return_value = _snapshot('lane', routable=0, demand=3)
        check_lane_stall('lane')
        mock_lh.return_value = _snapshot('lane', routable=1, demand=3)
        r = check_lane_stall('lane')
        self.assertEqual(r['action'], 'none')
        self.assertFalse(r['is_stalled'])
        mock_submit.assert_not_called()


class HoldOverrideSignatureTests(TestCase):
    def test_hold_override_is_not_a_failure_mode(self):
        snap = _snapshot('merge', routable=1, demand=2)
        snap['hold_override_events'] = [{
            'lane': 'merge',
            'pr': 'shizuha-labs/id#55',
            'merger': 'haru@shizuha.com',
            'held_by': 'aoi@shizuha.com',
            'head_sha': '34e2cfab',
            'approved_sha': 'f7523e46',
            'at': '2026-08-28T00:20:58Z',
        }]
        v = classify_stall(snap)
        self.assertEqual(v['failure_mode'], 'none')
        sig = hold_override_events(snap)
        self.assertTrue(sig['flagged'])
        self.assertEqual(sig['events'][0]['merger'], 'haru@shizuha.com')

    def test_exact_head_merge_is_not_flagged(self):
        snap = _snapshot('merge', routable=1, demand=0)
        snap['hold_override_events'] = [{
            'pr': 'shizuha-labs/id#1',
            'head_sha': 'abc',
            'approved_sha': 'ABC',
            'merger': 'haru@shizuha.com',
        }]
        self.assertFalse(hold_override_events(snap)['flagged'])

    @mock.patch('tasks.services.lane_stall.lane_health')
    @mock.patch('tasks.services.lane_stall._submit_stall_alert')
    def test_hold_override_fires_with_routable_seats(self, mock_submit, mock_lh):
        snap = _snapshot('merge', routable=2, demand=1)
        snap['stall'] = {
            'hold_override': hold_override_events({
                **snap,
                'hold_override_events': [{
                    'pr': 'shizuha-labs/id#55',
                    'merger': 'haru@shizuha.com',
                    'held_by': 'aoi@shizuha.com',
                    'head_sha': '34e2cfab',
                    'approved_sha': 'f7523e46',
                }],
            }),
        }
        mock_lh.return_value = snap
        r = check_lane_stall('merge')
        self.assertEqual(r['action'], 'fired')
        self.assertEqual(r['failure_mode'], 'none')
        self.assertEqual(mock_submit.call_args[0][1]['failure_mode'], 'hold_override')

    def test_merge_path_records_hold_override_event(self):
        item = mock.Mock()
        item.assignment_group = 'merge'
        item.item_key = 'ORIG-305'
        item.metadata = {}
        reviews = [
            {'state': 'APPROVED', 'commit_id': 'f7523e46', 'user': {'login': 'aoi'}},
        ]
        event = maybe_record_hold_override_from_merge(
            item,
            head_sha='34e2cfab',
            repo='shizuha-labs/id',
            number=55,
            merger='haru',
            reviews=reviews,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event['approved_sha'], 'f7523e46')
        snap = _snapshot('merge', routable=1, demand=0)
        self.assertTrue(hold_override_events(snap)['flagged'])

    def test_merge_path_exact_head_does_not_record(self):
        item = mock.Mock()
        item.assignment_group = 'merge'
        item.metadata = {}
        event = maybe_record_hold_override_from_merge(
            item,
            head_sha='abc',
            reviews=[{'state': 'APPROVED', 'commit_id': 'ABC'}],
        )
        self.assertIsNone(event)
    @mock.patch('tasks.services.lane_stall.lane_health')
    @mock.patch('tasks.services.deduplication.AlertDeduplicationService.submit_alert')
    def test_rollout_window_alert_is_annotated_not_raw_stall(self, mock_submit, mock_lh):
        # PLS-1061: a rollout_window verdict fires an "info / rollout in
        # progress" alert, not a raw warning lane-stall.
        from tasks.services.lane_health import _FLEET_RUNTIME_DIGEST_CACHE_KEY
        cache.set(
            _FLEET_RUNTIME_DIGEST_CACHE_KEY,
            {
                'digest': '8878023b',
                'changed_at': (timezone.now() - timedelta(minutes=2)).isoformat(),
            },
            timeout=86400,
        )
        self.addCleanup(cache.delete, _FLEET_RUNTIME_DIGEST_CACHE_KEY)
        mock_lh.return_value = _snapshot('lane', routable=0, demand=3)
        cache.set(
            'pulse:lane_stall:since:lane',
            (timezone.now() - timedelta(seconds=STALL_DEBOUNCE_SECONDS + 10)).isoformat(),
            timeout=86400,
        )
        r = check_lane_stall('lane')
        self.assertEqual(r['action'], 'fired')
        self.assertEqual(r['failure_mode'], 'rollout_window')
        mock_submit.assert_called_once()
        data = mock_submit.call_args[0][0]
        self.assertIn('rollout in progress', data['title'].lower())
        self.assertEqual(data['labels']['rollout_in_progress'], 'true')
        self.assertEqual(data['severity'], 'info')
        self.assertTrue(data['payload']['rollout_in_progress'])

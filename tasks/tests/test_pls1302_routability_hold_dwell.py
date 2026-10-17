"""PLS-1302: routability-hold dwell — the clear→normalize feedback loop breaker.

Production-order regression: the PLS-524/204 unavailable-owner clear and the
PLS-188 status-lane normalizer formed a ~10-minute feedback cycle that
re-materialized group-owned blocked items onto individual merge seats without
a lease (kei 15→39→54, aoi 14→19→49 across four beats; KOT-87/PLAT-4427/
PLS-516 ownership flipped kei↔ryo↔haru indefinitely, thousands of lifecycle
comments per task).

Fix: the clear path stamps a ``routability_hold_guard`` cycle marker; the
normalizer leaves a blocked item group-queued on its lane while the dwell
window is active (or the cycle cap is hit) instead of resolving a fresh
concrete seat. A real workflow transition — not the normalizer — re-resolves
ownership when the wait actually ends.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


def _guard(**kw):
    """A well-formed guard marker; timestamps default to now."""
    now = datetime.now(timezone.utc)
    base = {
        'count': 1,
        'first_at': now.isoformat(),
        'last_at': now.isoformat(),
    }
    base.update(kw)
    return base


class RoutabilityHoldDwellTests(SimpleTestCase):
    """Unit contract for the dwell gate + guard marker."""

    def test_fresh_guard_is_active(self):
        self.assertTrue(WorkflowService._routability_hold_dwell_active(_guard()))

    def test_expired_dwell_is_inactive(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        g = _guard(last_at=old.isoformat(), first_at=old.isoformat())
        self.assertFalse(WorkflowService._routability_hold_dwell_active(g))

    def test_cycle_cap_holds_within_window(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        g = _guard(count=3, first_at=old.isoformat(), last_at=old.isoformat())
        # last clear is 30min old (inside dwell) — active regardless.
        self.assertTrue(WorkflowService._routability_hold_dwell_active(g))

    def test_cycle_cap_holds_even_past_last_clear_dwell(self):
        # last_at outside the dwell window, but 3 cycles happened inside the
        # first-clear window → the loop breaker still holds group-queued.
        now = datetime.now(timezone.utc)
        g = _guard(
            count=3,
            first_at=(now - timedelta(minutes=50)).isoformat(),
            last_at=(now - timedelta(minutes=50)).isoformat(),
        )
        self.assertTrue(WorkflowService._routability_hold_dwell_active(g))

    def test_cycle_cap_expires_with_window(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        g = _guard(count=5, first_at=old.isoformat(), last_at=old.isoformat())
        self.assertFalse(WorkflowService._routability_hold_dwell_active(g))

    def test_malformed_guards_are_inert(self):
        for bad in ({}, None, {'count': 0}, {'count': 'x'},
                    {'count': 1, 'last_at': 'not-a-date'}):
            self.assertFalse(
                WorkflowService._routability_hold_dwell_active(bad),
                f'guard {bad!r} must be inert',
            )

    def test_stamp_increments_count_and_sets_timestamps(self):
        class _Item:
            metadata = {}
        item = _Item()
        self.assertTrue(WorkflowService._stamp_routability_hold_guard(item))
        g = item.metadata['routability_hold_guard']
        self.assertEqual(g['count'], 1)
        self.assertEqual(g['first_at'], g['last_at'])
        self.assertTrue(WorkflowService._stamp_routability_hold_guard(item))
        g2 = item.metadata['routability_hold_guard']
        self.assertEqual(g2['count'], 2)
        self.assertEqual(g2['first_at'], g['first_at'])  # first clear preserved

    def test_stamp_never_raises_on_hostile_metadata(self):
        class _Item:
            metadata = None  # falsy metadata must not break the stamp
        item = _Item()
        self.assertTrue(WorkflowService._stamp_routability_hold_guard(item))
        self.assertEqual(item.metadata['routability_hold_guard']['count'], 1)

    def test_slow_cadence_loop_trips_sliding_cap(self):
        # PLS-1337 KOT-87 shape: one clear per sweep beat (~70 min apart).
        # The legacy first_at-anchored hourly cap can never trip on this
        # cadence (now - first_ts outgrows the dwell after hour one, count
        # keeps climbing), and the hourly dwell is expired between beats —
        # the sliding 24h window is what must hold the item group-queued.
        now = datetime.now(timezone.utc)
        clears = [
            (now - timedelta(minutes=216)).isoformat(),
            (now - timedelta(minutes=143)).isoformat(),
            (now - timedelta(minutes=70)).isoformat(),
        ]
        g = _guard(
            count=25,
            first_at=(now - timedelta(hours=28)).isoformat(),
            last_at=clears[-1],
            clears=clears,
        )
        self.assertTrue(WorkflowService._routability_hold_dwell_active(g))

    def test_sliding_cap_releases_after_window_drains(self):
        # Self-healing: once clears stop for the cap window, the hold
        # releases and the normalizer may resolve again.
        now = datetime.now(timezone.utc)
        g = _guard(
            count=40,
            first_at=(now - timedelta(days=5)).isoformat(),
            last_at=(now - timedelta(hours=30)).isoformat(),
            clears=[
                (now - timedelta(hours=30)).isoformat(),
                (now - timedelta(hours=31)).isoformat(),
                (now - timedelta(hours=32)).isoformat(),
            ],
        )
        self.assertFalse(WorkflowService._routability_hold_dwell_active(g))

    def test_stamp_maintains_ring_and_engages_breaker(self):
        class _Item:
            metadata = {}
        item = _Item()
        for expected in (1, 2):
            self.assertTrue(WorkflowService._stamp_routability_hold_guard(item))
            g = item.metadata['routability_hold_guard']
            self.assertEqual(len(g['clears']), expected)
            self.assertNotIn('breaker_engaged_at', g)
        self.assertTrue(WorkflowService._stamp_routability_hold_guard(item))
        g = item.metadata['routability_hold_guard']
        self.assertEqual(len(g['clears']), 3)
        self.assertIn('breaker_engaged_at', g)

    def test_stamp_hostile_ring_does_not_raise(self):
        class _Item:
            metadata = {
                'routability_hold_guard': {
                    'count': 'x', 'clears': [1, None, 'ok'],
                },
            }
        item = _Item()
        # Hostile count is rejected wholesale by the pre-existing stamp
        # contract (int('x') raises inside the guarded block -> False).
        self.assertFalse(WorkflowService._stamp_routability_hold_guard(item))
        # Hostile ring entries with a valid count are filtered, not fatal.
        item2 = _Item()
        item2.metadata = {
            'routability_hold_guard': {
                'count': 4, 'clears': [1, None, 'ok'],
            },
        }
        self.assertTrue(WorkflowService._stamp_routability_hold_guard(item2))
        g = item2.metadata['routability_hold_guard']
        self.assertEqual(g['count'], 5)
        self.assertEqual(g['clears'], ['ok', g['last_at']])


class NormalizerDwellGateTests(SimpleTestCase):
    """The PLS-188 normalizer must not re-materialize a concrete owner while
    the dwell is active — and must resolve normally once it expires."""

    def test_dwell_suppresses_concrete_resolution(self):
        # Real call shape from sweep_rebalance_pods: the effective lane status
        # (pre_block_state) is passed as `status` while item.status stays
        # 'blocked' — the gate keys on item.status, the lane on the arg.
        item = mock.Mock()
        item.status = 'blocked'
        item.assignment_group = 'merge'
        item.assignee_email = None
        item.metadata = {'routability_hold_guard': _guard()}
        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(7, 'kei@shizuha.com'),
        ) as resolver:
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, 'awaiting_merge')
        self.assertEqual(lane, 'merge')
        resolver.assert_not_called()
        self.assertIsNone(item.assignee_email)  # stays group-queued

    def test_expired_dwell_resolves_normally(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        item = mock.Mock()
        item.status = 'blocked'
        item.assignment_group = 'merge'
        item.assignee_email = None
        item.metadata = {
            'routability_hold_guard': _guard(
                first_at=old.isoformat(), last_at=old.isoformat()),
        }
        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(7, 'kei@shizuha.com'),
        ) as resolver, mock.patch.object(
            WorkflowService, '_apply_resolved_assignee',
        ):
            WorkflowService._enforce_status_lane_owner(item, 'awaiting_merge')
        resolver.assert_called_once()

    def test_slow_cadence_guard_suppresses_resolution(self):
        # PLS-1337: with the hourly dwell and the legacy first-anchored cap
        # both expired (clears ~70 min apart, first_at 28h old), the sliding
        # 24h cap is what suppresses concrete re-materialization.
        now = datetime.now(timezone.utc)
        clears = [
            (now - timedelta(minutes=216)).isoformat(),
            (now - timedelta(minutes=143)).isoformat(),
            (now - timedelta(minutes=70)).isoformat(),
        ]
        item = mock.Mock()
        item.status = 'blocked'
        item.assignment_group = 'merge'
        item.assignee_email = None
        item.metadata = {
            'routability_hold_guard': _guard(
                count=25,
                first_at=(now - timedelta(hours=28)).isoformat(),
                last_at=clears[-1],
                clears=clears,
            ),
        }
        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(7, 'kei@shizuha.com'),
        ) as resolver:
            WorkflowService._enforce_status_lane_owner(item, 'awaiting_merge')
        resolver.assert_not_called()

    def test_non_blocked_status_is_never_suppressed(self):
        # The dwell gate is scoped to blocked items: an awaiting_merge item
        # with a stale guard marker still gets its lane owner re-resolved.
        item = mock.Mock()
        item.status = 'awaiting_merge'
        item.assignment_group = 'review'
        item.assignee_email = None
        item.metadata = {'routability_hold_guard': _guard()}
        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(7, 'kei@shizuha.com'),
        ) as resolver, mock.patch.object(
            WorkflowService, '_apply_resolved_assignee',
        ):
            WorkflowService._enforce_status_lane_owner(item, 'awaiting_merge')
        resolver.assert_called_once()

    def test_no_guard_resolves_normally(self):
        item = mock.Mock()
        item.status = 'blocked'
        item.assignment_group = 'merge'
        item.assignee_email = None
        item.metadata = {}
        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(7, 'kei@shizuha.com'),
        ) as resolver, mock.patch.object(
            WorkflowService, '_apply_resolved_assignee',
        ):
            WorkflowService._enforce_status_lane_owner(item, 'awaiting_merge')
        resolver.assert_called_once()

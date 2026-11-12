"""PLS-1307: the admin-ops lane's pinned routing floor.

The admin-ops compat row was found projecting a stale retirement-timestamp
snapshot (a phantom disabled identity as the only member) while the real
Admin Ops team's live routing seat is goro — the admin-ops capacity queue
starved (5+ unrouted tasks, PLAT-8263 at 234h vs 96h SLA) while the sweep
never saw the live seat. Pinning the floor in
EXPECTED_TEAM_ROSTER_MEMBERS makes any future shrinkage fail loudly in CI
and the live-read command (the PLS-833 pattern) instead of silently
starving the queue again.
"""
from django.test import SimpleTestCase

from tasks.services.team_roster_drift import (
    EXPECTED_TEAM_ROSTER_MEMBERS,
    EXPECTED_TEAM_ROSTER_ORGANIZATION_IDS,
    compare_team_roster_members,
)

ADMIN_OPS_KEY = 'shizuha:admin-ops'


class AdminOpsRosterFloorTests(SimpleTestCase):
    def test_floor_is_pinned_to_the_live_routing_seat(self):
        self.assertEqual(
            EXPECTED_TEAM_ROSTER_MEMBERS[ADMIN_OPS_KEY],
            frozenset({'goro@shizuha.com'}),
        )
        self.assertEqual(EXPECTED_TEAM_ROSTER_ORGANIZATION_IDS[ADMIN_OPS_KEY], 1)

    def test_live_routing_seat_present_is_not_drifted(self):
        signal = compare_team_roster_members(
            ADMIN_OPS_KEY,
            {'goro@shizuha.com'},
            source_version='test',
        )
        self.assertFalse(signal.drifted, signal.as_dict())

    def test_dropping_goro_emits_explicit_drift_signal(self):
        signal = compare_team_roster_members(
            ADMIN_OPS_KEY,
            set(),  # the stale-snapshot shape: no live routing seat
            source_version='test',
        )
        self.assertTrue(signal.drifted)
        self.assertEqual(signal.missing, ('goro@shizuha.com',))

    def test_phantom_identity_alone_does_not_satisfy_the_floor(self):
        # The starvation mechanism itself: a disabled phantom identity in the
        # roster must never count as routing capacity.
        signal = compare_team_roster_members(
            ADMIN_OPS_KEY,
            {'shion@shizuha.com'},  # phantom: disabled, no live seat
            source_version='test',
        )
        self.assertTrue(signal.drifted)
        self.assertEqual(signal.missing, ('goro@shizuha.com',))
        self.assertEqual(signal.unexpected, ('shion@shizuha.com',))

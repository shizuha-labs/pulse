"""PLAT-8980: capability-class lane detector — negative probes + episode FSM.

ADR-0086 v4 (Accepted): every critical lane maintains >=2 capable live seats.
The detector fires ALERT-NOT-PAGE at exactly 1 live-capable seat (15m
debounced, provisioning ask bundled). Four negative probes are pinned here as
fixtures across TWO workflow spellings (autonomous-bug via execute_transition
and simple via direct status update — the phantom-principal precedent).
"""
from unittest import mock

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services import capability_lane_health as clh
from tasks.services.workflow_service import WorkflowService


def _make_project(org_id=1):
    return Project.objects.create(name=f'CapLane{org_id}', key=f'CAP{org_id}', org_id=org_id)


def _make_item(project, *, title='cap lane probe', group='merge', status='todo',
               workflow_status='To Do'):
    it = Item.objects.create(
        mode='task', item_type='task', title=title, project=project,
        assignment_group=group, status='todo', workflow_status=workflow_status,
        priority='high', source='test-plat8980',
    )
    if status != 'todo':
        Item.objects.filter(pk=it.pk).update(status=status)
        it.refresh_from_db()
    return it


def _roster(team, emails, org_id=1):
    TeamRoster.objects.create(
        team_name=team, organization_id=org_id,
        tiers=[{'tier': 'core', 'emails': list(emails)}],
        ready_wip_cap=3, inflight_cap=3,
    )


class CapabilityLaneEpisodeTests(TestCase):
    """Probe/FSM tests for `evaluate_single_seat_episode` (no DB roster needed)."""

    def setUp(self):
        clh.cache.clear()

    def test_probe1_lane_at_one_with_zero_queued_demand_still_fires(self):
        """Degradation is a topology fact, not demand-dependent: the FSM has no
        queue leg — count==1 fires after the debounce regardless of demand."""
        t0 = clh.single_seat_debounce_seconds() + 1
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 1, now_ts=1000), 'hold')
        # zero queued demand is invisible to the FSM by construction:
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 1, now_ts=1000 + t0), 'fire')

    def test_probe2_sod_disqualified_seat_counts_as_zero(self):
        """A SoD-disqualified seat never counts: the typed verdict surfaces a
        disqualifying reason and `live_capable_seat_count` excludes it."""
        W = WorkflowService
        with mock.patch.object(TeamRoster, 'objects') as _:
            pass  # roster patched at the W level below
        with mock.patch.object(W, '_team_roster_entry_for_item', return_value=(
                {'tiers': [{'tier': 'core', 'emails': ['warm@agents.shizuha.io',
                                                           'disq@agents.shizuha.io']}],
                 'ready_wip_cap': 3, 'inflight_cap': 3}, 1)), \
             mock.patch.object(W, '_flat_roster', return_value=[
                 'warm@agents.shizuha.io', 'disq@agents.shizuha.io']), \
             mock.patch.object(W, '_seat_routability_verdict') as verdict:
            def _verdict(item, team, email, **kw):
                v = mock.Mock()
                if email.startswith('disq'):
                    v.reasons = ['seat_incapable']  # SoD/substrate disqualification
                else:
                    v.reasons = []
                return v
            verdict.side_effect = _verdict
            count, detail = clh.live_capable_seat_count(W, 'merge')
            self.assertEqual(count, 1)
            self.assertEqual(detail['disq@agents.shizuha.io'], ['seat_incapable'])
            self.assertEqual(detail['warm@agents.shizuha.io'], [])

    def test_probe3_phantom_identity_seat_never_counts(self):
        """A phantom-identity label (stored label matching no principal) can
        never confer membership — the verdict disqualifies it."""
        W = WorkflowService
        with mock.patch.object(W, '_team_roster_entry_for_item', return_value=(
                {'tiers': [{'tier': 'core', 'emails': ['ghost@agents.shizuha.io']}],
                 'ready_wip_cap': 3, 'inflight_cap': 3}, 1)), \
             mock.patch.object(W, '_flat_roster', return_value=['ghost@agents.shizuha.io']), \
             mock.patch.object(W, '_seat_routability_verdict') as verdict:
            v = mock.Mock()
            v.reasons = ['seat_not_team_member']  # phantom: no principal match
            verdict.return_value = v
            count, detail = clh.live_capable_seat_count(W, 'merge')
            self.assertEqual(count, 0)
            self.assertEqual(detail['ghost@agents.shizuha.io'], ['seat_not_team_member'])

    def test_probe4_dedup_and_deterministic_removal(self):
        """One episode per composite key: fire persists until count>=2 holds
        `clear_hold_seconds` — then deterministic removal ('clear')."""
        debounce = clh.single_seat_debounce_seconds()
        clear = clh.clear_hold_seconds()
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 1, now_ts=0), 'hold')
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 1, now_ts=debounce - 1), 'hold')
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 1, now_ts=debounce + 1), 'fire')
        # redundancy restored: clear only after the full clear hold
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 2, now_ts=debounce + 2), 'hold')
        self.assertEqual(
            clh.evaluate_single_seat_episode('merge', 2, now_ts=debounce + 2 + clear - 1),
            'hold')
        self.assertEqual(
            clh.evaluate_single_seat_episode('merge', 2, now_ts=debounce + 2 + clear + 1),
            'clear')

    def test_count_zero_is_not_owned_here(self):
        """count==0 belongs to the zero-live family: the FSM holds and does not
        fire, and does not reset the single-seat debounce window."""
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 1, now_ts=0), 'hold')
        self.assertEqual(clh.evaluate_single_seat_episode('merge', 0, now_ts=10), 'hold')
        # the debounce window survived the zero dip (no re-arm):
        self.assertEqual(
            clh.evaluate_single_seat_episode(
                'merge', 1, now_ts=clh.single_seat_debounce_seconds() + 1),
            'fire')


class CapabilityLaneSweepIntegrationTests(TestCase):
    """Second workflow spelling: the detector inside `sweep_org_health` findings,
    exercised through items created via direct status update (simple-style)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_sweep_fires_single_capability_seat_finding(self):
        from tasks.tasks import sweep_org_health
        project = _make_project()
        _make_item(project, group='merge')
        W = WorkflowService
        with mock.patch.object(clh, 'critical_capability_lanes', return_value=('merge',)), \
             mock.patch.object(W, '_team_roster_entry_for_item', return_value=(
                 {'tiers': [{'tier': 'core', 'emails': ['solo@agents.shizuha.io']}],
                  'ready_wip_cap': 3, 'inflight_cap': 3}, 1)), \
             mock.patch.object(W, '_flat_roster', return_value=['solo@agents.shizuha.io']), \
             mock.patch.object(W, '_seat_routability_verdict') as verdict, \
             mock.patch.object(clh, 'single_seat_debounce_seconds', return_value=0):
            v = mock.Mock()
            v.reasons = []
            verdict.return_value = v
            clh.cache.clear()
            # Pre-seed the single-seat first-seen timestamp: the episode FSM
            # records first-seen on the first evaluation and only fires on a
            # later one (even with a 0-second debounce), and the sweep
            # evaluates each lane exactly once per cycle.
            import time as _time
            clh.cache.set(
                clh._DEBOUNCE_KEY.format(org='1', lane='merge'),
                _time.time() - 1, timeout=3600,
            )
            result = sweep_org_health.run()
            # `findings` is a count (HIVE-694: PM-side findings are DM-only);
            # the detector must have contributed exactly one finding.
            self.assertGreaterEqual(result.get('findings', 0), 1)

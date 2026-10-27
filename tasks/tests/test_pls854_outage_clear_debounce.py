"""PLS-854 regression: Team-Outage carrier clear/fire symmetry, clear evidence,
successor linking, headline/action accuracy, and the lifted budget-cap directive.

Reproduces the org-health defects measured from PLAT-5721 / PLAT-5723 / PLAT-5724:
  1. the clear path terminalized a carrier on ONE absent detector cycle while
     the fire path required 2+ present cycles (inverted hysteresis);
  2. the clear comment recorded no evidence (no leg/roster/queued snapshot);
  3. a re-fired carrier was unlinked from its predecessor and carried no
     cumulative incident age;
  4. the headline asserted "0 live members" while the roster contained live but
     capability-filtered seats;
  5. "Action required" lacked a live-but-capability-filtered branch (steered
     responders to agent recovery);
  6. the emitter quoted the rescinded 2026-06-22 "1 agent/team" budget cap.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from tasks.models import Comment, Item, ItemLink, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    _is_terminal_status, _org_health_outage_clear_evidence, sweep_org_health,
)


def _mk_project():
    return Project.objects.create(
        name='PLS-854 Platform', key='P854', org_id=1, access_level='open',
    )


def _mk_task(project, group, key='PLX-9001', assignee_email=''):
    return Item.objects.create(
        project=project, item_key=key, title=f'{key} injected task',
        mode='task', status='open', workflow_name='autonomous-dev',
        assignment_group=group, assignee_email=assignee_email,
        created_by_email='zen@shizuha.com', created_by_id=0,
        created_at=timezone.now(),
    )


ADMIN_OK = {
    'status': 'ok', 'team_id': 1, 'member_count': 2,
    'members': ['nami@shizuha.com', 'mika@shizuha.com'],
}


class TeamOutageCarrierContentTests(TestCase):
    """Defects 3/3b/4/5/6: fire-path headline + body accuracy."""

    def setUp(self):
        WorkflowService._roster_cache = None
        self.project = _mk_project()
        TeamRoster.objects.create(
            team_name='qa-pls854', tiers=[['nami@shizuha.com', 'mika@shizuha.com']],
            is_active=True,
        )

    def _fire_outage(self, availability_reasons):
        with patch('tasks.tasks._org_health_exporter_fresh', return_value=True), \
                patch('tasks.tasks._org_health_candidate_availability',
                      return_value=availability_reasons), \
                patch('tasks.tasks._org_health_recent_active_team_activity',
                      return_value=None), \
                patch('tasks.tasks._org_health_admin_team_snapshot',
                      return_value=ADMIN_OK), \
                patch.dict('os.environ', {
                    'PULSE_ORG_HEALTH_OUTAGE_DEBOUNCE': '1',
                    'PULSE_ORG_HEALTH_OUTAGE_SANITY_MIN': '99',
                }):
            return sweep_org_health.run()

    def _outage_carrier(self):
        return Item.objects.filter(
            metadata__org_health_signal='zero-member-outage:1:qa-pls854',
        ).first()

    def test_headline_names_live_but_capability_filtered_members(self):
        """Defect 3/4: '0 live members' only when every member is seat_not_live."""
        _mk_task(self.project, 'qa-pls854')
        reasons = {
            'nami@shizuha.com': ['seat_incapable'],   # LIVE, capability-filtered
            'mika@shizuha.com': ['seat_not_live'],    # offline
        }
        result = self._fire_outage(reasons)
        self.assertEqual(result['admin_filed'], 1)
        carrier = self._outage_carrier()
        self.assertIsNotNone(carrier)
        self.assertIn('1/2 offline, 1 live but capability-filtered', carrier.title)
        self.assertNotIn('0 live members', carrier.title)

    def test_headline_zero_live_only_when_every_member_not_live(self):
        _mk_task(self.project, 'qa-pls854')
        reasons = {
            'nami@shizuha.com': ['seat_not_live'],
            'mika@shizuha.com': ['seat_not_live'],
        }
        result = self._fire_outage(reasons)
        self.assertEqual(result['admin_filed'], 1)
        carrier = self._outage_carrier()
        self.assertIn('0 live members', carrier.title)

    def test_action_has_capability_routing_branch_and_lifted_directive(self):
        """Defects 5/6: capability-routing branch present; 2026-06-22 cap gone."""
        _mk_task(self.project, 'qa-pls854')
        reasons = {
            'nami@shizuha.com': ['seat_incapable'],
            'mika@shizuha.com': ['seat_not_live'],
        }
        self._fire_outage(reasons)
        carrier = self._outage_carrier()
        desc = carrier.description
        self.assertIn('capability-routing condition', desc)
        self.assertIn('PLS-721', desc)
        self.assertIn('PLAT-5032', desc)
        self.assertIn('do NOT restart', desc)
        self.assertIn('2026-06-26', desc)
        self.assertNotIn('budget cap: 1 agent/team, operator directive 2026-06-22', desc)


class TeamOutageClearDebounceTests(TestCase):
    """Defect 1: a carrier terminalizes only after 2+ consecutive absent cycles."""

    def setUp(self):
        WorkflowService._roster_cache = None
        self.project = _mk_project()
        TeamRoster.objects.create(
            team_name='qa-pls854', tiers=[['nami@shizuha.com', 'mika@shizuha.com']],
            is_active=True,
        )

    def _sweep_with(self, availability_reasons, clear_evidence):
        with patch('tasks.tasks._org_health_exporter_fresh', return_value=True), \
                patch('tasks.tasks._org_health_candidate_availability',
                      return_value=availability_reasons), \
                patch('tasks.tasks._org_health_recent_active_team_activity',
                      return_value=None), \
                patch('tasks.tasks._org_health_admin_team_snapshot',
                      return_value=ADMIN_OK), \
                patch('tasks.tasks._org_health_outage_clear_evidence',
                      return_value=clear_evidence), \
                patch.dict('os.environ', {
                    'PULSE_ORG_HEALTH_OUTAGE_DEBOUNCE': '1',
                    'PULSE_ORG_HEALTH_OUTAGE_SANITY_MIN': '99',
                }):
            return sweep_org_health.run()

    def test_single_absent_cycle_does_not_clear_but_two_does(self):
        _mk_task(self.project, 'qa-pls854')
        both_down = {
            'nami@shizuha.com': ['seat_not_live'],
            'mika@shizuha.com': ['seat_not_live'],
        }
        self._sweep_with(both_down, None)   # fire (debounce=1)
        carrier = Item.objects.filter(
            metadata__org_health_signal='zero-member-outage:1:qa-pls854',
        ).first()
        self.assertIsNotNone(carrier)

        recovered = {
            'nami@shizuha.com': ['available'],
            'mika@shizuha.com': ['seat_not_live'],
        }
        evidence = ('signal absent for 2+ consecutive detector cycles — '
                    'availability leg cleared; queued=1 at clear time '
                    '(PLS-854-test)')
        first = self._sweep_with(recovered, evidence)
        carrier.refresh_from_db()
        self.assertEqual(first['admin_resolved'], 0)
        self.assertFalse(_is_terminal_status(carrier.status))

        second = self._sweep_with(recovered, evidence)
        carrier.refresh_from_db()
        self.assertEqual(second['admin_resolved'], 1)
        self.assertTrue(_is_terminal_status(carrier.status))
        last = Comment.objects.filter(item=carrier).order_by('-created_at').first()
        self.assertIn('signal cleared', last.content)
        self.assertIn(evidence, last.content)


class TeamOutageClearEvidenceTests(TestCase):
    """Defect 2: the clear comment records which leg + roster + queued count."""

    def test_clear_evidence_records_legs_roster_and_queue(self):
        item = _mk_task(_mk_project(), 'qa-pls854')
        with patch('tasks.tasks._org_health_routable_queue', return_value=[item]), \
                patch('tasks.services.routing_authority.routing_candidate_snapshot_for_item',
                      return_value={
                          'candidates': [
                              {'email': 'nami@shizuha.com', 'user_id': 1},
                              {'email': 'mika@shizuha.com', 'user_id': 2},
                          ],
                      }), \
                patch('tasks.tasks._org_health_candidate_availability',
                      return_value={
                          'nami@shizuha.com': ['seat_incapable'],
                          'mika@shizuha.com': ['seat_not_live'],
                      }):
            evidence = _org_health_outage_clear_evidence(
                WorkflowService, 'zero-member-outage:1:qa-pls854', timezone.now(),
            )
        self.assertIsNotNone(evidence)
        self.assertIn('2+ consecutive detector cycles', evidence)
        self.assertIn('availability leg cleared', evidence)
        self.assertIn('queue leg still 1 task(s) queued', evidence)
        self.assertIn('1/2 member(s) readable-available now', evidence)
        self.assertIn('queued=1 at clear time', evidence)
        self.assertIn('roster=[mika@shizuha.com=seat_not_live, '
                      'nami@shizuha.com=seat_incapable]', evidence)

    def test_clear_evidence_falls_back_for_unparseable_signal(self):
        self.assertIsNone(_org_health_outage_clear_evidence(
            WorkflowService, 'capacity-starve:1:qa-pls854', timezone.now()))


class TeamOutageRecurrenceLinkingTests(TestCase):
    """Defect 2/3: a re-fire is linked to its predecessor and names it + age."""

    def setUp(self):
        WorkflowService._roster_cache = None
        self.project = _mk_project()

    def _sweep(self):
        with patch('tasks.tasks._org_health_exporter_fresh', return_value=False):
            return sweep_org_health.run()

    def test_refire_links_predecessor_and_names_cumulative_age(self):
        TeamRoster.objects.create(team_name='prod-pls854', tiers=[[]], is_active=True)
        _mk_task(self.project, 'prod-pls854')

        onset = self._sweep()
        first_carrier = Item.objects.filter(
            metadata__org_health_signal='zero-member-team:1:prod-pls854',
        ).first()
        self.assertEqual(onset['admin_filed'], 1)
        self.assertIsNotNone(first_carrier)

        # clear via two consecutive clean cycles (members appear).
        TeamRoster.objects.filter(team_name='prod-pls854').update(tiers=[['kai@shizuha.com']])
        WorkflowService._roster_cache = None
        with patch('tasks.tasks._org_health_outage_clear_evidence',
                   return_value='evidence (PLS-854-test)'):
            self._sweep()
            self._sweep()
        first_carrier.refresh_from_db()
        self.assertTrue(_is_terminal_status(first_carrier.status))

        # re-fire: empty roster again.
        TeamRoster.objects.filter(team_name='prod-pls854').update(tiers=[[]])
        WorkflowService._roster_cache = None
        recurrence = self._sweep()
        self.assertEqual(recurrence['admin_filed'], 1)

        carriers = list(Item.objects.filter(
            metadata__org_health_signal='zero-member-team:1:prod-pls854',
        ).order_by('created_at', 'id'))
        self.assertEqual(len(carriers), 2)
        successor = next(c for c in carriers if c.pk != first_carrier.pk)

        linked = ItemLink.objects.filter(
            link_type='relates_to',
            source_item=first_carrier, target_item=successor,
        ).first() or ItemLink.objects.filter(
            link_type='relates_to',
            source_item=successor, target_item=first_carrier,
        ).first()
        self.assertIsNotNone(linked)
        self.assertIn('Predecessor carrier', successor.description)
        self.assertIn(first_carrier.item_key, successor.description)
        self.assertIn('cumulative age', successor.description)
        self.assertIn('relates_to', successor.description)


class TeamUnlawfulResolveTests(TestCase):
    """PLS-1440 regression: an outage resolution is unlawful when the
    availability walk still detects the condition."""

    setUp = TeamOutageClearDebounceTests.setUp

    def _sweep_with(self, availability_reasons, clear_evidence):
        return TeamOutageClearDebounceTests._sweep_with(self, availability_reasons, clear_evidence)

    def test_resolves_refused_when_walk_still_detects(self):
        # Harness note: the availability-walk mock drives DETECTOR PRESENCE,
        # while the clear-time availability re-read is
        # `_org_health_outage_clear_evidence` (mocked separately). The live
        # PLAT-11610 shape — detector quiet for 2+ cycles while the CLEAR-TIME
        # walk still reports every seat seat_not_live and demand still queued —
        # is therefore simulated with an UP walk (signal absent → the clear-side
        # streak accumulates) plus a 'neither leg cleared' evidence re-read.
        # With a DOWN walk the signal stays present, the streak resets, and the
        # clear leg (and this refusal) is unreachable — the vacuous pass that
        # hid this from run 9001 until the lawful-recovery test failed.
        down = {
            'nami@shizuha.com': ['seat_not_live'],
            'mika@shizuha.com': ['seat_not_live'],
        }
        _mk_task(self.project, 'qa-pls854', key='PLX-9003')
        self._sweep_with(down, None)   # fire (fire sweep resets the streak)
        carrier = Item.objects.filter(
            metadata__org_health_signal='zero-member-outage:1:qa-pls854',
        ).first()
        self.assertIsNotNone(carrier)

        up = {
            'nami@shizuha.com': ['available'],
            'mika@shizuha.com': ['available'],
        }
        unlawful = ('signal absent for 2+ consecutive detector cycles - '
                    'neither leg cleared (unexpected clear while condition '
                    'still detected); 0/2 member(s) readable-available now; '
                    'queued=1 at clear time; roster=[all seat_not_live] '
                    '(PLS-1440-test)')
        for _ in range(4):
            result = self._sweep_with(up, unlawful)
            carrier.refresh_from_db()
            self.assertEqual(result['admin_resolved'], 0)
            self.assertFalse(_is_terminal_status(carrier.status))

    def test_later_lawful_recovery_still_clears(self):
        down = {
            'nami@shizuha.com': ['seat_not_live'],
            'mika@shizuha.com': ['seat_not_live'],
        }
        _mk_task(self.project, 'qa-pls854', key='PLX-9004')
        self._sweep_with(down, None)   # fire (fire sweep resets the streak)
        carrier = Item.objects.filter(
            metadata__org_health_signal='zero-member-outage:1:qa-pls854',
        ).first()
        self.assertIsNotNone(carrier)

        up = {
            'nami@shizuha.com': ['available'],
            'mika@shizuha.com': ['available'],
        }
        unlawful = ('signal absent for 2+ detector cycles - neither leg '
                    'cleared (condition still detected) (PLS-1440-test)')
        # Absent streak 1/2: below the clear debounce — kept regardless.
        first = self._sweep_with(up, unlawful)
        self.assertEqual(first['admin_resolved'], 0)
        # Absent streak 2/2: clear leg reached, but the clear-time walk
        # contradicts signal absence (neither leg cleared) → refused, latched.
        second = self._sweep_with(up, unlawful)
        self.assertEqual(second['admin_resolved'], 0)
        carrier.refresh_from_db()
        self.assertFalse(_is_terminal_status(carrier.status))

        lawful = ('signal absent for 2+ detector cycles - both legs cleared '
                  '(availability leg AND queue leg); 2/2 member(s) '
                  'readable-available now (PLS-1440-test)')
        # The refusal latch is not a terminal state: the first clear-time walk
        # that AGREES with signal absence (both legs cleared) resolves the
        # carrier — the streak is already past the debounce.
        third = self._sweep_with(up, lawful)
        self.assertEqual(third['admin_resolved'], 1)
        carrier.refresh_from_db()
        self.assertTrue(_is_terminal_status(carrier.status))

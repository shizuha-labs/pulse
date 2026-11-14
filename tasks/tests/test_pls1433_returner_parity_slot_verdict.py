"""PLS-1433 returner-parity: the shared capacity verdict must read the same
cap policy AND the same row set as the PLS-509 returner.

Live loop 2026-11-13 (hritik, cmt 1265029): after pulse#878's shed-view
parity, unassign thrash ROSE (4,989 -> 6,150/day). Two residual
asymmetries between `_slot_at_capacity` (the sweep/event-drain pull gate)
and the PLS-509 recovery returner drove pull->return pairs:

  1. Cap-value mismatch: the returner enforces the strictest positive
     ready/active cap across EVERY active wip_enforced roster the seat
     sits on (org-scoped, keyed (org_id, email)); the verdict read only
     the pulled item's team roster. A seat on engineering (ready 5) and
     review (ready 3) looks under-cap at 3-4 rows to the sweep and is
     returned by the returner (cap 3) minutes later, every tick.

  2. Row-set mismatch: the PLS-1456 devops-lane exclusion fired even on
     the shed view, so an engineering seat's ready load hid its
     devops-owned rows. The returner has no lane exclusion — it counts
     them — so the sweep undercounted, pulled, and the returner shed the
     pull.

These tests pin:
  1. Strictest-cap-across-rosters parity (multi-roster seat).
  2. Shed-view row parity: devops-owned rows count on an engineering seat.
  3. The min-positive arithmetic matches the returner (a 0-cap roster is
     disabled, not a cap of zero).
  4. A seat on no wip_enforced roster still returns None (legacy deferral
     preserved).

Fixture-auth note (premerge 10769): `pin_org()` patches the roster read the
VERDICT uses, but Item.save's assignment authorization resolves the task org
on its own path, so the setup stack stamps the fence's typed proof for THIS
fixture's own rows only (`authorize_setup_rows`) — the fence stays live for
everything else.
"""
import os
import uuid
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService

_ENV = {
    'PULSE_API_HEALTH_URL': 'disabled',
    'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    'PULSE_HIVE_LIFECYCLE_AVAILABILITY_ENABLED': '0',
}


class _ReturnerParityFixture:
    """Multi-roster seat in its own org. `roster_specs` maps team_name ->
    ready_wip_cap; every roster lists the seat in its flat tiers."""

    def __init__(self, roster_specs, stacked_rows):
        tag = uuid.uuid4().hex[:10]
        self.seat = f'parity{tag}@shizuha.com'
        self.base = 800000 + (int(tag, 16) % 100000)
        self.org_id = 800000 + (int(tag, 16) % 30000)
        for team_name, ready_cap in roster_specs.items():
            TeamRoster.objects.create(
                team_name=team_name,
                organization_slug=f'porg{self.org_id}',
                organization_id=self.org_id,
                tiers=[[self.seat]],
                version=1,
                is_active=True,
                wip_enforced=True,
                ready_wip_cap=ready_cap,
                active_wip_cap=10,
            )
        self.project = Project.objects.create(
            org_id=self.org_id, name=f'Returner parity {self.org_id}',
            key=f'RP{int(tag, 16) % 1000:03d}',
        )
        self.stack_ids = []
        with self.authorize_setup_rows():
            for i, (title, group) in enumerate(stacked_rows):
                self.stack_ids.append(Item.objects.create(
                    title=title, mode='task', status='awaiting_merge',
                    # autonomous-bug seeds awaiting_merge so the row lands in the
                    # READY band (see test_pls1433_shed_parity_rebalance for the
                    # run-9389 active-band-remap signature).
                    priority='high', project=self.project,
                    workflow_name='autonomous-bug',
                    assignment_group=group,
                    assignee_id=self.base, assignee_email=self.seat,
                ).id)

    def authorize_setup_rows(self):
        """Stamp the typed proof the Item.save assignment fence expects for
        THIS fixture's own setup rows.

        `pin_org()` patches the roster read the VERDICT uses, but Item.save's
        assignment authorization resolves the task org on its own path, so a
        setup stack can fail closed during the constructor (run 10769:
        ValidationError 'assignee not in the task organization's target team
        roster'). The fixture's own rosters DO list the seat — these rows are
        authorized content — so stamp the fence's (mismatch, roster_org,
        task_org) tuple for the duration of setup only. The fence stays live
        for everything else, and the verdict under test runs outside this
        patch.
        """
        org_slug = f'porg{self.org_id}'
        return patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            side_effect=lambda item, email, team_name=None, *,
            user_id=None, _t=(False, org_slug, org_slug): _t,
        )

    def probe(self, title='Parity probe', team='engineering'):
        return Item.objects.create(
            title=title, mode='task', status='todo', priority='high',
            project=self.project, assignment_group=team,
        )

    def probe_fresh_org_rosterless(self, title='Parity probe fresh org'):
        """A probe Item in a fresh-org project with a team that has NO roster
        anywhere (platform included).

        PLS-1433 follow-up (run on fix/pls1433-returner-parity-slot-verdict,
        2026-11-14): probing team 'engineering' still returned False, not
        None. Root cause: _team_roster_entry_for_item's PLS-1069 cell C
        borrows the PLATFORM (shizuha) row when the team is shizuha-only,
        and 'engineering' is shizuha-only in the CI database (the fixture
        creates only the 'qa' roster — there is no non-shizuha 'engineering'
        row). Cell C therefore resolved org_slug='shizuha' and
        _wip_roster_config found the live platform engineering row
        (wip_enforced) -> config non-None -> on_enforced_roster True ->
        boolean verdict under the platform cap. (Adding an engineering row
        to a third non-shizuha org would NOT cure this:
        _wip_roster_config force-defaults org_slug to 'shizuha' when the
        entry lookup returns empty, so the platform row is still found.)

        Cure: probe a team name that exists in NO roster anywhere —
        platform lookup returns None, cell C cannot fire, the fresh org has
        zero rows, and _wip_roster_config returns None -> the verdict
        genuinely defers (None).
        """
        fresh_org = self.org_id + 500000
        tag = uuid.uuid4().hex[:6]
        fresh_project = Project.objects.create(
            org_id=fresh_org, name=f'Returner parity fresh {fresh_org}',
            key=f'RF{int(tag, 16) % 1000:03d}',
        )
        return Item.objects.create(
            title=title, mode='task', status='todo', priority='high',
            project=fresh_project,
            assignment_group=f'rosterless{self.base}',
            # Explicit workflow stamp: CI's postgres save-path resolver stamps
            # workflow_name before the task_must_have_workflow_name CHECK;
            # stamping here keeps the fixture DB-agnostic without changing
            # what the verdict-under-test reads.
            workflow_name='autonomous-bug',
        )

    def pin_org(self):
        return patch.object(
            WorkflowService,
            '_team_roster_entry_for_item',
            side_effect=lambda t, item=None,
            _t=([[self.seat]], f'porg{self.org_id}'): _t,
        )


class ReturnerParityVerdictTests(TestCase):
    def test_multi_roster_seat_uses_strictest_cap(self):
        """Cure 1: the verdict reads the returner's strictest-cap policy.

        Seat on engineering (ready 5) + review (ready 3), holding 3 ready
        rows: the item-team-only read said 3 < 5 = under-cap (pull), while
        the returner's policy cap is min(5, 3) = 3 -> returned the pull.
        The verdict must now read full at 3.
        """
        fx = _ReturnerParityFixture(
            roster_specs={'engineering': 5, 'review': 3},
            stacked_rows=[(f'Stack {i}', 'engineering') for i in range(3)],
        )
        probe = fx.probe()
        with patch.dict(os.environ, _ENV, clear=False), fx.pin_org():
            verdict = WorkflowService._slot_at_capacity(
                probe, 'engineering', fx.seat, user_id=fx.base,
            )
        self.assertTrue(verdict, (
            'a seat the returner would reject (strictest ready cap 3, 3 '
            f'assigned-ready rows) must be pull-ineligible, got {verdict!r}'
        ))

    def test_shed_view_counts_devops_rows_on_engineering_seat(self):
        """Cure 2: the PLS-1456 devops-lane exclusion must not fire on the
        shed view. Seat on engineering (ready 5) holding 4 engineering +
        2 devops ready rows: the old shed count (4 < 5) pulled; the
        returner counted 6 > 5 and shed the pull."""
        fx = _ReturnerParityFixture(
            roster_specs={'engineering': 5},
            stacked_rows=[(f'Eng stack {i}', 'engineering') for i in range(4)]
            + [(f'Devops stack {i}', 'devops') for i in range(2)],
        )
        probe = fx.probe()
        with patch.dict(os.environ, _ENV, clear=False), fx.pin_org():
            shed = WorkflowService._agent_slot_queryset(
                probe, fx.seat, 'ready', user_id=fx.base,
                apply_team_role_exclusion=False,
            ).count()
            verdict = WorkflowService._slot_at_capacity(
                probe, 'engineering', fx.seat, user_id=fx.base,
            )
        self.assertEqual(shed, 6, 'shed view must count devops-owned rows')
        self.assertTrue(verdict, (
            '6 assigned-ready rows vs cap 5 must read full; the returner '
            f'would shed any pull onto this seat, got {verdict!r}'
        ))

    def test_zero_cap_roster_is_disabled_not_zero(self):
        """The returner's min-positive arithmetic: a 0-cap roster is
        disabled — the effective cap across engineering (5) and a
        zero/disabled roster is 5, not 0. Four ready rows stay under-cap."""
        fx = _ReturnerParityFixture(
            roster_specs={'engineering': 5, 'qa': 0},
            stacked_rows=[(f'Stack {i}', 'engineering') for i in range(4)],
        )
        probe = fx.probe()
        with patch.dict(os.environ, _ENV, clear=False), fx.pin_org():
            verdict = WorkflowService._slot_at_capacity(
                probe, 'engineering', fx.seat, user_id=fx.base,
            )
        self.assertFalse(verdict, (
            'a 0 (disabled) roster cap must not collapse the seat-level cap '
            f'to zero; 4 rows vs effective cap 5 is under-cap, got {verdict!r}'
        ))

    def test_seat_off_every_roster_still_defers_to_legacy(self):
        """None-semantics preserved: no wip_enforced roster membership in
        the item's org means the returner holds no policy for the seat —
        the verdict must still defer (None) to the legacy counters."""
        fx = _ReturnerParityFixture(
            roster_specs={'qa': 2},
            stacked_rows=[('Stack', 'qa')],
        )
        # The seat IS on a roster (qa), but the probe item's org has no
        # roster the seat sits on for THIS org — probe_fresh_org_rosterless
        # creates the probe in a fresh org from the start (run 10776:
        # mutating an existing project's org trips the immutability trigger
        # tasks_guard_project_org_immutable), with a team name that has NO
        # roster anywhere. The team must be roster-free EVERYWHERE (not
        # just in this org): probing 'engineering' here still hit
        # PLS-1069 cell C, which borrowed the platform (shizuha)
        # engineering row — 'engineering' is shizuha-only in the CI DB —
        # so config was non-None and the verdict returned False instead
        # of deferring.
        # NOTE: pin_org is deliberately NOT applied — it would force the
        # fixture org's roster read onto the fresh-org probe and turn the
        # absent-roster case into an artificial cross-org mismatch. With
        # the read unpatched and the probe team absent from every roster
        # table, config is genuinely None and the no-roster case really
        # runs.
        probe = fx.probe_fresh_org_rosterless()
        with patch.dict(os.environ, _ENV, clear=False):
            verdict = WorkflowService._slot_at_capacity(
                probe, probe.assignment_group, fx.seat, user_id=fx.base,
            )
        self.assertIsNone(verdict, (
            'a seat on no wip_enforced roster in the item\'s org must defer '
            f'to the legacy counters (None), got {verdict!r}'
        ))

"""PLS-1433 shed-parity: the rebalance sweep must never pull onto a seat the
PLS-509 cap would return.

Live loop 2026-10-19 (13:43→13:52 IST, urgent root carriers): the sweep pulled
onto a lane seat stacked with its own in-lane cards because the shared verdict's
ready-band excluded the very rows the PLS-509 returner sheds — a reviewer
holding ~17 merge cards read as idle (~0 after the exclusion) and was pulled
onto minutes before the cap returned the pull. The cure makes the shared
verdict's band counts read the shed view (no team role-exclusion band), so
capacity eligibility and the returner read one identity.

These tests pin:
  1. The shared verdict reads the shed view: a seat whose own-lane cards are
     at the cap is pull-ineligible.
  2. The legacy excluded band (default `_agent_slot_queryset`) still reads the
     author-side exemption for its own callers — the cure moved parity into
     the shared verdict, it did not delete the exemption.
  3. A group-queued task whose only enforcement candidate is at cap stays
     group-queued with zero pull/return comments over N sweeps.
"""
import os
import uuid
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_rebalance_pods

_ENV = {
    'PULSE_API_HEALTH_URL': 'disabled',
    'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    'PULSE_HIVE_LIFECYCLE_AVAILABILITY_ENABLED': '0',
}


def _fake_user_service(users):
    class FakeUS:
        def get_user_by_email(self, email):
            rec = users.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def get_user_by_id(self, user_id):
            for uid, email in users.values():
                if int(user_id) == uid:
                    return {'id': uid, 'email': email}
            return None
    return FakeUS()


class _ShedParityFixture:
    """Org-scoped enforced devops roster (ready=2) + a lane seat stacked with
    own-lane cards."""

    def __init__(self):
        tag = uuid.uuid4().hex[:10]
        self.stacked = f'stacked{tag}@shizuha.com'
        self.base = 700000 + (int(tag, 16) % 100000)
        self.org_id = 700000 + (int(tag, 16) % 30000)
        TeamRoster.objects.create(
            team_name='devops',
            organization_slug=f'torg{self.org_id}',
            organization_id=self.org_id,
            tiers=[[self.stacked]],
            version=1,
            is_active=True,
            wip_enforced=True,
            ready_wip_cap=2,
            active_wip_cap=10,
        )
        self.project = Project.objects.create(
            org_id=self.org_id, name=f'Parity {self.org_id}',
            key=f'PB{int(tag, 16) % 1000:03d}',
        )
        self.users = {self.stacked: (self.base, self.stacked)}
        self.stack_ids = []
        for _i, (title, priority) in enumerate((
            ('Stacked reviewer merge card', 'urgent'),
            ('Second stack card', 'high'),
        )):
            self.stack_ids.append(Item.objects.create(
                title=title, mode='task', status='awaiting_merge',
                # Pin a merge-lane workflow. Items with no explicit workflow
                # auto-attach the system 'simple' workflow (todo/in_progress/
                # done only), and Item.save()'s _coerce_invalid_status then
                # remaps 'awaiting_merge' -> 'in_progress' (same display
                # category, lowest position) on insert. 'in_progress' sits in
                # the ACTIVE band, which the ready band subtracts, so the
                # stacked cards silently vanish from both the shed and the
                # excluded views — the run-9389 shed=0 signature. Explicitly
                # attaching 'autonomous-bug' (which seeds awaiting_merge) is
                # what makes these rows real pipeline carriers.
                priority=priority, project=self.project,
                workflow_name='autonomous-bug',
                assignment_group='devops',
                assignee_id=self.base, assignee_email=self.stacked,
            ).id)

    def probe(self, title='Group-queued probe'):
        # Ordinary priority, NOT 'urgent': urgent work is cap-exempt by
        # operator policy 2026-09-27 (`_priority_ignores_wip_cap`), so an
        # urgent probe short-circuits the verdict's `not ignores_cap` leg and
        # can never witness pull-ineligibility — the run-9393 False signature
        # was the exemption firing correctly on an urgent probe, not a verdict
        # defect. The pinned scenario is ordinary-priority work.
        return Item.objects.create(
            title=title, mode='task', status='todo', priority='high',
            project=self.project, assignment_group='devops',
        )


class ShedParityVerdictTests(TestCase):
    def test_shared_verdict_reads_shed_view(self):
        fx = _ShedParityFixture()
        probe = fx.probe()
        with patch.dict(os.environ, _ENV, clear=False), \
             patch.object(
                 WorkflowService,
                 '_team_roster_entry_for_item',
                 # Pin the org-resolution leg. `_wip_roster_config` resolves
                 # the roster row via `_team_roster_entry_for_item`, whose
                 # org lookup is environmental (admin-service); where it
                 # cannot resolve it defaults to org_slug='shizuha' and the
                 # lookup finds a system-wide devops roster with a large cap
                 # — the seat reads under-cap and the verdict returns False
                 # (run-9391 signature). Pinning the resolver to the fixture's
                 # org keeps the row match + band arithmetic under test.
                 side_effect=lambda team, item=None,
                 _t=([[fx.stacked]], f'torg{fx.org_id}'): _t,
             ):
            verdict = WorkflowService._slot_at_capacity(
                probe, 'devops', fx.stacked, user_id=fx.base,
            )
            # Self-diagnosing message: any future red carries the live leg
            # values in the assertion text (run-9393 class took three vantage
            # pulls to localize).
            config = WorkflowService._wip_roster_config('devops', probe)
            ready_n = WorkflowService._agent_slot_queryset(
                probe, fx.stacked, 'ready', user_id=fx.base,
                apply_team_role_exclusion=False,
            ).count()
            active_n = WorkflowService._agent_slot_queryset(
                probe, fx.stacked, 'active', user_id=fx.base,
                apply_team_role_exclusion=False,
            ).count()
        self.assertTrue(verdict, (
            'seat stacked with own-lane cards must be pull-ineligible '
            f'[diag: verdict={verdict!r} config={config!r} '
            f'ready_cap={getattr(config, "ready_wip_cap", None)!r} '
            f'active_cap={getattr(config, "active_wip_cap", None)!r} '
            f'ready_n={ready_n} active_n={active_n} '
            f'probe_priority={probe.priority!r}]'
        ))

    def test_legacy_band_keeps_author_exemption_for_own_callers(self):
        fx = _ShedParityFixture()
        probe = fx.probe(title='Legacy excluded-band probe')
        # Pin the role-exclusion config this test's semantics assume. The
        # shipped `_CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM` keys only
        # 'engineering', so an unpatched devops run flaps per environment
        # (ryo cmt 1124351 on premerge run 9376: shed=2 asserted, excluded
        # band counted the stacked rows). The pin preserves the pinned
        # intent: the legacy band applies a CONFIGURED own-lane role
        # exclusion while the shed query counts every band row.
        pinned = dict(WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM)
        pinned['devops'] = {'awaiting_merge'}
        with patch.dict(os.environ, _ENV, clear=False), \
             patch.object(
                 WorkflowService,
                 '_CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM',
                 pinned,
             ):
            excluded = WorkflowService._agent_slot_queryset(
                probe, fx.stacked, 'ready', user_id=fx.base,
            ).count()
            shed = WorkflowService._agent_slot_queryset(
                probe, fx.stacked, 'ready', user_id=fx.base,
                apply_team_role_exclusion=False,
            ).count()
        self.assertEqual(excluded, 0)
        self.assertEqual(shed, 2)


class ShedParitySweepTests(TestCase):
    def test_no_pull_return_pair_at_cap(self):
        fx = _ShedParityFixture()
        probe = fx.probe(title='Sweep probe at cap')
        for _ in range(3):
            with patch.dict(os.environ, _ENV, clear=False), \
                 patch('tasks.user_service.user_service', _fake_user_service(fx.users)), \
                 patch.object(
                     WorkflowService, '_is_agent_available_for_assignment',
                     side_effect=lambda e: e.strip().lower() == fx.stacked,
                 ), \
                 patch.object(
                     WorkflowService,
                     '_team_roster_entry_for_item',
                     # Pin the org-resolution leg for the sweep's shared
                     # verdict too: without it `_wip_roster_config` resolves
                     # to the 'shizuha' fallback slug, finds no row, and the
                     # verdict returns None — which the sweep's enforced
                     # branch treats as under-cap (run-9395 signature: the
                     # pull landed on the at-cap seat).
                     side_effect=lambda team, item=None,
                     _t=([[fx.stacked]], f'torg{fx.org_id}'): _t,
                 ):
                sweep_rebalance_pods.run()
        probe.refresh_from_db()
        self.assertFalse(probe.assignee_email, 'pull must never land on an at-cap seat')
        texts = list(
            Comment.objects.filter(item=probe)
            .values_list('content', flat=True)
        )
        self.assertFalse(
            [t for t in texts if 'Pulled from' in t or 'Returned to' in t],
            f'zero pull/return comments expected, got {texts}',
        )

"""SRCH-133 regression: the org-health sweep's routable queue must NOT count
`resolved_elsewhere` items as actionable demand.

PLAT-8802's blocker resolved elsewhere on 2026-09-15 and the item has sat in
`resolved_elsewhere` since — deliberately NON-terminal (Pulse
`BLOCK_RELEASING_NONTERMINAL_SLUGS`, PLS-719/PLS-1261, so the blocker-release
machinery can discover it) but NOT workable by any seat (the resolver's
active-queue ready lanes exclude it). The sweep counted it anyway, so the
stalest such item became a PHANTOM demand leg and a permanent availability
probe: `[Team Outage]` carriers PLAT-9765/9733/9772 all cited
"oldest: PLAT-8802, 208h" 8 days after the blocker closed.

Fix (this file's subject): `_org_health_routable_queue()` drops
`resolved_elsewhere` via `_ORG_HEALTH_UNACTIONABLE_QUEUE_STATUSES`, so a
`resolved_elsewhere` item can no longer satisfy the demand leg or serve as the
probe item. PLS-1382 later also excluded the slug from capacity-pull via
`_assignment_excluded_statuses`; block-release refuse-leg guards stay
byte-identical.
"""
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    _ORG_HEALTH_UNACTIONABLE_QUEUE_STATUSES,
    _org_health_routable_queue,
)


class ResolvedElsewhereUnitTests(TestCase):
    """Pure-logic: `resolved_elsewhere` is dropped, awaiting-routing kept."""

    def _stub(self, status):
        return SimpleNamespace(status=status, item_key=f'X-{status}')

    def test_resolved_elsewhere_is_dropped(self):
        self.assertEqual(
            _org_health_routable_queue([self._stub('resolved_elsewhere')]), [],
            "a resolved_elsewhere item is not actionable demand")

    def test_match_is_case_insensitive(self):
        self.assertEqual(
            _org_health_routable_queue([self._stub('Resolved_Elsewhere')]), [])

    def test_awaiting_routing_statuses_are_still_kept(self):
        for st in ('needs_triage', 'open', 'todo'):
            self.assertEqual(len(_org_health_routable_queue([self._stub(st)])), 1)

    def test_mixed_queue_keeps_only_actionable(self):
        q = [self._stub('resolved_elsewhere'), self._stub('todo'),
             self._stub('open')]
        kept = {it.status for it in _org_health_routable_queue(q)}
        self.assertEqual(kept, {'todo', 'open'})

    def test_constant_is_scoped_to_the_phantom_slug(self):
        self.assertEqual(_ORG_HEALTH_UNACTIONABLE_QUEUE_STATUSES,
                         {'resolved_elsewhere'})


class ResolvedElsewhereDbIntegrationTests(TestCase):
    """DB-level: the observed PLAT-8802 shape in the admin-ops lane — the
    stalest item is `resolved_elsewhere`, a workable `todo` item is fresher.
    PLS-1382: `_queued_tasks_for_team` now excludes `resolved_elsewhere` via
    `_assignment_excluded_statuses`, so the capacity-pull queue itself never
    sees the phantom; the sweep filter remains a belt-and-suspenders defense.
    """

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open')
        WorkflowService._roster_cache = None
        self._make('PLAT-9999', 'todo')

    def _make(self, key, status):
        return Item.objects.create(
            project=self.project, item_key=key, title=f'{key} item',
            mode='task', item_type='task.generic', workflow_name='simple',
            workflow_status=status, status=status, assignment_group='admin-ops',
            assignee_email='', created_by_email='system@pulse', created_by_id=0,
        )

    def _make_aged_phantom(self):
        """The PLAT-8802 shape: stalest item is `resolved_elsewhere` (9d old).
        Uses update() for created_at because create() applies the DB default."""
        phantom = self._make('PLAT-8802', 'resolved_elsewhere')
        Item.objects.filter(pk=phantom.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=9))
        return phantom

    def test_raw_queue_excludes_phantom_sweep_filter_keeps_todo(self):
        self._make_aged_phantom()
        raw = list(WorkflowService._queued_tasks_for_team('admin-ops'))
        self.assertEqual(
            {i.item_key for i in raw}, {'PLAT-9999'},
            'PLS-1382: capacity-pull must not see resolved_elsewhere',
        )

        routable = _org_health_routable_queue(raw)
        self.assertEqual([i.item_key for i in routable], ['PLAT-9999'])

    def test_phantom_cannot_satisfy_demand_leg_or_probe(self):
        self._make_aged_phantom()
        raw = list(WorkflowService._queued_tasks_for_team('admin-ops'))
        routable = _org_health_routable_queue(raw)
        self.assertEqual(
            [i.item_key for i in routable], ['PLAT-9999'],
            "the phantom must not count toward the demand leg")
        # probe derivation used by the sweep (min by created_at) cannot see it
        oldest = min(routable, key=lambda i: i.created_at)
        self.assertNotEqual(oldest.item_key, 'PLAT-8802')

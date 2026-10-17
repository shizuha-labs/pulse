"""PLS-168: replenishment / Definition-of-Ready sweep (sweep_replenish_ready).

The upstream complement to the rebalance sweep: when a team's READY queue
(workable non-epic open/todo) is below target AND an under-cap member is
available, spawn bounded 'Groom & decompose' tasks from the team's UNGROOMED
epics so a pull-based team never starves while epics pile in the backlog.
Also lifts epics off the coordinator/CoS identity onto the owning team.
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, TeamRoster
from tasks.tasks import sweep_replenish_ready


def _echo_ref(ref):
    # Pure resolver: dict -> (id, email); avoids the user_service round-trip.
    if isinstance(ref, dict):
        return ref.get('id'), ref.get('email')
    return None, None


@patch('tasks.models.Item._resolve_user_ref', staticmethod(_echo_ref))
class ReplenishReadyTests(TestCase):
    COORD = 'shizuha@shizuha.com'

    TEAM = 'pls37-test-team'

    def setUp(self):
        # Distinct team name so we don't collide with migration-seeded rosters and
        # other seeded teams (which have no Items in the test DB → the sweep skips them).
        TeamRoster.objects.get_or_create(
            team_name=self.TEAM,
            defaults=dict(is_active=True, tiers=[['hiro@shizuha.com']], junior_emails=[]),
        )

    def _epic(self, **kw):
        d = dict(title='[EPIC] Frontier cortex', mode='task', item_type='epic',
                 status='backlog', assignment_group=self.TEAM,
                 assignee_email=self.COORD, priority='high')
        d.update(kw)
        return Item.objects.create(**d)

    def _ready(self, n):
        for i in range(n):
            Item.objects.create(title=f'ready {i}', mode='task', item_type='task.generic',
                                status='open', assignment_group=self.TEAM)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_spawns_groom_task_and_reowns_epic(self, _mr):
        epic = self._epic()
        res = sweep_replenish_ready.run(ready_target=1)
        self.assertEqual(res['groom_spawned'], 1)
        self.assertEqual(res['epics_reowned'], 1)
        child = Item.objects.filter(parent_item=epic).first()
        self.assertIsNotNone(child)
        self.assertTrue(child.title.startswith('Groom & decompose:'))
        self.assertEqual(child.status, 'open')
        self.assertEqual(child.assignment_group, self.TEAM)
        self.assertEqual(child.assignee_email, 'hiro@shizuha.com')
        epic.refresh_from_db()
        self.assertEqual(epic.metadata.get('groom_spawned'), child.item_key or str(child.id))
        # epic lifted off the coordinator onto the team member
        self.assertEqual(epic.assignee_email, 'hiro@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_idempotent_no_double_spawn(self, _mr):
        self._epic()
        sweep_replenish_ready.run(ready_target=1)
        res2 = sweep_replenish_ready.run(ready_target=1)
        self.assertEqual(res2['groom_spawned'], 0)
        # exactly one grooming child total
        self.assertEqual(Item.objects.filter(title__startswith='Groom & decompose:').count(), 1)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_no_spawn_when_ready_queue_full(self, _mr):
        self._epic()
        self._ready(3)
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 0)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(None, None))
    def test_no_spawn_when_no_available_member(self, _mr):
        # all members at cap / offline -> resolver returns (None,None) -> hold
        self._epic()
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 0)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_skips_epic_already_in_decomposition(self, _mr):
        epic = self._epic()
        # an existing child via parent_item = decomposition already started
        Item.objects.create(title='child story', mode='task', item_type='task.generic',
                            status='open', assignment_group='engineering', parent_item=epic)
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 0)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_skips_epic_decomposed_via_itemlink(self, _mr):
        # Real roadmap epics carry their slices as ItemLinked tasks (epic as link
        # target), NOT parent_item children. Such an epic must NOT be re-groomed.
        from tasks.models import ItemLink
        epic = self._epic()
        slice_ = Item.objects.create(title='Slice 1: do a thing', mode='task',
                                     item_type='task.generic', status='in_progress',
                                     assignment_group='engineering')
        ItemLink.objects.create(source_item=slice_, target_item=epic,
                                link_type='blocks', created_by_id=0)
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 0)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_existing_groom_task_without_metadata_is_idempotency_evidence(self, _mr):
        # PLAT-1055: a prior grooming child with missing metadata is evidence that
        # replenishment already happened (or a concurrent worker won the race). The
        # sweep must self-heal metadata instead of spawning a duplicate groom task.
        epic = self._epic()
        existing = Item.objects.create(title='Groom & decompose: [EPIC] x', mode='task',
                                       item_type='task.generic', status='open',
                                       assignment_group=self.TEAM, parent_item=epic)
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 0)
        self.assertEqual(Item.objects.filter(title__startswith='Groom & decompose:').count(), 1)
        epic.refresh_from_db()
        self.assertEqual(epic.metadata.get('groom_spawned'), existing.item_key or str(existing.id))

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_epic_provenance_link_does_not_mask_grooming(self, _mr):
        # 2026-07-02: the PLAT-197 seeding epic links itself to the roadmap
        # epics it creates. A link to another EPIC is provenance, not
        # decomposition — such epics must still be groomable (15 sat invisible
        # to replenishment because their ONLY link was their seed epic).
        from tasks.models import ItemLink
        seed = Item.objects.create(title='[EPIC] seed backlogs', mode='task',
                                   item_type='epic', status='done',
                                   assignment_group=self.TEAM)
        epic = self._epic(title='[EPIC] roadmap item, seed-linked only')
        ItemLink.objects.create(source_item=seed, target_item=epic,
                                link_type='relates_to', created_by_id=0)
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 1)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(101, 'hiro@shizuha.com'))
    def test_bounded_to_ready_deficit(self, _mr):
        # 5 epics but target 3, ready 0 -> spawn exactly 3 (bounded, no flood)
        for i in range(5):
            self._epic(title=f'[EPIC] roadmap {i}')
        res = sweep_replenish_ready.run(ready_target=3)
        self.assertEqual(res['groom_spawned'], 3)

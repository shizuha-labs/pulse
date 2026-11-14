"""PLS-1486: the WIP returner must not shed urgent rows that the queue
assigner admits cap-exempt (``_priority_ignores_wip_cap``).

Live loop (13-14 Nov): ``_reclaim_wip_overload`` shed urgent ready rows above
``ready_wip_cap`` at every */10 rebalance tick and ``sweep_assign_group_queued``
re-pushed them to the SAME seat minutes later — 4,427 same-seat loops/day on
116 urgent items (12:41 IST ``ready_queued: 34`` -> 12:47 ``assigned: 34``).
"""
import os
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reclaim_wip_overload


WORKER = 'worker@shizuha.com'

LIVE_SEAT = patch.object(
    WorkflowService, '_is_agent_available_for_assignment', return_value=True,
)


def _set_roster(ready_cap=5, active_cap=5):
    TeamRoster.objects.update_or_create(
        organization_id=1,
        team_name='engineering',
        defaults={
            'organization_slug': 'shizuha',
            'tiers': [[WORKER]],
            'ready_wip_cap': ready_cap,
            'active_wip_cap': active_cap,
            'is_active': True,
            'wip_enforced': True,
        },
    )


def _item(project, *, title, priority, status='open'):
    return Item.objects.create(
        title=title,
        mode='task',
        status=status,
        priority=priority,
        assignment_group='engineering',
        workflow_name='autonomous-dev',
        project=project,
        assignee_email=WORKER,
    )


class ReclaimUrgentExemptTests(TestCase):

    def setUp(self):
        self.project = Project.objects.create(
            org_id=1, name='PLS-1486 Urgent Reclaim', key='PLSU',
        )

    def test_urgent_only_overflow_sheds_nothing(self):
        # Cap 5, 9 urgent ready rows (the live ichi shape): 0 shed.
        _set_roster(ready_cap=5, active_cap=5)
        urgents = [_item(self.project, title=f'u{i}', priority='urgent')
                   for i in range(9)]
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        for it in urgents:
            it.refresh_from_db()
            self.assertEqual(it.assignee_email, WORKER,
                             'urgent rows are cap-exempt and must not shed')
        self.assertEqual(result['ready_queued'], 0)

    def test_mixed_sheds_only_non_urgent(self):
        # Cap 5, 3 urgent + 4 normal: urgent kept and counted -> 2 normal shed.
        _set_roster(ready_cap=5, active_cap=5)
        urgents = [_item(self.project, title=f'u{i}', priority='urgent')
                   for i in range(3)]
        normals = [_item(self.project, title=f'n{i}', priority='normal')
                   for i in range(4)]
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        for it in urgents:
            it.refresh_from_db()
            self.assertEqual(it.assignee_email, WORKER)
        shed = 0
        for it in normals:
            it.refresh_from_db()
            shed += it.assignee_email is None
        self.assertEqual(shed, 2, 'urgent rows still count toward the cap')
        self.assertEqual(result['ready_queued'], 2)

    def test_urgent_over_cap_still_sheds_all_non_urgent(self):
        # Urgent alone exceeds the cap: every non-urgent sibling sheds.
        _set_roster(ready_cap=2, active_cap=2)
        for i in range(3):
            _item(self.project, title=f'u{i}', priority='urgent')
        normal = _item(self.project, title='n', priority='normal')
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        normal.refresh_from_db()
        self.assertIsNone(normal.assignee_email)
        self.assertEqual(result['ready_queued'], 1)

    def test_disabled_seat_urgent_still_sheds(self):
        _set_roster(ready_cap=1, active_cap=1)
        a = _item(self.project, title='ua', priority='urgent')
        b = _item(self.project, title='ub', priority='urgent')
        with patch.object(WorkflowService, '_disabled_agent_emails',
                          return_value={WORKER}):
            result = _reclaim_wip_overload()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertGreaterEqual(
            [a.assignee_email, b.assignee_email].count(None), 1,
            'a disabled seat loses the urgent exemption',
        )
        self.assertGreaterEqual(result['ready_queued'], 1)

    def test_kill_switch_restores_old_behaviour(self):
        _set_roster(ready_cap=2, active_cap=2)
        for i in range(4):
            _item(self.project, title=f'u{i}', priority='urgent')
        with LIVE_SEAT, patch.dict(os.environ,
                                   {'PULSE_RECLAIM_URGENT_EXEMPT': '0'}):
            result = _reclaim_wip_overload()
        self.assertEqual(result['ready_queued'], 2)

    def test_pull_side_still_treats_urgent_as_cap_exempt(self):
        # Parity anchor: the rule the returner now mirrors.
        self.assertTrue(WorkflowService._priority_ignores_wip_cap('urgent'))
        self.assertFalse(WorkflowService._priority_ignores_wip_cap('high'))

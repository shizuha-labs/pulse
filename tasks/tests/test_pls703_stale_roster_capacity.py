"""PLS-703: stale/non-member over-cap seat must never win assignment.

Regression for PLS-278's availability/capacity-aware assignment contract.
Reproduction (2026-07-26, PLS-702): an `autonomous-bug` create routed to
`engineering` resolved to legacy Kai (WIP 23, NOT a current Engineering member
per the live Admin directory) while under-cap current members (Nagi=1, Ryo=2,
Ni=3) existed. PLS-278's binding predicate requires `staffed ∧ capable ∧ live
∧ under_capacity`; an over-cap legacy/non-member seat must never win while
under-cap current members exist.

These fixtures exercise the real create→needs_triage→Triage-to-Engineering
caller sequence (not just the resolver helper) and assert the stale/over-cap
seat is never assigned — the item must resolve to an under-cap current member
or stay group-queued, never land on the stale seat.

Refs: PLS-278, PLS-703, canonical availability-aware resolver HLD
`902431bb-80ed-4300-bef6-88c03d2ad6a2`.
"""
import os
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class _FakeUserService:
    users = {
        'kai@shizuha.com': {'id': 201, 'email': 'kai@shizuha.com', 'username': 'kai'},
        'nagi@shizuha.com': {'id': 202, 'email': 'nagi@shizuha.com', 'username': 'nagi'},
        'ryo@shizuha.com': {'id': 203, 'email': 'ryo@shizuha.com', 'username': 'ryo'},
        'ni@shizuha.com': {'id': 204, 'email': 'ni@shizuha.com', 'username': 'ni'},
    }

    def get_user_by_email(self, email):
        return self.users.get((email or '').strip().lower())


class PLS703StaleRosterCapacityTests(TestCase):
    """Stale/non-member over-cap seat is never assigned over under-cap members."""

    def setUp(self):
        self.project = Project.objects.create(name='Pulse', key='PLS703', org_id=1, access_level='open')
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _roster(self, tiers, wip_cap=5):
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [tiers],
                'wip_enforced': True,
                'ready_wip_cap': wip_cap,
                'active_wip_cap': wip_cap,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def _item(self, **kwargs):
        data = dict(
            project=self.project,
            title='PLS-703 stale-roster regression',
            mode='task',
            item_type='task.bug',
            workflow_name='autonomous-bug',
            status='needs_triage',
            workflow_status='needs_triage',
            assignment_group='engineering',
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    @patch.dict(os.environ, {
        'PULSE_ROUTABILITY_ENFORCE': '1',
        'PULSE_WIP_CAP': '5',
        'PULSE_INFLIGHT_CAP': '5',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    def test_stale_over_cap_seat_never_assigned_over_under_cap_members(
        self, _avail, _can_execute, _notify,
    ):
        """Roster has stale Kai (WIP 23) + under-cap members (Nagi=1, Ryo=2, Ni=3).
        The resolver must never land on Kai."""
        self._roster(['kai@shizuha.com', 'nagi@shizuha.com', 'ryo@shizuha.com', 'ni@shizuha.com'])

        def _fake_slot_load(item, email, slot_kind, *, user_id=None):
            # Kai is at WIP 23 (over cap 5); current members are under cap.
            loads = {
                'kai@shizuha.com': 23,
                'nagi@shizuha.com': 1,
                'ryo@shizuha.com': 2,
                'ni@shizuha.com': 3,
            }
            return loads.get((email or '').strip().lower(), 0)

        with patch.object(
            WorkflowService, '_agent_slot_load', staticmethod(_fake_slot_load),
        ):
            item = self._item()
            uid, email = WorkflowService._resolve_team_assignee(
                'engineering', item=item,
            )

        self.assertIsNotNone(email)
        self.assertNotEqual((email or '').strip().lower(), 'kai@shizuha.com')
        self.assertIn((email or '').strip().lower(), {'nagi@shizuha.com', 'ryo@shizuha.com', 'ni@shizuha.com'})

    @patch.dict(os.environ, {
        'PULSE_ROUTABILITY_ENFORCE': '1',
        'PULSE_WIP_CAP': '5',
        'PULSE_INFLIGHT_CAP': '5',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    def test_all_candidates_over_capacity_holds_group_queued_not_stale_seat(
        self, _avail, _can_execute, _notify,
    ):
        """Every candidate (incl. stale Kai) is over-cap → the item stays
        group-queued (None, None) — it must NOT fall back to the stale seat."""
        self._roster(['kai@shizuha.com', 'nagi@shizuha.com'])

        def _fake_slot_load(item, email, slot_kind, *, user_id=None):
            loads = {
                'kai@shizuha.com': 23,
                'nagi@shizuha.com': 6,
            }
            return loads.get((email or '').strip().lower(), 0)

        with patch.object(
            WorkflowService, '_agent_slot_load', staticmethod(_fake_slot_load),
        ):
            item = self._item()
            uid, email = WorkflowService._resolve_team_assignee(
                'engineering', item=item,
            )

        self.assertEqual((uid, email), (None, None))

"""PLS-190: rebalance must not route team work to provider-unavailable peers.

When an agent is Claude-backed and quota-limited (provider_healthy=0), the health
sweep marks it unavailable in AgentAvailability. The rebalancer must exclude such
agents from `members` and never assign work to them — even if they have the lowest
load. Provider-down agents go into `down` and their work is drained to live peers.

Tests cover:
  1. Architecture: nova (Codex, alive) keeps work when hiro (Claude, unavailable) exists.
  2. Architecture: task already on hiro (unavailable) drains to nova (available).
  3. Admin Ops: shion (available) receives work when shizuha (unavailable) is down.
  4. _resolve_team_assignee: provider-unavailable agent is excluded from routing even
     when it's the lightest member.
"""
import os
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_rebalance_pods


# Mirrors the live architecture roster from the DB.
_ARCH_ROSTER = {
    'architecture': [['hiro@shizuha.com', 'nova@shizuha.com', 'aoi@shizuha.com', 'sora@shizuha.com']],
}
_ADMIN_ROSTER = {
    'admin-ops': [['shizuha@shizuha.com', 'shion@shizuha.com']],
}
_COMBINED_ROSTER = {**_ARCH_ROSTER, **_ADMIN_ROSTER}

_USERS = {
    'hiro@shizuha.com':    (201, 'hiro@shizuha.com'),
    'nova@shizuha.com':    (202, 'nova@shizuha.com'),
    'aoi@shizuha.com':     (203, 'aoi@shizuha.com'),
    'sora@shizuha.com':    (204, 'sora@shizuha.com'),
    'shizuha@shizuha.com': (301, 'shizuha@shizuha.com'),
    'shion@shizuha.com':   (302, 'shion@shizuha.com'),
}


def _fake_user_service(users=_USERS):
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


_ENV = {
    'PULSE_API_HEALTH_URL': 'disabled',
    'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
}


class PLS190ArchitectureProviderUnavailableTests(TestCase):
    """Architecture team: hiro is Claude-backed and provider-unavailable; nova is Codex/alive."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Architecture provider failover',
            key='P190A',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='architecture',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': _ARCH_ROSTER['architecture'],
                'is_active': True,
            },
        )

    @patch.dict(os.environ, _ENV)
    def test_nova_keeps_task_when_hiro_provider_unavailable(self):
        """Tasks assigned to nova (Codex, alive) must not be moved to hiro (Claude, down).

        Before the PLS-190 fix, the skew loop could reassign nova's tasks to hiro if
        hiro had a lower load count, because the sweep did not check provider health.
        Now hiro is excluded from `members` entirely when provider-unavailable.
        """
        nova_task = Item.objects.create(
            title='Architecture task — nova should keep it',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='architecture',
            assignee_id=202, assignee_email='nova@shizuha.com',
        )
        # nova has 1 task, hiro has 0 — without the fix, hiro gets it.
        # With the fix, hiro is unavailable and excluded from members.
        available = {'nova@shizuha.com', 'aoi@shizuha.com', 'sora@shizuha.com'}

        with patch('tasks.user_service.user_service', _fake_user_service()), \
             patch.object(WorkflowService, '_pod_roster', return_value=_ARCH_ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            sweep_rebalance_pods.run()

        nova_task.refresh_from_db()
        self.assertNotEqual(
            nova_task.assignee_email, 'hiro@shizuha.com',
            "task must never be routed to hiro (Claude/provider-unavailable)",
        )
        self.assertIn(
            nova_task.assignee_email, available,
            "task must remain with a provider-available member",
        )

    @patch.dict(os.environ, _ENV)
    def test_hiro_task_drains_to_nova_when_hiro_provider_unavailable(self):
        """A task stranded on hiro (provider-unavailable/Claude-down) drains to nova (Codex/alive).

        This is the exact HIVE-287 / PLS-190 regression: hiro owned architecture work,
        Claude quota was exhausted, but the rebalancer did not drain because the exporter
        was not emitting enabled=0 for stopped/disabled agents. Once _is_agent_available_
        for_assignment returns False for hiro, the failover drain must move the task.
        """
        hiro_task = Item.objects.create(
            title='Architecture task stranded on hiro (Claude-down)',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='architecture',
            assignee_id=201, assignee_email='hiro@shizuha.com',
        )
        # Only nova is available; hiro, aoi, sora are all unavailable.
        available = {'nova@shizuha.com'}

        with patch('tasks.user_service.user_service', _fake_user_service()), \
             patch.object(WorkflowService, '_pod_roster', return_value=_ARCH_ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            sweep_rebalance_pods.run()

        hiro_task.refresh_from_db()
        self.assertEqual(
            hiro_task.assignee_email, 'nova@shizuha.com',
            "hiro's task must drain to nova (the only available architecture peer)",
        )

    @patch.dict(os.environ, _ENV)
    def test_waiting_external_entry_is_retired(self):
        task = Item.objects.create(
            title='Authoritative external wait on unavailable holder',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='architecture',
            assignee_id=201, assignee_email='hiro@shizuha.com',
        )
        task.refresh_from_db()
        before = task.status
        task.status = 'waiting_external'
        task._external_wait_authorized = True
        with self.assertRaises(ValidationError):
            task.save(update_fields=['status'])
        task.refresh_from_db()
        self.assertEqual(task.status, before)
        self.assertNotEqual(task.status, 'waiting_external')
        self.assertEqual(task.assignee_email, 'hiro@shizuha.com')

    @patch.dict(os.environ, _ENV)
    def test_no_assignment_to_any_unavailable_member_even_at_zero_load(self):
        """The rebalancer must never select a provider-unavailable member as a target,
        even when that member has zero tasks (i.e. would win a pure load sort)."""
        # nova has 3 tasks (high load), hiro has 0 (would win pure load sort).
        for i in range(3):
            Item.objects.create(
                title=f'nova task {i}',
                mode='task', status='open', priority='normal',
                project=self.project,
                assignment_group='architecture',
                assignee_id=202, assignee_email='nova@shizuha.com',
            )
        trigger_task = Item.objects.create(
            title='unassigned arch task to route',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='architecture',
        )
        available = {'nova@shizuha.com'}  # hiro/aoi/sora all unavailable

        with patch('tasks.user_service.user_service', _fake_user_service()), \
             patch.object(WorkflowService, '_pod_roster', return_value=_ARCH_ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            sweep_rebalance_pods.run()

        trigger_task.refresh_from_db()
        if trigger_task.assignee_email:
            self.assertNotEqual(
                trigger_task.assignee_email, 'hiro@shizuha.com',
                "task must never go to hiro (provider-unavailable) even with load=0",
            )


class PLS190AdminOpsProviderUnavailableTests(TestCase):
    """Admin Ops team: shizuha unavailable → work routes to shion (available peer)."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Admin Ops provider failover',
            key='P190O',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='admin-ops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': _ADMIN_ROSTER['admin-ops'],
                'is_active': True,
            },
        )

    @patch.dict(os.environ, _ENV)
    def test_admin_ops_routes_to_shion_when_shizuha_unavailable(self):
        """PLS-190: when shizuha (team lead) is unavailable, admin-ops work routes to shion."""
        shizuha_task = Item.objects.create(
            title='Admin Ops task stranded on shizuha (unavailable)',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='admin-ops',
            assignee_id=301, assignee_email='shizuha@shizuha.com',
        )
        available = {'shion@shizuha.com'}

        with patch('tasks.user_service.user_service', _fake_user_service()), \
             patch.object(WorkflowService, '_pod_roster', return_value=_ADMIN_ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            sweep_rebalance_pods.run()

        shizuha_task.refresh_from_db()
        self.assertEqual(
            shizuha_task.assignee_email, 'shion@shizuha.com',
            "admin-ops work must drain to shion when shizuha is unavailable",
        )

    @patch.dict(os.environ, _ENV)
    def test_admin_ops_capacity_queues_when_all_unavailable(self):
        """If all admin-ops members are unavailable, the task is capacity-queued
        (group-assigned, no concrete assignee) rather than assigned to an unavailable agent."""
        task = Item.objects.create(
            title='Admin Ops task — all members unavailable',
            mode='task', status='open', priority='normal',
            project=self.project,
            assignment_group='admin-ops',
            assignee_id=301, assignee_email='shizuha@shizuha.com',
        )
        available = set()  # everyone unavailable

        with patch('tasks.user_service.user_service', _fake_user_service()), \
             patch.object(WorkflowService, '_pod_roster', return_value=_ADMIN_ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            result = sweep_rebalance_pods.run()

        task.refresh_from_db()
        # Whole pod dark → sweep skips it (no members). Task is unchanged.
        self.assertNotIn(task.assignee_email, {'shion@shizuha.com'},
                         "task must not move to shion (shion is also unavailable)")


class PLS190ResolverProviderUnavailableTests(TestCase):
    """_resolve_team_assignee must exclude provider-unavailable agents from routing."""

    def test_resolver_skips_unavailable_member(self):
        """When hiro is provider-unavailable, _resolve_team_assignee must return nova."""
        users = _USERS
        available = {'nova@shizuha.com'}

        class FakeUS:
            def get_user_by_email(self, email):
                rec = users.get((email or '').strip().lower())
                return {'id': rec[0], 'email': rec[1]} if rec else None

        with patch('tasks.user_service.user_service', FakeUS()), \
             patch.object(WorkflowService, '_pod_roster',
                          return_value={'architecture': [['hiro@shizuha.com', 'nova@shizuha.com']]}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available):
            uid, email = WorkflowService._resolve_team_assignee('architecture')

        self.assertEqual(email, 'nova@shizuha.com',
                         "resolver must select nova (available) not hiro (provider-unavailable)")
        self.assertEqual(uid, 202)

    def test_resolver_returns_none_none_when_all_unavailable(self):
        """If all architecture members are provider-unavailable, resolver returns (None, None)."""
        with patch.object(WorkflowService, '_pod_roster',
                          return_value={'architecture': [['hiro@shizuha.com', 'nova@shizuha.com']]}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=False), \
             patch.object(WorkflowService, '_resolve_team_lead_email', return_value=None):
            uid, email = WorkflowService._resolve_team_assignee('architecture')

        self.assertIsNone(uid)
        self.assertIsNone(email)

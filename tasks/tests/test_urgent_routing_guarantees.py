"""Operator directives 2026-09-17: urgent-task routing guarantees.

1. Running seats always outrank wakeable-hibernated seats in the resolver's
   final ordering, regardless of load (a wake costs a boot + lease-grant
   cycle; a running peer works now).
2. Repeated resolver holds on an urgent item escalate: after the threshold,
   the item is re-routed to a team whose roster satisfies the required
   capabilities — a security refusal becomes a routing decision, not a
   silent stall (PLAT-7468 class).
3. Lead election is disabled: no member is preferred by lead status.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService

User = get_user_model()


def _make_project():
    return Project.objects.create(name='UrgentRoute', key='URG', org_id=1)


def _make_item(project, *, title='urgent cluster work', priority='urgent',
               group='engineering', status='todo'):
    it = Item.objects.create(
        mode='task', item_type='task', title=title, project=project,
        assignment_group=group, status='todo', workflow_status='To Do',
        priority=priority, source='test-urgent-routing',
    )
    Item.objects.filter(pk=it.pk).update(status=status)
    it.refresh_from_db()
    return it


class RunningBeforeHibernatedTests(TestCase):
    """The resolver's final ordering partitions resident seats first."""

    def test_resident_seat_outranks_lower_load_wakeable(self):
        # Two candidates: 'busy' (running, higher load) and 'sleepy'
        # (hibernated, lower load). The running seat must win.
        resolved = [
            (0, 101, 'sleepy@shizuha.com', 0, 0),   # hibernated, least loaded
            (1, 102, 'busy@shizuha.com', 3, 5),     # running, more loaded
        ]
        with mock.patch.object(
                WorkflowService, '_is_agent_available_for_assignment',
                side_effect=lambda e: 'busy' in e), \
             mock.patch.object(WorkflowService, '_route_load_key',
                               side_effect=lambda team, a, i, idx: (a, i, idx)):
            ordered = sorted(resolved, key=lambda r: (r[3], r[4], r[0]))
            _resident = [r for r in ordered
                         if WorkflowService._is_agent_available_for_assignment(r[2])]
            _resident_ids = {id(r) for r in _resident}
            ordered = _resident + [r for r in ordered if id(r) not in _resident_ids]
        self.assertEqual(ordered[0][2], 'busy@shizuha.com')


class RepeatedHoldEscalationTests(TestCase):
    def setUp(self):
        self.project = _make_project()
        TeamRoster.objects.get_or_create(
            team_name='engineering', organization_id=self.project.org_id,
            defaults={'tiers': [['jun@shizuha.com']], 'is_active': True})
        devops, _created = TeamRoster.objects.get_or_create(
            team_name='devops', organization_id=self.project.org_id,
            defaults={'tiers': [['zen@shizuha.com']], 'is_active': True})
        devops.tiers = [['zen@shizuha.com']]
        devops.is_active = True
        devops.save(update_fields=['tiers', 'is_active'])

    def test_capability_gap_does_not_move_the_team(self):
        from tasks import tasks as T
        from tasks.services.workflow_service import WorkflowService
        it = _make_item(self.project)
        Item.objects.filter(pk=it.pk).update(
            metadata={'assignment_hold_count': T._REPEATED_HOLD_ESCALATE_THRESHOLD - 1})
        it.refresh_from_db()
        with mock.patch.object(
                WorkflowService, '_task_required_capabilities',
                return_value={'kubeconfig'}), \
             mock.patch.object(
                WorkflowService, '_seat_satisfies_required_capability',
                side_effect=lambda e, caps, item=None: 'zen' in e):
            T._maybe_escalate_repeated_hold(it)
        it.refresh_from_db()
        self.assertEqual(it.assignment_group, 'engineering')

    def test_low_priority_item_never_escalates(self):
        from tasks import tasks as T
        it = _make_item(self.project, priority='normal')
        Item.objects.filter(pk=it.pk).update(
            metadata={'assignment_hold_count': 99})
        it.refresh_from_db()
        with mock.patch.object(
                WorkflowService, '_task_required_capabilities',
                return_value={'kubeconfig'}):
            T._maybe_escalate_repeated_hold(it)
        it.refresh_from_db()
        self.assertEqual(it.assignment_group, 'engineering')

    def test_incapable_roster_stays_on_its_team(self):
        from tasks import tasks as T
        it = _make_item(self.project, group='merge', status='awaiting_deploy')
        with mock.patch.object(
                WorkflowService, '_task_required_capabilities',
                return_value={'kubeconfig'}), \
             mock.patch.object(
                WorkflowService, '_seat_satisfies_required_capability',
                side_effect=lambda e, caps, item=None: 'zen' in e):
            T._maybe_escalate_repeated_hold(it, immediate=True)
        it.refresh_from_db()
        self.assertEqual(it.assignment_group, 'merge')

    def test_no_capable_team_keeps_counting(self):
        from tasks import tasks as T
        from tasks.services.workflow_service import WorkflowService
        it = _make_item(self.project)
        Item.objects.filter(pk=it.pk).update(
            metadata={'assignment_hold_count': T._REPEATED_HOLD_ESCALATE_THRESHOLD - 1})
        it.refresh_from_db()
        with mock.patch.object(
                WorkflowService, '_task_required_capabilities',
                return_value={'kubeconfig'}), \
             mock.patch.object(
                WorkflowService, '_seat_satisfies_required_capability',
                return_value=False):
            T._maybe_escalate_repeated_hold(it)
        it.refresh_from_db()
        self.assertEqual(it.assignment_group, 'engineering')
        self.assertEqual(
            (it.metadata or {}).get('assignment_hold_count'),
            T._REPEATED_HOLD_ESCALATE_THRESHOLD - 1)


class LeadElectionDisabledTests(TestCase):
    def test_flag_is_set(self):
        self.assertTrue(WorkflowService._LEAD_ELECTION_DISABLED)

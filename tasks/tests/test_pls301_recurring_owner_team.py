"""PLS-301 recurring owner-team marker + safe drift healing."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from tasks import qa_cadence
from tasks.models import (
    Item,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_recurring_owner_team_drift


class RecurringOwnerTeamTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Pulse', key='PLS301', org_id=1)
        for team_name, email in (
            ('qa', 'qa@shizuha.com'),
            ('engineering', 'engineering@shizuha.com'),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'is_active': True,
                },
            )
        self.user = get_user_model().objects.create_user(
            username='pls301', email='pls301@example.com', password='x', is_staff=True,
        )
        self.open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        self.review_status, _ = Status.objects.get_or_create(
            slug='in_review', defaults={'name': 'In Review', 'category': 'in_progress', 'position': 2},
        )
        self.done_status, _ = Status.objects.get_or_create(
            slug='done', defaults={'name': 'Done', 'category': 'done', 'position': 9},
        )
        self.scheduled_status, _ = Status.objects.get_or_create(
            slug='scheduled', defaults={'name': 'Scheduled', 'category': 'scheduled', 'position': 10},
        )
        self.workflow = Workflow.objects.create(
            name='PLS301 Workflow', slug='pls301-workflow', initial_status=self.open_status,
        )
        for pos, status in enumerate((self.open_status, self.review_status, self.done_status, self.scheduled_status)):
            WorkflowStatus.objects.create(workflow=self.workflow, status=status, position=pos)
        self.to_review = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Submit to Engineering Review',
            from_status='open',
            to_status='in_review',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )

    def _item(self, **kw):
        data = dict(
            mode='task', item_type='qa.cadence', title='Recurring QA cadence',
            project=self.project, item_key=f'PLS301-{Item.objects.count() + 1}',
            status='open', workflow_name=self.workflow.name, assignment_group='qa',
            assignee_id=11, assignee_email='qa@shizuha.com', is_recurring=True,
            frequency='daily', due_date=timezone.now() - timedelta(minutes=1),
            labels={'no-pr': 'true'},
            metadata={'recurring_owner_team': 'qa'},
        )
        data.update(kw)
        return Item.objects.create(**data)

    def _is_team_member(self, email, team, item=None):
        return (email or '').lower().startswith(f'{team}@')

    def _resolve_team(self, team, *args, **kwargs):
        return {'qa': (11, 'qa@shizuha.com'), 'engineering': (22, 'engineering@shizuha.com')}.get(
            team, (None, None)
        )

    def test_qa_cadence_fields_stamp_owner_team_marker(self):
        svc = qa_cadence._Svc('Connect', 'CON', 'connect', '- check')
        with mock.patch('tasks.qa_cadence._build_checklist_from_map', return_value=('- mapped', True)):
            fields = qa_cadence.build_task_fields('T1', svc, self.project, timezone.now())
        self.assertEqual(fields['assignment_group'], 'qa')
        self.assertEqual(fields['metadata']['recurring_owner_team'], 'qa')

    def test_recurring_create_stamps_marker_from_initial_assignment_group(self):
        item = Item.objects.create(
            mode='task', title='Daily cadence', project=self.project, status='open',
            workflow_name=self.workflow.name, assignment_group='engineering',
            is_recurring=True, frequency='daily', due_date=timezone.now(),
        )
        self.assertEqual(item.metadata['recurring_owner_team'], 'engineering')

    @mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    @mock.patch.object(WorkflowService, '_resolve_team_assignee')
    @mock.patch.object(WorkflowService, '_is_team_member')
    def test_transition_explicit_assign_team_route_is_honored_for_marked_recurring_driver(self, member, resolve, *_):
        """PLS-918: an explicit `assign_team` post-function is the workflow
        author's deliberate lane route and is TRANSIENT — it must not be vetoed
        by the recurring-owner healer in the same transition batch (which
        previously stranded the destination-lane WIP reservation and raised
        `wip_reservation_target_mismatch` on every cross-lane submit of a
        recurring item). The owner team is restored on the return/park/sweep
        paths, so this is not permanent drift."""
        from tasks.models import Item

        member.side_effect = self._is_team_member
        resolve.side_effect = self._resolve_team
        item = self._item()

        WorkflowService.execute_transition(item, 'in_review', user=self.user, transition_id=str(self.to_review.id))
        item.refresh_from_db()

        self.assertEqual(item.status, 'in_review')
        # The deliberate assign_team route (submitting to the engineering review
        # lane) is honored: the recurring marker does not veto the transition.
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(item.assignee_email, 'engineering@shizuha.com')

    @mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    @mock.patch.object(WorkflowService, '_resolve_team_assignee')
    @mock.patch.object(WorkflowService, '_is_team_member')
    def test_transition_non_assign_team_drift_still_restores_marked_recurring_owner(self, member, resolve, *_):
        """PLS-918 companion: the recurring-owner healer still runs when the
        transition batch does NOT contain an explicit cross-team `assign_team`
        route (generic `assign_to`-style drift / failover). Only the deliberate
        assign_team route is exempted."""
        from tasks.models import Item, WorkflowTransition

        member.side_effect = self._is_team_member
        resolve.side_effect = self._resolve_team
        item = self._item()
        # No assign_team post-function: drift would be unintended.
        drift_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Open to In Review (generic)',
            from_status='open',
            to_status='in_review',
            post_functions=[],
        )
        # Simulate a drifted concrete owner before the generic transition.
        Item.objects.filter(pk=item.pk).update(
            assignment_group='engineering',
            assignee_id=22,
            assignee_email='engineering@shizuha.com',
        )
        item.refresh_from_db()

        WorkflowService.execute_transition(
            item, 'in_review', user=self.user,
            transition_id=str(drift_transition.id),
        )
        item.refresh_from_db()

        self.assertEqual(item.status, 'in_review')
        # No explicit assign_team route -> the healer restored the owner team.
        self.assertEqual(item.assignment_group, 'qa')
        self.assertEqual(item.assignee_email, 'qa@shizuha.com')

    @mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    @mock.patch.object(WorkflowService, '_resolve_team_assignee')
    @mock.patch.object(WorkflowService, '_is_team_member')
    def test_recurring_completion_parks_scheduled_and_restores_owner_team(self, member, resolve, *_):
        member.side_effect = self._is_team_member
        resolve.side_effect = self._resolve_team
        item = self._item(assignment_group='engineering', assignee_id=22, assignee_email='engineering@shizuha.com')

        WorkflowService._park_recurring_scheduled(item, user=self.user)
        item.refresh_from_db()

        self.assertEqual(item.status, 'scheduled')
        self.assertGreater(item.due_date, timezone.now())
        self.assertEqual(item.assignment_group, 'qa')
        self.assertEqual(item.assignee_email, 'qa@shizuha.com')

    @mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    @mock.patch.object(WorkflowService, '_resolve_team_assignee')
    @mock.patch.object(WorkflowService, '_is_team_member')
    def test_residual_healer_repairs_low_risk_and_refuses_ambiguous_active_items(self, member, resolve, *_):
        member.side_effect = self._is_team_member
        resolve.side_effect = self._resolve_team
        future = timezone.now() + timedelta(days=1)
        safe = self._item(
            item_key='PLS301-100', status='open', due_date=future,
            assignment_group='engineering', assignee_id=22, assignee_email='engineering@shizuha.com',
        )
        active = self._item(
            item_key='PLS301-101', status='in_progress', due_date=future,
            assignment_group='engineering', assignee_id=22, assignee_email='engineering@shizuha.com',
        )

        result = sweep_recurring_owner_team_drift.apply(kwargs={'apply': True}).get()
        safe.refresh_from_db()
        active.refresh_from_db()

        self.assertIn('PLS301-100', result['healed'])
        self.assertIn('PLS301-101', result['refused'])
        self.assertEqual(safe.assignment_group, 'qa')
        self.assertEqual(safe.assignee_email, 'qa@shizuha.com')
        self.assertEqual(active.assignment_group, 'engineering')


class RetiredOwnerTeamRerouteTests(RecurringOwnerTeamTests):
    """PLS-989: retiring a team group must reroute its assigned recurring tasks
    to a staffed fallback instead of leaving them permanently unroutable."""

    def setUp(self):
        super().setUp()
        # Retired team: is_active=False AND zero members (the EVOL-65 case).
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='marketing',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [],
                'is_active': False,
                'note': 'RETIRED ROUTING GROUP',
            },
        )

    def test_restore_reroutes_retired_owner_team_to_staffed_fallback(self):
        item = self._item(
            item_key='PLS301-200', assignment_group='marketing',
            assignee_id=None, assignee_email='',
            metadata={'recurring_owner_team': 'marketing'},
            due_date=timezone.now() + timedelta(days=1),
        )
        changed = WorkflowService._restore_recurring_owner_team(item, save=True)
        item.refresh_from_db()
        self.assertTrue(changed)
        # marketing is retired/zero-member → rerouted to a staffed fallback
        # (reporter's group is empty here, so admin-ops is the candidate).
        self.assertNotEqual(item.assignment_group, 'marketing')
        self.assertIn(item.assignment_group, ('admin-ops', 'engineering'))
        self.assertEqual(item.metadata['recurring_owner_team'], item.assignment_group)

    def test_sweep_heals_retired_owner_team_item(self):
        item = self._item(
            item_key='PLS301-201', assignment_group='marketing',
            assignee_id=None, assignee_email='',
            metadata={'recurring_owner_team': 'marketing'},
            due_date=timezone.now() + timedelta(days=1),
        )
        result = sweep_recurring_owner_team_drift.apply(kwargs={'apply': True}).get()
        item.refresh_from_db()
        self.assertIn('PLS301-201', result['healed'])
        self.assertNotEqual(item.assignment_group, 'marketing')
        self.assertIn(item.assignment_group, ('admin-ops', 'engineering'))
        self.assertEqual(item.metadata['recurring_owner_team'], item.assignment_group)

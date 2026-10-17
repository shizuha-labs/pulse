from datetime import timedelta
import importlib
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from tasks.models import (
    Comment,
    Item,
    ItemActivity,
    Project,
    Status,
    TeamRoster,
    WipSlotReservation,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.recurring_cycle_state import complete_cycle
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class _PLS474Directory:
    def __init__(self, users):
        self.by_email = {
            user.email.strip().lower(): {
                'id': user.id,
                'email': user.email.strip().lower(),
                'username': user.username,
            }
            for user in users
        }
        self.by_id = {str(row['id']): row for row in self.by_email.values()}
        self.by_username = {
            row['username'].strip().lower(): row
            for row in self.by_email.values()
        }

    def get_user_by_email(self, email):
        return self.by_email.get(str(email or '').strip().lower())

    def resolve_user_info(self, email=None, user_id=None, **_kwargs):
        if email:
            return self.get_user_by_email(email)
        return self.by_id.get(str(user_id or ''))

    def get_user_by_id(self, user_id):
        return self.by_id.get(str(user_id or ''))

    def get_user_by_username(self, username):
        return self.by_username.get(str(username or '').strip().lower())


class ReviewedTaskWorkflowTests(TestCase):
    def test_reviewed_task_workflow_is_seeded_with_review_gate(self):
        workflow = Workflow.objects.get(slug='reviewed-task')

        self.assertEqual(workflow.name, 'Reviewed Task')
        self.assertEqual(workflow.get_initial_status_slug(), 'todo')
        self.assertEqual(
            workflow.get_status_slugs(),
            [
                'todo',
                'in_progress',
                'in_review',
                'blocked',
                'done',
                'cancelled',
                'accepted_capacity_held',
                'implementation_active',
                'scheduled',
            ],
        )

        submit = WorkflowTransition.objects.get(
            workflow=workflow,
            name='Submit for Review',
            from_status='in_progress',
            to_status='in_review',
        )
        self.assertEqual(
            submit.post_functions,
            [
                {'type': 'stash_implementer'},
                {'type': 'assign_team', 'value': 'review'},
                {'type': 'stamp_review_started'},
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'watchers'],
                },
                {
                    'type': 'publication_handoff',
                    'required_label': 'publication-binding-required',
                },
            ],
        )

        request_changes = WorkflowTransition.objects.get(
            workflow=workflow,
            name='Request Changes',
            from_status='in_review',
            to_status='in_progress',
        )
        self.assertIn({'type': 'assign_implementer'}, request_changes.post_functions)
        self.assertFalse(
            WorkflowTransition.objects.filter(
                workflow=workflow,
                name='Escalate to DevOps',
                from_status='blocked',
                to_status='todo',
            ).exists()
        )

    def test_reviewed_task_recurring_cycle_parks_in_scheduled(self):
        future_due = timezone.now() + timedelta(days=12)
        item = Item.objects.create(
            title='reviewed recurring cycle',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            is_recurring=True,
            frequency='monthly',
            due_date=future_due,
            metadata={'recurring_cycle': {
                'cycle_revision': 4,
                'active_generation': 2,
                'completed_generation': 1,
                'consecutive_skips': 3,
                'escalated_at_skips': 3,
                'last_terminal_completion_at': '2026-01-01T00:00:00+00:00',
            }},
        )

        WorkflowService._park_recurring_scheduled(item)
        item.refresh_from_db()

        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.workflow_status, 'Scheduled')
        self.assertIsNone(item.completed_at)
        self.assertEqual(item.due_date, future_due)
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(cycle['cycle_revision'], 5)
        self.assertEqual(cycle['completed_generation'], 2)
        self.assertEqual(cycle['consecutive_skips'], 0)
        self.assertEqual(cycle['escalated_at_skips'], 0)
        self.assertNotEqual(cycle['last_terminal_completion_at'], '2026-01-01T00:00:00+00:00')

        # A duplicate terminal hook is an idempotent no-op, including boundary.
        WorkflowService._park_recurring_scheduled(item)
        item.refresh_from_db()
        self.assertEqual(item.due_date, future_due)
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], 5)

    def test_generic_run_now_uses_same_revision_guard_as_dedicated_endpoint(self):
        user = get_user_model().objects.create_user(
            username='run-now-owner', email='run-now-owner@shizuha.com', password='x',
        )
        project = Project.objects.create(
            name='Reviewed Run Now', key='RRUN', org_id=1,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={
                'organization_id': 1,
                'tiers': [[user.email]],
                'is_active': True,
            },
        )
        workflow = Workflow.objects.get(slug='reviewed-task')
        run_now = WorkflowTransition.objects.get(
            workflow=workflow, from_status='scheduled', name='Run Now',
        )
        due = timezone.now() + timedelta(days=7)
        item = Item.objects.create(
            project=project,
            title='generic guarded run now', mode='task',
            workflow_name='reviewed-task', status='scheduled',
            workflow_status='Scheduled', is_recurring=True, frequency='weekly',
            assignment_group='engineering',
            due_date=due, assignee_id=user.id, assignee_email=user.email,
            metadata={'recurring_cycle': {
                'cycle_revision': 9,
                'active_generation': 2,
                'completed_generation': 2,
            }},
        )

        with self.assertRaisesRegex(WorkflowValidationError, 'expected_cycle_revision'):
            WorkflowService.execute_transition(
                item, 'todo', user=user, screen_data={}, transition_id=run_now.id,
            )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')

        WorkflowService.execute_transition(
            item,
            'todo',
            user=user,
            screen_data={
                'expected_cycle_revision': 9,
                'expected_generation': 2,
                'expected_due_at': due.isoformat(),
                'recurring_event_id': 'generic-run-now-revision-9',
            },
            transition_id=run_now.id,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'todo')
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], 10)
        self.assertEqual(item.metadata['recurring_cycle']['active_generation'], 3)

    def test_generic_run_now_returns_structured_conflict_for_stale_cycle(self):
        user = get_user_model().objects.create_user(
            username='stale-run-now-owner', email='stale-run-now-owner@shizuha.com', password='x',
        )
        workflow = Workflow.objects.get(slug='reviewed-task')
        run_now = WorkflowTransition.objects.get(
            workflow=workflow, from_status='scheduled', name='Run Now',
        )
        due = timezone.now() + timedelta(days=7)
        item = Item.objects.create(
            title='stale generic run now', mode='task',
            workflow_name='reviewed-task', status='in_progress',
            workflow_status='In Progress', is_recurring=True, frequency='weekly',
            due_date=due, assignee_id=user.id, assignee_email=user.email,
            metadata={'recurring_cycle': {
                'cycle_revision': 9,
                'active_generation': 3,
                'completed_generation': 2,
                'last_terminal_completion_at': '2026-07-01T00:00:00+00:00',
            }},
        )
        stale_payload = {
            'expected_cycle_revision': 9,
            'expected_generation': 3,
            'expected_due_at': due.isoformat(),
            'expected_completion_watermark': '2026-07-01T00:00:00+00:00',
            'recurring_event_id': 'stale-assignment-generation-3',
        }
        completed = complete_cycle(item.pk, actor_id=user.id, actor_email=user.email)
        self.assertTrue(completed.applied)

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            f'/api/items/{item.pk}/execute-transition/',
            {
                'to_status': 'todo',
                'transition_id': str(run_now.id),
                'screen_data': stale_payload,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'stale_recurring_cycle')
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.due_date, due)
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], 10)

    def test_runtime_backstop_attaches_scheduled_to_workflow_before_parking(self):
        workflow = Workflow.objects.get(slug='reviewed-task')
        scheduled = Status.objects.get(slug='scheduled')
        workflow.workflow_statuses.filter(status=scheduled).delete()
        WorkflowTransition.objects.filter(
            workflow=workflow,
            from_status='scheduled',
            to_status='todo',
        ).delete()
        item = Item.objects.create(
            title='reviewed recurring gap missing',
            mode='task',
            workflow_name='reviewed-task',
            status='done',
            is_recurring=True,
            frequency='monthly',
            due_date=timezone.now() - timedelta(days=1),
        )

        WorkflowService._park_recurring_scheduled(item)
        item.refresh_from_db()

        self.assertEqual(item.status, 'scheduled')
        self.assertTrue(workflow.workflow_statuses.filter(status=scheduled).exists())
        self.assertTrue(
            WorkflowTransition.objects.filter(
                workflow=workflow,
                from_status='scheduled',
                to_status='todo',
                name='Run Now',
            ).exists()
        )

    def test_reviewed_task_is_intrinsically_reviewable_without_pr_or_no_pr_label(self):
        item = SimpleNamespace(
            workflow_name='reviewed-task',
            metadata={},
            labels=[],
            tags=[],
        )

        self.assertTrue(WorkflowService._is_reviewable(item))

    def test_regular_task_without_pr_or_no_pr_label_is_not_reviewable(self):
        item = SimpleNamespace(
            workflow_name='simple',
            metadata={},
            labels=[],
            tags=[],
        )

        self.assertFalse(WorkflowService._is_reviewable(item))


class PLS474ImplementationReturnTests(TestCase):
    """Production-order review-return regressions for the shared engine path."""

    def setUp(self):
        self.impl = get_user_model().objects.create_user(
            username='pls474-impl', email='pls474-impl@shizuha.com', password='x',
        )
        self.fallback = get_user_model().objects.create_user(
            username='pls474-fallback', email='pls474-fallback@shizuha.com', password='x',
        )
        self.reviewer = get_user_model().objects.create_user(
            username='pls474-reviewer', email='pls474-reviewer@shizuha.com', password='x',
        )
        self.old_reviewer = get_user_model().objects.create_user(
            username='pls474-old-reviewer',
            email='pls474-old-reviewer@shizuha.com',
            password='x',
        )
        self.merger = get_user_model().objects.create_user(
            username='pls474-merger', email='pls474-merger@shizuha.com', password='x',
        )
        self.qa = get_user_model().objects.create_user(
            username='pls474-qa', email='pls474-qa@shizuha.com', password='x',
        )
        self.directory = _PLS474Directory(
            [
                self.impl,
                self.fallback,
                self.reviewer,
                self.old_reviewer,
                self.merger,
                self.qa,
            ]
        )
        self.project = Project.objects.create(
            name='PLS-474 return routing',
            key='PLS474',
            org_id=1,
        )
        self._set_roster('engineering', [self.impl.email, self.fallback.email])
        self._set_roster('review', [self.reviewer.email])
        self._set_roster('merge', [self.merger.email])
        self._set_roster('qa', [self.qa.email])

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _set_roster(self, team, emails):
        TeamRoster.objects.update_or_create(
            team_name=team,
            organization_slug='shizuha',
            organization_id=1,
            defaults={'tiers': [list(emails)], 'is_active': True},
        )
        WorkflowService._invalidate_team_roster_caches()

    def _runtime_patches(self, *, availability=None):
        availability = availability or (lambda _email: True)
        return (
            patch('tasks.user_service.user_service', self.directory),
            patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                side_effect=availability,
            ),
            patch.object(
                WorkflowService, '_member_can_execute_from_status', return_value=True,
            ),
            patch.object(WorkflowService, '_is_human_account', return_value=False),
            patch.object(WorkflowService, '_notify_routability_hold', return_value=True),
        )

    def _execute_with_runtime(self, item, transition, actor, *, screen_data=None):
        patches = self._runtime_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return WorkflowService.execute_transition(
                item,
                transition.to_status,
                user=actor,
                screen_data=screen_data or {},
                transition_id=transition.id,
            )

    def _reviewed_transitions(self):
        workflow = Workflow.objects.get(slug='reviewed-task')
        submit = WorkflowTransition.objects.get(
            workflow=workflow,
            name='Submit for Review',
            from_status='in_progress',
            to_status='in_review',
        )
        request_changes = WorkflowTransition.objects.get(
            workflow=workflow,
            name='Request Changes',
            from_status='in_review',
            to_status='in_progress',
        )
        return submit, request_changes

    def test_two_rounds_commit_one_final_tuple_and_fenced_notifications(self):
        submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 two-round return',
            mode='task',
            workflow_name='reviewed-task',
            status='in_progress',
            workflow_status='in_progress',
            assignee_id=self.impl.id,
            assignee_email=self.impl.email,
            assignment_group='engineering',
        )

        self._execute_with_runtime(item, submit, self.impl)
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.reviewer.email)
        context = item.metadata['implementation_return_context']
        self.assertEqual(context['implementation_team'], 'engineering')
        self.assertEqual(context['review_generation'], 1)
        self.assertNotIn('implementer', item.metadata)

        before_activity = ItemActivity.objects.filter(item=item).count()
        with patch('tasks.tasks.send_item_notification.delay') as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self._execute_with_runtime(
                    item,
                    request_changes,
                    self.reviewer,
                    screen_data={'comment': 'round one changes'},
                )
                dispatch.assert_not_called()

        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_progress', 'engineering', self.impl.email),
        )
        new_activity = ItemActivity.objects.filter(item=item).order_by('created_at')[
            before_activity:
        ]
        self.assertEqual(
            [row.action for row in new_activity if row.field_name == 'Status'],
            ['status_changed'],
        )
        self.assertEqual(
            [row.action for row in new_activity if row.field_name == 'Assignee'],
            ['assigned'],
        )

        dispatched = [call.args for call in dispatch.call_args_list]
        status_events = [args for args in dispatched if args[1] == 'status_changed']
        assignment_events = [args for args in dispatched if args[1] == 'assignment_changed']
        self.assertEqual(len(status_events), 1)
        self.assertEqual(len(assignment_events), 1)
        for _item_id, _event, payload in status_events + assignment_events:
            self.assertEqual(payload['dispatched_assignee_id'], str(self.impl.id))
            self.assertEqual(payload['dispatched_assignment_group'], 'engineering')
            self.assertEqual(payload['dispatched_return_generation'], 1)
            self.assertNotEqual(
                payload['dispatched_assignee_id'], str(self.reviewer.id)
            )

        comment = Comment.objects.filter(
            item=item,
            origin_event='assignment.review_changes_requested',
            origin_payload__return_generation=1,
        ).latest('created_at')
        self.assertIn(self.impl.email, comment.content)
        self.assertNotIn(self.reviewer.email, comment.content)

        # Production caller order for a second review round: normal submit
        # stamps a new generation, then Request Changes consumes that current
        # generation.  The same owner/status must not make round-one delivery
        # look current.
        self._execute_with_runtime(item, submit, self.impl)
        item.refresh_from_db()
        self.assertEqual(
            item.metadata['implementation_return_context']['review_generation'], 2
        )
        with self.captureOnCommitCallbacks(execute=True):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'round two changes'},
            )
        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_progress', 'engineering', self.impl.email),
        )
        self.assertTrue(Comment.objects.filter(
            item=item,
            content__contains='return generation 2',
        ).exists())
        self.assertFalse(WipSlotReservation.objects.filter(item=item).exists())
        self.assertFalse(hasattr(item, '_wip_reservation_token'))

    def test_active_origin_branch_owner_wins_across_two_top_level_rounds(self):
        """PLS-568 + PLS-474: caller order repeats without split routing.

        Production sequence exercised twice: implementation owner submits,
        Review requests changes, the active Origin author receives the exact
        branch, then that author re-submits and receives round two. The branch
        author is intentionally outside the recorded implementation team so the
        fixture proves active-PR precedence and versioned team/generation
        preservation at the real ``execute_transition`` boundary.
        """
        submit, request_changes = self._reviewed_transitions()
        pr_url = (
            'https://origin.shizuha.com/shizuha-labs/'
            'shizuha-tasks/pulls/568474'
        )
        item = Item.objects.create(
            title='PLS-568/474 two-round branch return',
            mode='task',
            project=self.project,
            workflow_name='reviewed-task',
            status='in_progress',
            workflow_status='in_progress',
            assignee_id=self.impl.id,
            assignee_email=self.impl.email,
            assignment_group='engineering',
            metadata={'pull_requests': [{
                'url': pr_url,
                'status': 'open',
                'head_sha': '568474a',
            }]},
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'user': {'login': self.qa.username}},
        )

        with patch('requests.get', return_value=response), patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ), patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'test-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=False):
            self._execute_with_runtime(item, submit, self.impl)
            with self.captureOnCommitCallbacks(execute=True):
                self._execute_with_runtime(
                    item,
                    request_changes,
                    self.reviewer,
                    screen_data={'comment': 'round one branch changes'},
                )

            item.refresh_from_db()
            self.assertEqual(
                (item.status, item.assignment_group, item.assignee_email),
                ('in_progress', None, self.qa.email),
            )
            self.assertEqual(
                item.metadata['implementation_return_context'][
                    'implementation_team'
                ],
                'engineering',
            )
            self.assertEqual(
                item.metadata['implementation_return_context'][
                    'review_generation'
                ],
                1,
            )
            self.assertEqual(
                item.metadata['branch_return_routing']['state'],
                'branch_owner',
            )

            self._execute_with_runtime(item, submit, self.qa)
            item.refresh_from_db()
            self.assertEqual(
                item.metadata['implementation_return_context'][
                    'implementation_team'
                ],
                'engineering',
            )
            self.assertEqual(
                item.metadata['implementation_return_context'][
                    'review_generation'
                ],
                2,
            )
            with self.captureOnCommitCallbacks(execute=True):
                self._execute_with_runtime(
                    item,
                    request_changes,
                    self.reviewer,
                    screen_data={'comment': 'round two branch changes'},
                )

        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_progress', None, self.qa.email),
        )
        self.assertEqual(
            item.metadata['implementation_return_context']['review_generation'],
            2,
        )
        self.assertFalse(WipSlotReservation.objects.filter(item=item).exists())
        self.assertFalse(hasattr(item, '_wip_reservation_token'))

    def test_legacy_qa_owner_promotes_and_returns_without_http_500(self):
        """SCS-93/CON-236 live shape: old review rows recover their real lane."""
        _submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            title='Legacy QA review return',
            mode='task',
            project=self.project,
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            assignment_group='review',
            metadata={'implementer': {
                'id': self.qa.id,
                'email': self.qa.email,
            }},
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'live QA P1/P2 verdict'},
            )

        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_progress', 'qa', self.qa.email),
        )
        context = item.metadata['implementation_return_context']
        self.assertEqual(context['implementation_team'], 'qa')
        self.assertEqual(context['implementer']['email'], self.qa.email)
        self.assertEqual(context['review_generation'], 1)
        self.assertNotIn('implementer', item.metadata)
        self.assertFalse(WipSlotReservation.objects.filter(item=item).exists())
        self.assertFalse(hasattr(item, '_wip_reservation_token'))

    def test_ineligible_recorded_owner_uses_team_candidate_and_comments_committed_identity(self):
        _submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 return fallback',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            assignment_group='review',
            metadata={'implementation_return_context': {
                'version': 1,
                'implementer': {'id': self.impl.id, 'email': self.impl.email},
                'implementation_team': 'engineering',
                'review_generation': 7,
            }},
        )
        availability = lambda email: email != self.impl.email
        patches = self._runtime_patches(availability=availability)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    user=self.reviewer,
                    screen_data={'comment': 'use current team candidate'},
                    transition_id=request_changes.id,
                )

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.fallback.email)
        comment = Comment.objects.filter(
            item=item,
            content__contains='return generation 7',
        ).latest('created_at')
        self.assertIn(self.fallback.email, comment.content)
        self.assertNotIn(self.impl.email, comment.content)
        self.assertNotIn(self.reviewer.email, comment.content)

    def test_no_candidate_is_typed_group_hold_never_engineering_fallback(self):
        _submit, request_changes = self._reviewed_transitions()
        self._set_roster('performance', [self.impl.email])
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 typed return hold',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            assignment_group='review',
            metadata={'implementation_return_context': {
                'version': 1,
                'implementer': {'id': self.impl.id, 'email': self.impl.email},
                'implementation_team': 'performance',
                'review_generation': 3,
            }},
        )
        patches = self._runtime_patches(availability=lambda _email: False)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    user=self.reviewer,
                    screen_data={'comment': 'no return seat'},
                    transition_id=request_changes.id,
                )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertEqual(item.assignment_group, 'performance')
        self.assertIsNone(item.assignee_id)
        self.assertEqual(item.assignee_email, '')
        self.assertEqual(
            item.metadata['implementation_return_hold']['code'],
            'no_routable_return_candidate',
        )
        self.assertFalse(
            ItemActivity.objects.filter(
                item=item,
                action='assigned',
                new_value__icontains=self.reviewer.email,
            ).exists()
        )
        self.assertTrue(Comment.objects.filter(
            item=item,
            content__contains='PLS-278 routability hold',
        ).exists())

    def test_stale_unblock_generation_discards_captured_reviewer(self):
        self._set_roster(
            'review', [self.old_reviewer.email, self.reviewer.email]
        )
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 stale unblock fence',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.old_reviewer.id,
            assignee_email=self.old_reviewer.email,
            assignment_group='review',
        )
        blocked_metadata = {
                'implementation_return_context': {
                    'version': 1,
                    'implementer': {'id': self.impl.id, 'email': self.impl.email},
                    'implementation_team': 'engineering',
                    'review_generation': 2,
                },
                'pre_block_state': {
                    'status': 'in_review',
                    'workflow_status': 'in_review',
                    'assignee_id': self.old_reviewer.id,
                    'assignee_email': self.old_reviewer.email,
                    'assignment_group': 'review',
                    'percent_complete': 50,
                    'had_active_blocker': True,
                    'implementation_return_generation': 1,
                },
            }
        # Model creation correctly forbids materializing `blocked` directly.
        # This test is specifically a delayed unblock callback fixture, so seed
        # the already-authorized blocked snapshot without exercising block-entry
        # policy (covered by PLAT-4215 tests).
        Item.objects.filter(pk=item.pk).update(
            status='blocked',
            workflow_status='blocked',
            metadata=blocked_metadata,
        )
        item.refresh_from_db()
        patches = self._runtime_patches()
        active_load = (
            lambda _uid, email: 10 if email == self.old_reviewer.email else 0
        )
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(WorkflowService, '_agent_active_load', side_effect=active_load),
            patch.object(WorkflowService, '_agent_workable_load', side_effect=active_load),
            self.captureOnCommitCallbacks(execute=True),
        ):
            restored = WorkflowService._restore_blocked_item(
                item,
                blocker=SimpleNamespace(item_key='PLS-474-BLOCKER'),
                user=self.reviewer,
            )

        self.assertTrue(restored)
        item.refresh_from_db()
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'review')
        self.assertEqual(item.assignee_email, self.reviewer.email)
        self.assertEqual(
            item.metadata['implementation_return_context']['review_generation'], 2
        )

    def test_delayed_event_with_same_owner_status_is_stale_after_new_generation(self):
        from tasks.tasks import _notification_tuple_staleness

        item = Item.objects.create(
            project=self.project,
            title='PLS-474 delayed generation event',
            mode='task',
            workflow_name='reviewed-task',
            status='in_progress',
            workflow_status='in_progress',
            assignee_id=self.impl.id,
            assignee_email=self.impl.email,
            assignment_group='engineering',
            metadata={'implementation_return_context': {
                'version': 1,
                'implementer': {'id': self.impl.id, 'email': self.impl.email},
                'implementation_team': 'engineering',
                'review_generation': 2,
            }},
        )
        reason = _notification_tuple_staleness(item, {
            'dispatched_assignee_id': str(self.impl.id),
            'dispatched_assignee_email': self.impl.email,
            'dispatched_assignment_group': 'engineering',
            'dispatched_return_generation': 1,
        })
        self.assertEqual(reason, 'return generation changed from 1 to 2')

    def test_generation_watermark_survives_invalid_submit_hold_and_repair(self):
        from tasks.tasks import _notification_tuple_staleness

        submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 monotonic return watermark',
            mode='task',
            workflow_name='reviewed-task',
            status='in_progress',
            workflow_status='in_progress',
            assignee_id=self.impl.id,
            assignee_email=self.impl.email,
            assignment_group='engineering',
        )

        # Production order: a valid first review/return creates generation 1.
        self._execute_with_runtime(item, submit, self.impl)
        with self.captureOnCommitCallbacks(execute=True):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'generation one changes'},
            )
        item.refresh_from_db()
        self.assertEqual(item.metadata['implementation_return_generation'], 1)
        old_event = {
            'dispatched_assignee_id': str(self.impl.id),
            'dispatched_assignee_email': self.impl.email,
            'dispatched_assignment_group': 'engineering',
            'dispatched_return_generation': 1,
        }

        # A later invalid submit advances to generation 2 and removes the armed
        # context. PLS-796: Request Changes without context must still land on
        # the engineering default (ladder step 4c) rather than a naked
        # missing_return_context hold, while preserving the generation watermark.
        item.assignment_group = 'review'
        item.assignee_id = self.reviewer.id
        item.assignee_email = self.reviewer.email
        item.save(update_fields=['assignment_group', 'assignee_id', 'assignee_email'])
        self._execute_with_runtime(item, submit, self.reviewer)
        item.refresh_from_db()
        self.assertNotIn('implementation_return_context', item.metadata)
        self.assertEqual(item.metadata['implementation_return_generation'], 2)
        self.assertEqual(
            item.metadata['implementation_return_hold']['code'],
            'invalid_submit_owner',
        )
        with patch('tasks.tasks.send_item_notification.delay') as delayed_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self._execute_with_runtime(
                    item,
                    request_changes,
                    self.reviewer,
                    screen_data={'comment': 'missing context return'},
                )
        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_progress', 'engineering', self.impl.email),
        )
        self.assertNotIn('implementation_return_hold', item.metadata)
        self.assertNotIn('implementation_return_context', item.metadata)
        self.assertEqual(item.metadata['implementation_return_generation'], 2)
        held_status_payloads = [
            call.args[2]
            for call in delayed_dispatch.call_args_list
            if call.args[1] == 'status_changed'
        ]
        self.assertEqual(len(held_status_payloads), 1)
        held_event = held_status_payloads[0]
        self.assertEqual(held_event['dispatched_return_generation'], 2)

        # Repeat the same invalid submit/return path before repairing it. Both
        # returns finish on the same engineering ownership tuple, so only the
        # generation watermark can fence the delayed round-two event.
        item.assignment_group = 'review'
        item.assignee_id = self.reviewer.id
        item.assignee_email = self.reviewer.email
        item.save(update_fields=['assignment_group', 'assignee_id', 'assignee_email'])
        self._execute_with_runtime(item, submit, self.reviewer)
        with self.captureOnCommitCallbacks(execute=True):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'second missing context return'},
            )
        item.refresh_from_db()
        self.assertEqual(item.metadata['implementation_return_generation'], 3)
        self.assertEqual(
            _notification_tuple_staleness(item, held_event),
            'return generation changed from 2 to 3',
        )

        # Repair ownership, then run the real submit/return sequence again. The
        # new context must be generation 4, and the original generation-1 event
        # remains stale even though status/team/owner match its old final tuple.
        item.assignment_group = 'engineering'
        item.assignee_id = self.impl.id
        item.assignee_email = self.impl.email
        item.save(update_fields=['assignment_group', 'assignee_id', 'assignee_email'])
        self._execute_with_runtime(item, submit, self.impl)
        item.refresh_from_db()
        self.assertEqual(
            item.metadata['implementation_return_context']['review_generation'], 4
        )
        self.assertEqual(item.metadata['implementation_return_generation'], 4)
        with self.captureOnCommitCallbacks(execute=True):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'generation three changes'},
            )
        item.refresh_from_db()
        self.assertEqual(
            _notification_tuple_staleness(item, old_event),
            'return generation changed from 1 to 4',
        )

    def test_return_audit_failure_rolls_back_return_tuple(self):
        """PLS-478: return ownership and its typed audit commit atomically."""
        from tasks.services.mechanical_comments import (
            MechanicalCommentError,
            MechanicalCommentFactory,
        )

        submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='PLS-568 robust return audit',
            mode='task',
            workflow_name='reviewed-task',
            status='in_progress',
            workflow_status='in_progress',
            assignee_id=self.impl.id,
            assignee_email=self.impl.email,
            assignment_group='engineering',
        )
        self._execute_with_runtime(item, submit, self.impl)
        with (
            patch.object(
                MechanicalCommentFactory,
                'create',
                side_effect=MechanicalCommentError('synthetic typed-audit failure'),
            ),
            self.assertRaises(MechanicalCommentError),
        ):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'return despite audit outage'},
            )

        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_review', 'review', self.reviewer.email),
        )
        self.assertEqual(item.metadata['implementation_return_generation'], 1)

    def test_missing_original_return_context_defaults_to_engineering(self):
        """PLS-796 4c: no context/assign_team → engineering, never naked.

        Pre-PLS-796 this path typed-held ``missing_return_context`` with a
        cleared ownership tuple. The durable ladder now defaults to the
        engineering group-queue so Request Changes cannot trip PLAT-4215 on a
        blocked dependent. Genuine naked fail-closed is covered by PLS-577 E
        (ladder mocked empty).
        """
        _submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 direct missing return context',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            assignment_group='review',
            metadata={},
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._execute_with_runtime(
                item,
                request_changes,
                self.reviewer,
                screen_data={'comment': 'missing original context'},
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(item.assignee_email, self.impl.email)
        self.assertEqual(item.assignee_id, self.impl.id)
        self.assertNotIn('implementation_return_hold', item.metadata)
        self.assertNotIn('implementation_return_context', item.metadata)

    def test_legacy_no_pr_qa_return_applies_destination_lane_before_owner(self):
        """SCS-93: the real caller returns legacy QA work without a 500.

        Production order is the top-level reviewed-task transition, not a
        helper-only replay: ``in_review / review / reviewer`` -> Request
        Changes -> ``assign_implementer``.  The legacy item has no linked PR or
        versioned PLS-474 context; its durable focused-QA marker proves the
        original QA lane and its legacy implementer is the QA owner.
        """
        _submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='SCS-93 legacy no-PR QA return',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            assignment_group='review',
            metadata={
                'implementer': {'id': self.qa.id, 'email': self.qa.email},
                'focused_qa_assignment_group': 'qa',
            },
        )

        before_activity = ItemActivity.objects.filter(item=item).count()
        with patch('tasks.tasks.send_item_notification.delay') as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self._execute_with_runtime(
                    item,
                    request_changes,
                    self.reviewer,
                    screen_data={'comment': 'SCS-93 changes required'},
                )

        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group, item.assignee_email),
            ('in_progress', 'qa', self.qa.email),
        )
        self.assertEqual(item.assignee_id, self.qa.id)
        self.assertNotIn('implementer', item.metadata)
        self.assertEqual(
            item.metadata['implementation_return_context'],
            {
                'version': 1,
                'implementer': {'id': self.qa.id, 'email': self.qa.email},
                'implementation_team': 'qa',
                'review_generation': 1,
            },
        )
        self.assertNotIn('implementation_return_hold', item.metadata)

        new_activity = ItemActivity.objects.filter(item=item).order_by('created_at')[
            before_activity:
        ]
        self.assertEqual(
            [row.action for row in new_activity if row.field_name == 'Status'],
            ['status_changed'],
        )
        self.assertEqual(
            [row.action for row in new_activity if row.field_name == 'Assignee'],
            ['assigned'],
        )
        events = [call.args for call in dispatch.call_args_list]
        self.assertEqual(
            len([args for args in events if args[1] == 'status_changed']), 1,
        )
        self.assertEqual(
            len([args for args in events if args[1] == 'assignment_changed']), 1,
        )
        for _item_id, _event, payload in events:
            if _event in {'status_changed', 'assignment_changed'}:
                self.assertEqual(payload['dispatched_assignment_group'], 'qa')
                self.assertEqual(payload['dispatched_assignee_id'], str(self.qa.id))
                self.assertEqual(payload['dispatched_return_generation'], 1)

    def test_top_level_return_with_multiple_origin_authors_is_typed_team_hold(self):
        """PLS-568/474 overlap repeats without random owner or stale WIP."""
        submit, request_changes = self._reviewed_transitions()
        item = Item.objects.create(
            project=self.project,
            title='PLS-568 ambiguous branch owners',
            mode='task',
            workflow_name='reviewed-task',
            status='in_review',
            workflow_status='in_review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            assignment_group='review',
            metadata={
                'implementation_return_context': {
                    'version': 1,
                    'implementer': {
                        'id': self.impl.id,
                        'email': self.impl.email,
                    },
                    'implementation_team': 'engineering',
                    'review_generation': 4,
                },
                'pull_requests': [
                    {
                        'url': 'https://origin.shizuha.com/shizuha-labs/repo-a/pulls/1',
                        'status': 'open',
                        'head_sha': 'head-a',
                    },
                    {
                        'url': 'https://origin.shizuha.com/shizuha-labs/repo-b/pulls/2',
                        'status': 'open',
                        'head_sha': 'head-b',
                    },
                ],
            },
        )

        def forgejo_response(url, **_kwargs):
            login = 'sara' if 'repo-a' in url else 'ichi'
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {'user': {'login': login}},
            )

        patches = self._runtime_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch('requests.get', side_effect=forgejo_response),
            patch.dict(os.environ, {
                'FORGEJO_TOKEN': 'test-token',
                'ORIGIN_URL': 'https://origin.shizuha.com',
            }, clear=False),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService.execute_transition(
                    item,
                    request_changes.to_status,
                    user=self.reviewer,
                    screen_data={'comment': 'multiple branch owners round one'},
                    transition_id=request_changes.id,
                )

            item.refresh_from_db()
            self.assertEqual(
                (item.status, item.assignment_group),
                ('in_progress', 'engineering'),
            )
            self.assertIsNone(item.assignee_id)
            self.assertIn(item.assignee_email, ('', None))
            self.assertFalse(WipSlotReservation.objects.filter(item=item).exists())
            self.assertFalse(hasattr(item, '_wip_reservation_token'))

            # Production re-arm: an implementation-team member claims the typed
            # hold, submits again, and the same ambiguous active branches return
            # through the top-level executor a second time.
            item.assignee_id = self.impl.id
            item.assignee_email = self.impl.email
            item.save(update_fields=['assignee_id', 'assignee_email'])
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService.execute_transition(
                    item,
                    submit.to_status,
                    user=self.impl,
                    transition_id=submit.id,
                )
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService.execute_transition(
                    item,
                    request_changes.to_status,
                    user=self.reviewer,
                    screen_data={'comment': 'multiple branch owners round two'},
                    transition_id=request_changes.id,
                )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertIsNone(item.assignee_id)
        self.assertIn(item.assignee_email, ('', None))
        self.assertEqual(
            item.metadata['branch_return_routing']['reason_code'],
            'ambiguous_origin_branch_owners',
        )
        self.assertEqual(
            item.metadata['implementation_return_hold'],
            {
                'code': 'ambiguous_origin_branch_owners',
                'implementation_team': 'engineering',
                'review_generation': 5,
            },
        )
        self.assertFalse(WipSlotReservation.objects.filter(item=item).exists())
        self.assertFalse(hasattr(item, '_wip_reservation_token'))

    def _autonomous_workflow(self, slug):
        in_progress = Status.objects.get(slug='in_progress')
        workflow, _ = Workflow.objects.get_or_create(
            slug=slug,
            defaults={
                'name': slug.replace('-', ' ').title(),
                'initial_status': in_progress,
                'is_system': True,
            },
        )
        for position, status_slug in enumerate(
            ('in_progress', 'in_review', 'in_qa', 'awaiting_merge'), start=1,
        ):
            status, _ = Status.objects.get_or_create(
                slug=status_slug,
                defaults={
                    'name': status_slug.replace('_', ' ').title(),
                    'category': 'in_progress',
                    'position': position,
                    'is_system': True,
                },
            )
            WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=status,
                defaults={'position': position},
            )
        return workflow

    def test_shared_config_and_engine_cover_autonomous_merge_and_qa_returns(self):
        bug = self._autonomous_workflow('autonomous-bug')
        dev = self._autonomous_workflow('autonomous-dev')
        # PLS-796 seeds these edges on autonomous-* workflows; unique_together
        # (workflow, from_status, name) means create() collides. Reuse the
        # seeded row and reset post_functions to the pre-normalize shape this
        # test exercises.
        qa_failed, _ = WorkflowTransition.objects.update_or_create(
            workflow=bug,
            name='QA Failed',
            from_status='in_qa',
            defaults={
                'to_status': 'in_progress',
                'post_functions': [
                    {'type': 'assign_team', 'value': 'engineering'},
                    {'type': 'add_comment', 'body': 'QA failed'},
                ],
            },
        )
        return_changes, _ = WorkflowTransition.objects.update_or_create(
            workflow=dev,
            name='Return for Changes',
            from_status='awaiting_merge',
            defaults={
                'to_status': 'in_progress',
                'post_functions': [
                    {'type': 'assign_team', 'value': 'engineering'},
                    {'type': 'send_notification', 'template': 'status_changed'},
                ],
            },
        )
        migration = importlib.import_module(
            'tasks.migrations.0119_pls474_versioned_implementation_return'
        )
        migration.normalize_implementation_return_edges(django_apps, None)
        qa_failed.refresh_from_db()
        return_changes.refresh_from_db()
        for transition in (qa_failed, return_changes):
            types = [pf['type'] for pf in transition.post_functions]
            self.assertEqual(types.count('assign_implementer'), 1)
            self.assertNotIn('assign_team', types)

        cases = (
            (bug, qa_failed, self.qa, 'in_qa'),
            (dev, return_changes, self.merger, 'awaiting_merge'),
        )
        for workflow, transition, lane_owner, source_status in cases:
            item = Item.objects.create(
                project=self.project,
                title=f'PLS-474 {workflow.slug} return',
                mode='task',
                workflow_name=workflow.slug,
                status=source_status,
                workflow_status=source_status,
                assignee_id=lane_owner.id,
                assignee_email=lane_owner.email,
                assignment_group='qa' if source_status == 'in_qa' else 'merge',
                metadata={'implementation_return_context': {
                    'version': 1,
                    'implementer': {'id': self.impl.id, 'email': self.impl.email},
                    'implementation_team': 'engineering',
                    'review_generation': 4,
                }},
            )
            with self.captureOnCommitCallbacks(execute=True):
                self._execute_with_runtime(
                    item,
                    transition,
                    lane_owner,
                    screen_data={'comment': 'return it'},
                )
            item.refresh_from_db()
            self.assertEqual(
                (item.status, item.assignment_group, item.assignee_email),
                ('in_progress', 'engineering', self.impl.email),
            )

    def _autonomous_request_changes(self):
        workflow = Workflow.objects.get(slug='autonomous-dev')
        return WorkflowTransition.objects.get(
            workflow=workflow,
            name='Request Changes',
            from_status='in_review',
            to_status='in_progress',
        )

    def _bounce_runtime(self, users, availability):
        directory = _PLS474Directory(list(users))
        return (
            patch('tasks.user_service.user_service', directory),
            patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                side_effect=availability,
            ),
            patch.object(
                WorkflowService, '_member_can_execute_from_status', return_value=True,
            ),
            patch.object(WorkflowService, '_is_human_account', return_value=False),
            patch.object(WorkflowService, '_notify_routability_hold', return_value=True),
        )

    def test_bounce_hold_path_never_seats_rejecting_reviewer(self):
        """PLS-474 / CTX-598: hold-path bounce group-queues, never seats the rejecter.

        Live shape: an autonomous-dev no-PR artifact whose submit owner was
        invalid arms ``implementation_return_hold`` naming the implementation
        team. When that team is ALSO the vacated reviewing lane's team
        (architecture reviews architecture work), the destination ladder
        reaches team resolution with no recorded implementer and a roster
        whose only resident member is the independent reviewer of record.
        The resolver must never seat the party who just rejected the work as
        the implementer — the typed outcome is the group queue.
        """
        request_changes = self._autonomous_request_changes()
        author = get_user_model().objects.create_user(
            username='pls474-arch-author',
            email='pls474-arch-author@shizuha.com',
            password='x',
        )
        rejecter = get_user_model().objects.create_user(
            username='pls474-arch-reviewer',
            email='pls474-arch-reviewer@shizuha.com',
            password='x',
        )
        self._set_roster('architecture', [author.email, rejecter.email])
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 rejecter retention reproducer',
            mode='task',
            workflow_name='autonomous-dev',
            status='in_review',
            workflow_status='in_review',
            assignee_id=rejecter.id,
            assignee_email=rejecter.email,
            assignment_group='architecture',
            metadata={
                'implementation_return_hold': {
                    'code': 'invalid_submit_owner',
                    'implementation_team': 'architecture',
                    'review_generation': 1,
                },
            },
        )

        def _availability(email):
            # The recorded author is down; the rejecter is resident. Pre-fix
            # the resolver's only available candidate was the rejecter itself.
            return email == rejecter.email

        patches = self._bounce_runtime([author, rejecter], _availability)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.captureOnCommitCallbacks(execute=True):
                WorkflowService.execute_transition(
                    item,
                    request_changes.to_status,
                    user=rejecter,
                    screen_data={'comment': 'changes required'},
                    transition_id=request_changes.id,
                )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertEqual(item.assignment_group, 'architecture')
        self.assertNotEqual(item.assignee_email, rejecter.email)
        self.assertEqual(item.assignee_email, '')
        self.assertIsNone(item.assignee_id)
        self.assertEqual(
            item.metadata['implementation_return_hold']['implementation_team'],
            'architecture',
        )

    def test_bounce_hold_path_seats_available_non_reviewer_candidate(self):
        """PLS-474 / PLAT-5343: with a second resident member, the bounce seats them.

        Same hold-path shape as the rejecter-retention reproducer, but the
        architecture roster has a second available member. The destination
        ladder must seat that member — not the rejecting reviewer — and emit
        exactly one assignment event for the committed identity.
        """
        request_changes = self._autonomous_request_changes()
        author = get_user_model().objects.create_user(
            username='pls474-arch-author2',
            email='pls474-arch-author2@shizuha.com',
            password='x',
        )
        rejecter = get_user_model().objects.create_user(
            username='pls474-arch-reviewer2',
            email='pls474-arch-reviewer2@shizuha.com',
            password='x',
        )
        other = get_user_model().objects.create_user(
            username='pls474-arch-other',
            email='pls474-arch-other@shizuha.com',
            password='x',
        )
        self._set_roster(
            'architecture', [author.email, rejecter.email, other.email],
        )
        item = Item.objects.create(
            project=self.project,
            title='PLS-474 non-rejecter team candidate',
            mode='task',
            workflow_name='autonomous-dev',
            status='in_review',
            workflow_status='in_review',
            assignee_id=rejecter.id,
            assignee_email=rejecter.email,
            assignment_group='architecture',
            metadata={
                'implementation_return_hold': {
                    'code': 'invalid_submit_owner',
                    'implementation_team': 'architecture',
                    'review_generation': 1,
                },
            },
        )

        def _availability(email):
            # The recorded author is down; the rejecter and the second member
            # are resident. Pre-fix the resolver could seat either — including
            # the reviewer of record.
            return email in (rejecter.email, other.email)

        patches = self._bounce_runtime(
            [author, rejecter, other], _availability,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patch('tasks.tasks.send_item_notification.delay') as dispatch:
                with self.captureOnCommitCallbacks(execute=True):
                    WorkflowService.execute_transition(
                        item,
                        request_changes.to_status,
                        user=rejecter,
                        screen_data={'comment': 'changes required'},
                        transition_id=request_changes.id,
                    )

        item.refresh_from_db()
        self.assertEqual(
            (item.status, item.assignment_group),
            ('in_progress', 'architecture'),
        )
        self.assertEqual(item.assignee_email, other.email)
        self.assertEqual(item.assignee_id, other.id)
        self.assertNotIn('implementation_return_hold', item.metadata)
        assignment_events = [
            call.args for call in dispatch.call_args_list
            if call.args[1] == 'assignment_changed'
        ]
        self.assertEqual(len(assignment_events), 1)
        self.assertEqual(
            assignment_events[0][2]['dispatched_assignee_id'], str(other.id),
        )
        self.assertNotEqual(
            assignment_events[0][2]['dispatched_assignee_id'], str(rejecter.id),
        )
        self.assertFalse(
            WipSlotReservation.objects.filter(item=item).exists(),
        )

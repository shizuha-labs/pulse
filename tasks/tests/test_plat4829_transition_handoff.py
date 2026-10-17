"""PLAT-4829 regressions for atomic, team-first workflow handoffs."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase

from tasks.models import (
    Comment,
    Item,
    Status,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService


class TransitionHandoffAtomicityTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.reviewer = users.objects.create_user(
            username='plat4829-reviewer',
            email='revi-test@shizuha.com',
            password='x',
        )
        self.merger = users.objects.create_user(
            username='plat4829-merger',
            email='ryo-test@shizuha.com',
            password='x',
        )
        source = Status.objects.create(
            slug='plat4829_review',
            name='PLAT-4829 Review',
            category='in_progress',
        )
        target = Status.objects.create(
            slug='plat4829_awaiting_merge',
            name='PLAT-4829 Awaiting Merge',
            category='in_progress',
        )
        self.workflow = Workflow.objects.create(
            name='PLAT-4829 Atomic Handoff',
            slug='plat4829-atomic-handoff',
            initial_status=source,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=source,
            position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=target,
            position=1,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Review Passed',
            from_status=source.slug,
            to_status=target.slug,
            position=0,
            post_functions=[],
        )

    def _item(self):
        return Item.objects.create(
            title='Team-first transition regression',
            mode='task',
            workflow_name=self.workflow.slug,
            status='plat4829_review',
            workflow_status='PLAT-4829 Review',
            assignment_group='review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            created_by_id=self.reviewer.id,
            created_by_email=self.reviewer.email,
            metadata={
                'pull_requests': [{
                    'repo': 'shizuha-labs/shizuha-tasks',
                    'number': 59,
                    'status': 'open',
                }],
            },
        )

    @staticmethod
    def _apply(item, uid, email, update_fields, **_kwargs):
        # The concrete owner is only valid after the destination team is present.
        assert item.assignment_group == 'merge'
        item.assignee_id = uid
        item.assignee_email = email
        for field in ('assignee_id', 'assignee_email'):
            if field not in update_fields:
                update_fields.append(field)
        return uid, email

    def _merge_patches(self, resolver):
        return (
            patch.object(
                WorkflowService,
                '_delegated_merge_repos',
                return_value={'shizuha-labs/shizuha-tasks'},
            ),
            patch.object(
                WorkflowService,
                '_merge_capable_roster_emails',
                return_value=set(),
            ),
            patch.object(
                WorkflowService,
                '_resolve_team_assignee',
                side_effect=resolver,
            ),
            patch.object(
                WorkflowService,
                '_merge_credential_diagnostic',
                return_value='',
            ),
            patch.object(
                WorkflowService,
                '_apply_resolved_assignee',
                side_effect=self._apply,
            ),
        )

    def test_route_merge_sets_destination_team_before_resolving_member(self):
        item = self._item()

        def resolve(team, **kwargs):
            self.assertEqual(team, 'merge')
            self.assertIs(kwargs['item'], item)
            self.assertEqual(item.assignment_group, 'merge')
            return self.merger.id, self.merger.email

        patches = self._merge_patches(resolve)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            self.transition.post_functions = [{'type': 'route_merge'}]
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.reviewer,
            )

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(item.assignee_id, self.merger.id)
        self.assertEqual(item.assignee_email, self.merger.email)

    def test_route_merge_without_available_member_stays_on_merge_team_queue(self):
        item = self._item()

        def resolve(team, **kwargs):
            self.assertEqual(team, 'merge')
            self.assertEqual(kwargs['item'].assignment_group, 'merge')
            return None, None

        patches = self._merge_patches(resolve)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            self.transition.post_functions = [{'type': 'route_merge'}]
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.reviewer,
            )

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'merge')
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)

    def test_post_function_failure_rolls_back_stage_team_owner_and_comment(self):
        item = self._item()

        def fail_after_partial_writes(_transition, current, user=None, screen_data=None):
            Comment.objects.create(
                item=current,
                author_id=0,
                author_email='system@pulse',
                content='must roll back with failed handoff',
            )
            current.assignment_group = 'merge'
            current.save(update_fields=['assignment_group', 'updated_at'])
            raise RuntimeError('simulated destination-owner failure')

        with patch.object(
            WorkflowService,
            'execute_post_functions',
            side_effect=fail_after_partial_writes,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                'simulated destination-owner failure',
            ):
                WorkflowService.execute_transition(
                    item,
                    'plat4829_awaiting_merge',
                    user=self.reviewer,
                    transition_id=self.transition.id,
                )

        item.refresh_from_db()
        self.assertEqual(item.status, 'plat4829_review')
        self.assertEqual(item.assignment_group, 'review')
        self.assertEqual(item.assignee_id, self.reviewer.id)
        self.assertEqual(item.assignee_email, self.reviewer.email)
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content='must roll back with failed handoff',
            ).exists()
        )

    @patch('tasks.tasks.send_item_notification')
    def test_transition_notification_waits_for_handoff_commit(self, mock_task):
        item = self._item()
        self.transition.post_functions = [{
            'type': 'send_notification',
            'template': 'status_changed',
            'recipients': ['assignee', 'watchers'],
        }]

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                WorkflowService.execute_post_functions(
                    self.transition,
                    item,
                    user=self.reviewer,
                )
                mock_task.delay.assert_not_called()
            mock_task.delay.assert_not_called()

        self.assertEqual(len(callbacks), 1)
        mock_task.delay.assert_called_once_with(
            str(item.id),
            'status_changed',
            {
                'old_status': '',
                'new_status': item.status,
                'changed_by': self.reviewer.email,
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        )

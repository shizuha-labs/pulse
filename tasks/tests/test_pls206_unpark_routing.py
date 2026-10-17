"""PLS-206 regression coverage for un-defer owner re-resolution."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import (
    Comment,
    Item,
    ItemActivity,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.mechanical_comments import MechanicalCommentFactory
from tasks.services.workflow_service import WorkflowService


OPERATOR = 'operator@example.test'
ENGINEER = 'sara@shizuha.com'


class UnparkRoutingTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls206', email='pls206@example.com', password='x', is_staff=True,
        )
        self.open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        self.deferred_status, _ = Status.objects.get_or_create(
            slug='deferred', defaults={'name': 'Deferred', 'category': 'done', 'position': 9},
        )
        self.workflow = Workflow.objects.create(
            name='PLS206 Workflow', slug='pls206-workflow', initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.open_status, position=1)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.deferred_status, position=9)
        self.resume = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Resume from Deferred',
            from_status='deferred',
            to_status='open',
        )
        self.project = Project.objects.create(name='PLS206 Project', key='P206', org_id=1)
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[ENGINEER]],
                'is_active': True,
            },
        )

    def _item(self, group='engineering'):
        # Reproduce the historical operator-parked row through an explicit
        # fixture seam. The transition itself runs after this patch exits and
        # must satisfy the real org/team assignment guard.
        with patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'fixture-org', 'fixture-org'),
        ):
            item = Item.objects.create(
                mode='task',
                title='Deferred task',
                project=self.project,
                item_key='P206-1',
                status='deferred',
                workflow_status='deferred',
                workflow_name=self.workflow.name,
                assignment_group=group,
                assignee_id=1,
                assignee_email=OPERATOR,
                created_by_id=self.user.id,
                created_by_email=self.user.email,
            )
        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='open',
            new_value='deferred',
            user_id=self.user.id,
            user_email=self.user.email,
        )
        return item

    @patch.object(WorkflowService, '_is_team_member', return_value=True)
    @patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    @patch.object(WorkflowService, '_resolve_team_assignee', return_value=(42, ENGINEER))
    def test_un_defer_reroutes_operator_parked_normal_team_work(self, resolve, *_):
        item = self._item(group='engineering')

        WorkflowService.execute_transition(
            item,
            'open',
            user=self.user,
            transition_id=str(self.resume.id),
        )
        item.refresh_from_db()

        self.assertEqual(item.status, 'open')
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(item.assignee_id, 42)
        self.assertEqual(item.assignee_email, ENGINEER)
        resolve.assert_any_call(
            'engineering', exclude_email=self.user.email,
            project_key='P206', item=item,
        )
        self.assertTrue(
            Comment.objects.filter(item=item, content__contains='Auto-rerouted after un-defer').exists()
        )

    @patch.object(WorkflowService, '_is_team_member', return_value=True)
    @patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    @patch.object(WorkflowService, '_resolve_team_assignee', return_value=(42, ENGINEER))
    def test_unpark_retry_reuses_preexisting_deferred_event_identity(self, *_):
        item = self._item(group='engineering')
        parked_activity = item.activities.get(
            action='status_changed', new_value='deferred',
        )
        original_create = MechanicalCommentFactory.create
        attempted_ids = []

        def fail_once_then_create(**kwargs):
            attempted_ids.append(kwargs['origin_event_id'])
            if len(attempted_ids) == 1:
                raise RuntimeError('force rollback before mechanical comment commit')
            return original_create(**kwargs)

        with patch.object(
            MechanicalCommentFactory, 'create', side_effect=fail_once_then_create,
        ):
            with self.assertRaises(RuntimeError):
                WorkflowService.execute_transition(
                    item,
                    'open',
                    user=self.user,
                    transition_id=str(self.resume.id),
                )
            item.refresh_from_db()
            self.assertEqual(item.status, 'deferred')
            WorkflowService.execute_transition(
                item,
                'open',
                user=self.user,
                transition_id=str(self.resume.id),
            )

        expected = (
            f'item-activity:{parked_activity.pk}:assignment.unpark_rerouted'
        )
        self.assertEqual(attempted_ids, [expected, expected])
        self.assertEqual(
            item.comments.filter(origin_event='assignment.unpark_rerouted').count(),
            1,
        )

    @patch.object(WorkflowService, '_is_team_member', return_value=True)
    @patch.object(WorkflowService, '_resolve_team_assignee', return_value=(42, ENGINEER))
    def test_un_defer_preserves_operator_gated_human_ops_item(self, resolve, *_):
        item = self._item(group='human-ops')

        WorkflowService.execute_transition(
            item,
            'open',
            user=self.user,
            transition_id=str(self.resume.id),
        )
        item.refresh_from_db()

        self.assertEqual(item.status, 'open')
        self.assertEqual(item.assignment_group, 'human-ops')
        self.assertEqual(item.assignee_email, OPERATOR)
        self.assertFalse(Comment.objects.filter(item=item, content__contains='Auto-rerouted after un-defer').exists())
        self.assertFalse(resolve.called)

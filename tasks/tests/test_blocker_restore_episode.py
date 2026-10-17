"""PLAT-7252: real endpoint/worker COMMIT at full ready WIP, across episodes.

The old link-only audit ID collides with a previous release (including v1
receipts). Capacity refusal is valid: an unassigned todo remains team-owned.
Only cross-service identity/liveness and Celery delivery are test fixtures.
"""
from contextlib import ExitStack
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase

from tasks.models import Comment, Item, ItemLink, TeamRoster, WorkflowTransition
from tasks.services import deferred_workflow
from tasks.services.mechanical_comments import MechanicalCommentFactory
from tasks.services.workflow_service import WorkflowService
from . import test_dependency_release_commit_boundary as fixtures


@skipUnless(connection.vendor == 'postgresql', 'actual deferred COMMIT constraint')
class BlockerRestoreEpisodeTests(TransactionTestCase):
    setUp = fixtures.DependencyReleaseCommitTests.setUp
    _item = fixtures.DependencyReleaseCommitTests._item
    _link_blocks = fixtures.DependencyReleaseCommitTests._link_blocks
    _post = fixtures.DependencyReleaseCommitTests._post
    _identity = fixtures.DependencyReleaseCommitTests._identity

    def _fixture(self, count=11):
        self.resume = WorkflowTransition.objects.create(
            workflow=self.wf, name='Blocker Resolved - Resume Todo',
            from_status='blocked', to_status='todo', position=4)
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Escalate to DevOps',
            from_status='', to_status='todo', is_global=True, position=0,
            post_functions=[{'type': 'assign_team', 'team': 'devops'}])
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Blocked by Linked Issue',
            from_status='todo', to_status='blocked', position=5)
        self.reopen = WorkflowTransition.objects.create(
            workflow=self.wf, name='Reopen', from_status='done',
            to_status='in_progress', position=6)
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='devops', defaults={
                'organization_slug': 'shizuha', 'is_active': True,
                'tiers': [[self.owner_a.email, self.owner_b.email]],
                'wip_enforced': True, 'ready_wip_cap': 5,
                'active_wip_cap': 5})
        TeamRoster.objects.filter(organization_id=1, team_name='engineering').update(
            organization_slug='shizuha')
        WorkflowService._invalidate_team_roster_caches()
        self.addCleanup(WorkflowService._invalidate_team_roster_caches)
        stack = ExitStack()
        self.addCleanup(stack.close)
        users = {x.email: {'id': x.pk, 'email': x.email, 'username': x.username}
                 for x in (self.actor, self.owner_a, self.owner_b)}
        stack.enter_context(patch('tasks.user_service.user_service.get_user_by_email',
                                  side_effect=lambda email: users.get(email)))
        stack.enter_context(patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True))
        stack.enter_context(patch.object(WorkflowService, '_member_can_execute_from_status', return_value=True))
        for owner in (self.owner_a, self.owner_b):
            for i in range(5):
                Item.objects.create(
                    project=self.project, item_key=f'P839-LOAD-{owner.pk}-{i}',
                    title='existing ready work', mode='task', status='todo',
                    workflow_name=self.wf.slug, workflow_status='todo',
                    assignment_group='devops', assignee_email=owner.email,
                    assignee_id=owner.pk, created_by_email=self.actor.email,
                    created_by_id=self.actor.pk)
        root = self._item('P839-ROOT', 'in_progress', email=self.actor.email, uid=self.actor.pk)
        children = []
        for i in range(count):
            child = self._item(f'P839-CHILD-{i}', 'todo', email=None, uid=None)
            Item.objects.filter(pk=child.pk).update(assignment_group='devops')
            child.refresh_from_db()
            self._link_blocks(root, child)
            children.append(child)
        return root, children

    def _release(self, root):
        response, _ = self._post(root)
        self.assertEqual(response.status_code, 202, response.content)
        with self._identity():
            result = deferred_workflow.execute(response.data['operation_id'])
        return result

    def _assert_restored(self, root, children, result, *, check_transition=True):
        self.assertEqual(result['state'], 'succeeded', result)
        root.refresh_from_db()
        self.assertEqual(root.status, 'done')
        for child in children:
            child.refresh_from_db()
            self.assertEqual((child.status, child.assignment_group,
                              child.assignee_id, child.assignee_email),
                             ('todo', 'devops', None, None))
            if check_transition:
                self.assertTrue(child.metadata['last_auto_unblock']['used_workflow_transition'])
        for owner in (self.owner_a, self.owner_b):
            self.assertEqual(Item.objects.filter(assignee_id=owner.pk, status='todo').count(), 5)

    def test_full_ready_team_restores_through_semantic_transition(self):
        root, children = self._fixture()
        # Prove the real capacity instrument denies these real five-row loads.
        with transaction.atomic():
            probe = children[0]
            probe.status = 'todo'
            self.assertEqual(WorkflowService._resolve_team_assignee('devops', item=probe), (None, None))
        result = self._release(root)
        self._assert_restored(root, children, result)
        self.assertEqual(deferred_workflow.execute(result['operation_id']), result)

    def test_historical_v1_link_receipt_is_immutable_and_new_episode_commits(self):
        root, children = self._fixture()
        child = children[-1]
        link = ItemLink.objects.get(source_item=root, target_item=child, link_type='blocks')
        historical = MechanicalCommentFactory.create(
            item=child, origin_kind='WORKFLOW_LIFECYCLE', origin_event='link.auto_unblock',
            template_version=1, origin_event_id=f'item-link:{link.pk}:link.auto_unblock',
            structured_payload={'blocker_key': root.item_key, 'old_status': 'blocked',
                                'restored_status': 'todo', 'assignee_email': self.owner_a.email},
            causal_actor=self.owner_a)
        before = Comment.objects.filter(pk=historical.pk).values().get()
        result = self._release(root)
        self._assert_restored(root, children, result)
        self.assertEqual(Comment.objects.filter(pk=historical.pk).values().get(), before)
        self.assertEqual(child.comments.filter(origin_event='link.auto_unblock').count(), 2)

    def test_same_link_two_real_release_reopen_cycles_have_distinct_receipts(self):
        root, children = self._fixture()
        first = self._release(root)
        self._assert_restored(root, children, first, check_transition=False)
        old_ids = set(Comment.objects.filter(item__in=children, origin_event='link.auto_unblock')
                      .values_list('origin_event_id', flat=True))
        self.assertEqual(len(old_ids), 11)
        with transaction.atomic():
            WorkflowService.execute_transition(root, 'in_progress', user=self.actor,
                                              transition_id=str(self.reopen.pk))
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)
        second = self._release(root)
        self._assert_restored(root, children, second, check_transition=False)
        ids = set(Comment.objects.filter(item__in=children, origin_event='link.auto_unblock')
                  .values_list('origin_event_id', flat=True))
        self.assertEqual(len(ids), 22)
        self.assertTrue(old_ids < ids)
        self.assertEqual(deferred_workflow.execute(second['operation_id']), second)
        self.assertEqual(Comment.objects.filter(item__in=children, origin_event='link.auto_unblock').count(), 22)

    def test_duplicate_semantic_recovery_refuses_and_preserves_whole_graph(self):
        root, children = self._fixture()
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Blocker Resolved - Resume To Do duplicate',
            from_status='blocked', to_status='todo')
        result = self._release(root)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['code'], 'dependency_release_rejected')
        root.refresh_from_db()
        self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)
        self.assertFalse(Comment.objects.filter(item__in=children, origin_event='link.auto_unblock').exists())

    def test_small_team_owned_graph_still_commits_inline(self):
        root, children = self._fixture(2)
        response, delivery = self._post(root)
        self.assertEqual(response.status_code, 200, response.content)
        delivery.assert_not_called()
        self._assert_restored(root, children, {'state': 'succeeded'})

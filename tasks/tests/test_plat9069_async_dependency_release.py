"""PLAT-9069: dependency-release reconciliation must not time out at scale.

Live defect (jun ANDON on PLAT-7421, 2026-09-17): firing the terminal ``Done``
transition on a gate-layer root with ~47 blocked descendants timed out every
attempt — the synchronous ``require_restore`` fan-out is O(dependents) inside
the 30s API window (three documented timeout/reject cycles, no state change).

Fix pinned here:
- large transition/complete requests persist a typed command before any status
  mutation; its worker commits the full dependency graph atomically;
- small graphs keep the PLS-953 atomic inline contract (restored immediately,
  rejected restore still rolls the outer transaction back);
- pending intent is a durable operation receipt, never a completion claim.
  Legacy already-enqueued descendant jobs remain covered for compatibility.
"""
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import (
    Comment,
    Item,
    ItemLink,
    Project,
    ProjectMembership,
    TeamRoster,
)
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import release_blocked_descendants_async


class AsyncDependencyReleaseTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='plat9069', email='plat9069@shizuha.com',
            password='testpass123',
        )
        self.user.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(
            name='Platform', key='PLT', org_id=1,
            access_level='private', is_active=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='member',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.user.email]],
                'is_active': True,
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _task(self, item_key, title, status, **fields):
        item = Item.objects.create(
            mode='task', project=self.project, item_key=item_key,
            title=title, status=status,
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        ownership = {
            k: v for k, v in fields.items()
            if k in ('assignee_id', 'assignee_email', 'assignment_group')
        }
        if ownership:
            if ownership.get('assignee_id') and not ownership.get('assignment_group'):
                ownership['assignment_group'] = 'engineering'
            Item.objects.filter(pk=item.pk).update(**ownership)
            item.refresh_from_db()
        return item

    def _blocked_dependent(self, item_key, blocker):
        """A blocked dependent held only by ``blocker`` (PLAT-4215-safe fixture:
        the model choke-point rejects direct blocked creates, so enter blocked
        via QuerySet.update after stamping the captured pre-block state)."""
        dependent = self._task(
            item_key, f'dependent {item_key}', 'in_progress',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        Item.objects.filter(pk=dependent.pk).update(
            status='blocked',
            metadata={
                'pre_block_state': {
                    'status': 'in_progress',
                    'had_active_blocker': True,
                    'blocked_by_email': 'system',
                },
            },
        )
        dependent.refresh_from_db()
        ItemLink.objects.create(
            source_item=blocker, target_item=dependent, link_type='blocks',
            created_by_id=self.user.id,
        )
        return dependent

    def _gate_root(self, n_descendants, key_prefix='PLT-9'):
        root = self._task(
            f'{key_prefix}069-root', 'gate root', 'in_progress',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        dependents = [
            self._blocked_dependent(f'{key_prefix}069-d{i}', root)
            for i in range(n_descendants)
        ]
        return root, dependents

    @patch.dict(os.environ, {'PULSE_DEPENDENCY_RELEASE_INLINE_MAX': '10'})
    def test_scale_regression_45_descendants_queues_before_status_write(self):
        """Admission is pending; real COMMIT/worker coverage lives in the
        TransactionTestCase suite, not TestCase's outer rollback transaction."""
        from tasks.models import DeferredWorkflowCommand
        from tasks.services.deferred_workflow import execute
        root, dependents = self._gate_root(45)
        with patch('tasks.tasks.execute_deferred_workflow_command.delay'):
            resp = self.client.post(f'/api/items/{root.pk}/complete/', {}, format='json')
        self.assertEqual(resp.status_code, 202, resp.content)
        root.refresh_from_db()
        self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[d.pk for d in dependents], status='blocked').count(), 45)
        command = DeferredWorkflowCommand.objects.get(pk=resp.data['operation_id'])
        with patch('tasks.user_service.UserService.get_user_by_id', return_value={
            'id': self.user.id, 'email': self.user.email, 'is_active': True,
            'is_staff': False, 'is_superuser': False,
        }), patch('tasks.services.admin_service.admin_service._make_request', return_value={
            'is_member': True, 'role': 'owner',
        }):
            result = execute(command.pk)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(Item.objects.filter(pk__in=[d.pk for d in dependents], status='blocked').count(), 0)

    @patch.dict(os.environ, {'PULSE_DEPENDENCY_RELEASE_INLINE_MAX': '10'})
    def test_small_graph_keeps_atomic_inline_contract(self):
        """Below the threshold the PLS-953 contract is unchanged: dependents
        restore inline in the request transaction, no fan-out machinery."""
        root, dependents = self._gate_root(3)
        resp = self.client.post(f'/api/items/{root.id}/complete/', {}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        root.refresh_from_db()
        self.assertTrue(WorkflowService._status_is_terminal(root.status))
        for dependent in dependents:
            dependent.refresh_from_db()
            self.assertNotEqual(dependent.status, 'blocked')
        self.assertFalse(Comment.objects.filter(
            item=root, content__contains='Dependency release fan-out (async)',
        ).exists())
        self.assertNotIn(
            'dependency_release_fanout', (root.metadata or {}),
        )

    @patch.dict(os.environ, {'PULSE_DEPENDENCY_RELEASE_INLINE_MAX': '10'})
    def test_batch_job_is_bounded_replayable_and_marks_completion_once(self):
        """The background job restores at most ``batch_size`` dependents per
        invocation, re-enqueues itself while work remains, and a replayed
        batch after completion is a no-op that does not duplicate the
        completion marker."""
        root, dependents = self._gate_root(5)
        Item.objects.filter(pk=root.pk).update(status='done')
        root.refresh_from_db()

        release_blocked_descendants_async.delay(
            blocker_id=root.pk, batch_size=2,
        )
        root.refresh_from_db()
        fanout = (root.metadata or {}).get('dependency_release_fanout', {})
        self.assertEqual(fanout.get('state'), 'complete')
        self.assertEqual(fanout.get('restored'), 5)
        self.assertEqual(
            Item.objects.filter(pk__in=[d.pk for d in dependents], status='blocked').count(),
            0,
        )
        completion_comments = Comment.objects.filter(
            item=root, author_email='system@pulse',
            content__contains='Dependency release fan-out complete',
        )
        self.assertEqual(completion_comments.count(), 1)

        # Replay (at-least-once redelivery): no restores, no duplicate marker.
        release_blocked_descendants_async.delay(
            blocker_id=root.pk, batch_size=2,
        )
        self.assertEqual(completion_comments.count(), 1)
        root.refresh_from_db()
        self.assertEqual(
            (root.metadata or {}).get('dependency_release_fanout', {}).get('restored'),
            5,
        )

    @patch.dict(os.environ, {'PULSE_DEPENDENCY_RELEASE_INLINE_MAX': '10'})
    def test_replayed_batch_after_reopen_is_a_noop(self):
        """PLS-895 interplay: a fan-out batch redelivered after the root
        reopened must not restore anything — the reopen path re-armed the
        links and owns the graph state."""
        root, dependents = self._gate_root(2)
        Item.objects.filter(pk=root.pk).update(status='done')
        root.refresh_from_db()
        # Reopen before the job runs: the root no longer releases blocks.
        Item.objects.filter(pk=root.pk).update(status='in_progress')
        root.refresh_from_db()

        result = release_blocked_descendants_async.delay(blocker_id=root.pk)
        for dependent in dependents:
            dependent.refresh_from_db()
            self.assertEqual(
                dependent.status, 'blocked',
                'a reopened root must not release its dependents',
            )
        self.assertNotIn('dependency_release_fanout', (root.metadata or {}))

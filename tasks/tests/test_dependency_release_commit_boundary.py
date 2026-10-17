"""Real commit ordering: enqueue -> commit -> worker -> commit -> redelivery.

Baseline079 returns PLAT-4215 HTTP409 from BOTH endpoints before on_commit.
No deferred constraint, workflow validator, identity or graph restore is mocked.
Only the worker's ID/Admin HTTP reads and Celery delivery are bounded fixtures.
"""
from contextlib import ExitStack
from unittest.mock import patch
from unittest import skipUnless
from django.db import connection, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from tasks.models import Comment, DeferredWorkflowCommand, Item, ItemLink
from tasks.services import deferred_workflow
from tasks.services.workflow_service import WorkflowService
from . import test_pls839_blocker_root_merge as fixtures


@skipUnless(connection.vendor == 'postgresql', 'real deferred invariant')
class DependencyReleaseCommitTests(TransactionTestCase):
    setUp = fixtures.Pls839BlockerRootMergeTests.setUp
    _item = fixtures.Pls839BlockerRootMergeTests._item
    _link_blocks = fixtures.Pls839BlockerRootMergeTests._link_blocks

    def _graph(self, count=11):
        root = self._item('P839-ROOT', 'in_progress', email=self.actor.email, uid=self.actor.id)
        children = []
        for i in range(count):
            child = self._item(f'P839-CHILD-{i}', 'in_progress', email=self.owner_a.email, uid=self.owner_a.id)
            self._link_blocks(root, child)
            children.append(child)
        return root, children

    def _post(self, root, kind='transition', **extra):
        client = APIClient()
        client.force_authenticate(self.actor)
        body = {'to_status': 'done', 'transition_id': str(self.complete_t.pk), **extra}
        route = 'execute-transition' if kind == 'transition' else 'complete'
        with patch('tasks.tasks.execute_deferred_workflow_command.delay') as delivery:
            response = client.post(f'/api/items/{root.pk}/{route}/', body if kind == 'transition' else {}, format='json')
        return response, delivery

    def _identity(self, *, active=True, member=True):
        stack = ExitStack()
        stack.enter_context(patch('tasks.user_service.UserService.get_user_by_id', return_value={
            'id': self.actor.id, 'email': self.actor.email, 'is_active': active,
            'is_staff': False, 'is_superuser': False}))
        stack.enter_context(patch('tasks.services.admin_service.admin_service._make_request', return_value={
            'is_member': member, 'role': 'owner'}))
        return stack

    def _assert_pending(self, root, children, response, delivery):
        self.assertEqual(response.status_code, 202, response.content.decode())
        self.assertTrue(response.data['pending'])
        self.assertFalse(response.data['committed'])
        root.refresh_from_db()
        self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), len(children))
        delivery.assert_called_once_with(response.data['operation_id'])
        return response.data['operation_id']

    def test_transition_commit_worker_and_redelivery(self):
        root, children = self._graph()
        response, delivery = self._post(root, comment='Accepted verified result')
        operation_id = self._assert_pending(root, children, response, delivery)
        self.assertFalse(Comment.objects.filter(item=root, content='Accepted verified result').exists())
        again, delivery = self._post(root, comment='Accepted verified result')
        self.assertEqual(again.status_code, 202)
        self.assertEqual(again.data['operation_id'], operation_id)
        delivery.assert_not_called()
        with self._identity():
            result = deferred_workflow.execute(operation_id)
        self.assertEqual(result['state'], 'succeeded', result)
        root.refresh_from_db()
        self.assertEqual(root.status, 'done')
        self.assertFalse(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').exists())
        self.assertEqual(Comment.objects.filter(item=root, content='Accepted verified result').count(), 1)
        with patch('tasks.user_service.UserService.get_user_by_id') as identity:
            replay = deferred_workflow.execute(operation_id)
        identity.assert_not_called()
        self.assertEqual(replay, result)
        self.assertEqual(Comment.objects.filter(item=root, content='Accepted verified result').count(), 1)

    def test_complete_commit_worker_and_redelivery(self):
        root, children = self._graph()
        response, delivery = self._post(root, 'complete')
        operation_id = self._assert_pending(root, children, response, delivery)
        with self._identity():
            result = deferred_workflow.execute(operation_id)
        self.assertEqual(result['state'], 'succeeded', result)
        root.refresh_from_db()
        self.assertEqual(root.status, 'done')
        self.assertEqual(deferred_workflow.execute(operation_id), result)
        self.assertFalse(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').exists())

    def test_unrestorable_last_dependent_rolls_every_write_back(self):
        root, children = self._graph()
        last = children[-1]
        md = dict(last.metadata)
        md['pre_block_state']['had_active_blocker'] = False
        Item.objects.filter(pk=last.pk).update(metadata=md)
        response, delivery = self._post(root, comment='Must not survive rollback')
        operation_id = self._assert_pending(root, children, response, delivery)
        with self._identity():
            result = deferred_workflow.execute(operation_id)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['code'], 'dependency_release_rejected')
        root.refresh_from_db()
        self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)
        self.assertFalse(Comment.objects.filter(item=root, content='Must not survive rollback').exists())
        self.assertFalse(Comment.objects.filter(item_id__in=[x.pk for x in children], origin_event='link.auto_unblock').exists())
        self.assertEqual(deferred_workflow.execute(operation_id), result)

    def test_graph_change_stales_queued_command(self):
        root, children = self._graph()
        response, _ = self._post(root)
        other = self._item('P839-OTHER', 'in_progress', email=self.actor.email, uid=self.actor.id)
        self._link_blocks(other, children[-1])
        with self._identity():
            result = deferred_workflow.execute(response.data['operation_id'])
        self.assertEqual(result['state'], 'stale', result)
        root.refresh_from_db()
        self.assertEqual(root.status, 'in_progress')

    def test_generation_change_even_return_to_same_status_is_stale(self):
        root, children = self._graph()
        response, _ = self._post(root)
        root.refresh_from_db()
        before = root.state_generation
        root.status = root.workflow_status = 'in_review'
        root.save(update_fields=['status', 'workflow_status'])
        root.refresh_from_db()
        self.assertGreater(root.state_generation, before)
        # A real active-state change followed by return must not revive old intent.
        root.status = root.workflow_status = 'in_progress'
        root.save(update_fields=['status', 'workflow_status'])
        with self._identity():
            result = deferred_workflow.execute(response.data['operation_id'])
        self.assertEqual(result['state'], 'stale', result)

    def test_workflow_definition_change_is_stale(self):
        root, _ = self._graph()
        response, _ = self._post(root)
        self.complete_t.allowed_roles = ['security']
        self.complete_t.save(update_fields=['allowed_roles'])
        with self._identity():
            result = deferred_workflow.execute(response.data['operation_id'])
        self.assertEqual(result['state'], 'stale', result)

    def test_revoked_actor_or_membership_refuses_without_mutation(self):
        root, children = self._graph()
        for active, member in [(False, True), (True, False)]:
            response, _ = self._post(root)
            with self._identity(active=active, member=member):
                result = deferred_workflow.execute(response.data['operation_id'])
            self.assertEqual(result['state'], 'failed', result)
            root.refresh_from_db()
            self.assertEqual(root.status, 'in_progress')
            self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)

    def test_broker_failure_preserves_intent_and_relay_recovers(self):
        root, _ = self._graph()
        client = APIClient(); client.force_authenticate(self.actor)
        with patch('tasks.tasks.execute_deferred_workflow_command.delay', side_effect=OSError('broker down')):
            response = client.post(f'/api/items/{root.pk}/complete/', {}, format='json')
        self.assertEqual(response.status_code, 202, response.content)
        with patch('tasks.tasks.execute_deferred_workflow_command.delay') as delivery:
            self.assertEqual(deferred_workflow.relay_pending(), 1)
        delivery.assert_called_once_with(response.data['operation_id'])
        with self._identity():
            self.assertEqual(deferred_workflow.execute(response.data['operation_id'])['state'], 'succeeded')

    def test_small_graph_remains_inline_and_no_command_exists(self):
        root, children = self._graph(2)
        response, delivery = self._post(root)
        self.assertEqual(response.status_code, 200, response.content)
        root.refresh_from_db(); self.assertEqual(root.status, 'done')
        delivery.assert_not_called()
        self.assertFalse(DeferredWorkflowCommand.objects.exists())
        self.assertFalse(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').exists())

    def test_other_owned_active_blocker_is_preserved(self):
        root, children = self._graph()
        other = self._item('P839-OTHER', 'in_progress', email=self.actor.email, uid=self.actor.id)
        self._link_blocks(other, children[-1])
        response, _ = self._post(root)
        with self._identity():
            result = deferred_workflow.execute(response.data['operation_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        children[-1].refresh_from_db(); self.assertEqual(children[-1].status, 'blocked')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 1)

    def test_worker_loss_rolls_back_and_redelivery_can_finish(self):
        root, children = self._graph()
        response, _ = self._post(root)
        operation_id = response.data['operation_id']
        restore = WorkflowService._restore_blocked_item
        def interrupted(*args, **kwargs):
            restore(*args, **kwargs)
            raise KeyboardInterrupt('worker process lost after first restore')
        with self._identity(), patch.object(WorkflowService, '_restore_blocked_item', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                deferred_workflow.execute(operation_id)
        self.assertEqual(DeferredWorkflowCommand.objects.get(pk=operation_id).state, 'pending')
        root.refresh_from_db(); self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)
        with self._identity():
            result = deferred_workflow.execute(operation_id)
        self.assertEqual(result['state'], 'succeeded', result)

    def test_operation_read_keeps_task_truth_and_rejects_bad_id(self):
        root, _ = self._graph()
        response, _ = self._post(root)
        client = APIClient(); client.force_authenticate(self.actor)
        read = client.get(f'/api/items/{root.pk}/workflow-operations/', {'operation_id': response.data['operation_id']})
        self.assertEqual(read.status_code, 200)
        self.assertTrue(read.data[0]['pending'])
        self.assertFalse(read.data[0]['committed'])
        self.assertEqual(read.data[0]['status'], 'in_progress')
        self.assertEqual(client.get(f'/api/items/{root.pk}/workflow-operations/', {'operation_id': 'bad'}).status_code, 400)

    def _external(self, root):
        from django.utils import timezone
        from tasks.models import ExternalMcpClient, ExternalMcpToken, ProjectMembership
        client = ExternalMcpClient.objects.create(
            org_id=1, name='deferred caller', owner_user_id=self.actor.id,
            owner_email=self.actor.email, allowed_project_ids=[self.project.pk],
            scopes=['task.read', 'transition.execute'], rate_limit_policy={'per_minute': 100})
        ProjectMembership.objects.get_or_create(project=self.project, user_id=self.actor.id,
                                               defaults={'user_email': self.actor.email, 'role': 'member'})
        token = ExternalMcpToken.objects.create(client=client,
            token_hash=ExternalMcpToken.hash_token('deferred-test-token'),
            scopes=['task.read', 'transition.execute'], expires_at=timezone.now()+timezone.timedelta(days=1))
        def post():
            return APIClient().post('/api/external-mcp/call/', {
                'tool': 'pulse_execute_transition', 'arguments': {
                    'task_id': root.item_key, 'to_status': 'done', 'transition_id': str(self.complete_t.pk)}
            }, format='json', HTTP_AUTHORIZATION='Bearer deferred-test-token', HTTP_IDEMPOTENCY_KEY='deferred-close')
        return token, post

    def test_external_pending_replay_and_committed_receipt(self):
        root, _ = self._graph()
        token, post = self._external(root)
        with patch('tasks.tasks.execute_deferred_workflow_command.delay') as delivery:
            response = post()
            self.assertEqual(response.status_code, 202, response.content)
            pending = response.data['result']
            replay = post()
        delivery.assert_called_once_with(pending['operation_id'])
        self.assertEqual(replay.status_code, 202, replay.content)
        self.assertTrue(replay.data['result']['pending'])
        self.assertEqual(replay.data['result']['operation_id'], pending['operation_id'])
        with self._identity():
            result = deferred_workflow.execute(pending['operation_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        replay = post()
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.data['result']['pending'])
        self.assertTrue(replay.data['result']['committed'])
        self.assertEqual(Comment.objects.filter(item=root, content__contains='idempotency_key=deferred-close').count(), 1)

    def test_external_inline_grant_does_not_require_org_membership(self):
        root, children = self._graph(2)
        token, post = self._external(root)
        from tasks.models import ProjectMembership
        ProjectMembership.objects.filter(project=self.project, user_id=self.actor.id).delete()
        with self._identity(member=False), patch('tasks.services.admin_service.admin_service._make_request',
                                                return_value={'is_member': False}) as membership:
            response = post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(any('/members/' in str(call.args[1]) for call in membership.call_args_list),
                         membership.call_args_list)
        root.refresh_from_db(); self.assertEqual(root.status, 'done')
        self.assertFalse(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').exists())

    def test_external_deferred_grant_does_not_require_org_membership(self):
        root, children = self._graph()
        token, post = self._external(root)
        from tasks.models import ProjectMembership
        ProjectMembership.objects.filter(project=self.project, user_id=self.actor.id).delete()
        with self._identity(member=False), patch('tasks.services.admin_service.admin_service._make_request',
                                                return_value={'is_member': False}) as membership:
            with patch('tasks.tasks.execute_deferred_workflow_command.delay'):
                response = post()
            self.assertEqual(response.status_code, 202, response.content)
            result = deferred_workflow.execute(response.data['result']['operation_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertFalse(any('/members/' in str(call.args[1]) for call in membership.call_args_list),
                         membership.call_args_list)
        root.refresh_from_db(); self.assertEqual(root.status, 'done')
        self.assertFalse(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').exists())

    def test_external_removed_project_grant_refuses_queued_write_without_membership(self):
        root, children = self._graph()
        token, post = self._external(root)
        with patch('tasks.tasks.execute_deferred_workflow_command.delay'):
            response = post()
        self.assertEqual(response.status_code, 202, response.content)
        token.client.allowed_project_ids = []
        token.client.save(update_fields=['allowed_project_ids'])
        with self._identity(member=False), patch('tasks.services.admin_service.admin_service._make_request',
                                                return_value={'is_member': False}) as membership:
            result = deferred_workflow.execute(response.data['result']['operation_id'])
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('visibility was lost', result['detail'])
        self.assertFalse(any('/members/' in str(call.args[1]) for call in membership.call_args_list),
                         membership.call_args_list)
        root.refresh_from_db(); self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)

    def test_external_token_revocation_prevents_queued_write(self):
        root, children = self._graph()
        token, post = self._external(root)
        with patch('tasks.tasks.execute_deferred_workflow_command.delay'):
            response = post()
        self.assertEqual(response.status_code, 202, response.content)
        from django.utils import timezone
        token.revoked_at = timezone.now()
        token.save(update_fields=['revoked_at'])
        with self._identity():
            result = deferred_workflow.execute(response.data['result']['operation_id'])
        self.assertEqual(result['state'], 'failed', result)
        root.refresh_from_db(); self.assertEqual(root.status, 'in_progress')
        self.assertEqual(Item.objects.filter(pk__in=[x.pk for x in children], status='blocked').count(), 11)

    def test_concurrent_roots_lock_shared_graph_in_one_order(self):
        import threading
        from django.db import close_old_connections
        root, children = self._graph()
        other = self._item('P839-OTHER', 'in_progress', email=self.actor.email, uid=self.actor.id)
        for index in range(11):
            child = self._item(f'P839-OTHER-{index}', 'in_progress', email=self.owner_a.email, uid=self.owner_a.id)
            self._link_blocks(other, child)
        self._link_blocks(other, children[0])
        first, _ = self._post(root)
        second, _ = self._post(other)
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(second.status_code, 202, second.content)
        barrier = threading.Barrier(2, timeout=15)
        graph = deferred_workflow._graph
        def synchronized_graph(item, *, lock=False):
            if lock:
                barrier.wait()
            return graph(item, lock=lock)
        results, errors = [], []
        def worker(operation_id):
            close_old_connections()
            try:
                results.append(deferred_workflow.execute(operation_id))
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                close_old_connections()
        threads = [threading.Thread(target=worker, args=(response.data['operation_id'],))
                   for response in (first, second)]
        with self._identity(), patch.object(deferred_workflow, '_graph', side_effect=synchronized_graph):
            for thread in threads: thread.start()
            for thread in threads: thread.join(45)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result['state'] for result in results), ['stale', 'succeeded'], results)
        # The loser saw a real graph-generation change, not a deadlock failure.
        stale = next(result for result in results if result['state'] == 'stale')
        self.assertEqual(stale['code'], 'deferred_transition_stale')
        stale_command = DeferredWorkflowCommand.objects.get(pk=stale['operation_id'])
        retry, _ = self._post(stale_command.item)
        with self._identity():
            result = deferred_workflow.execute(retry.data['operation_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        children[0].refresh_from_db()
        self.assertEqual(children[0].status, 'in_progress')

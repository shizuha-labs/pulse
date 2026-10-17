from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from tasks.models import (
    Comment,
    ExternalMcpCallAudit,
    ExternalMcpClient,
    ExternalMcpToken,
    Item,
    ItemLink,
    Project,
    ProjectMembership,
    Status,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from .base import create_blocked_item


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class ExternalMcpGatewayTests(APITestCase):
    def setUp(self):
        self.project_a = Project.objects.create(name='Tenant A', key='TA', org_id=2)
        self.project_b = Project.objects.create(name='Tenant B', key='TB', org_id=3)
        self.item_a = Item.objects.create(
            mode='task', project=self.project_a, item_key='TA-1', title='visible task',
            description='alpha body', status='open', workflow_name='simple', created_by_id=1,
        )
        self.item_b = Item.objects.create(
            mode='task', project=self.project_b, item_key='TB-1', title='hidden task',
            description='secret body', status='open', workflow_name='simple', created_by_id=2,
        )
        ProjectMembership.objects.create(
            project=self.project_a,
            user_id=999,
            user_email='someone-else@example.com',
            role='member',
        )
        ProjectMembership.objects.create(
            project=self.project_a,
            user_id=10,
            user_email='owner@example.com',
            role='member',
        )
        Comment.objects.create(item=self.item_a, author_id=1, author_email='a@example.com', content='visible comment')
        self.client_record = ExternalMcpClient.objects.create(
            org_id=2,
            name='Cursor tenant A',
            owner_user_id=10,
            owner_email='owner@example.com',
            allowed_project_ids=[self.project_a.id],
            scopes=['task.read'],
            rate_limit_policy={'per_minute': 100},
        )
        self.raw_token = 'pls304-token-a'
        self.token = ExternalMcpToken.objects.create(
            client=self.client_record,
            token_hash=ExternalMcpToken.hash_token(self.raw_token),
            scopes=['task.read'],
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.raw_token}'}

    def call_tool(self, tool, arguments):
        return self.client.post(
            '/api/external-mcp/call/',
            {'tool': tool, 'arguments': arguments},
            format='json',
            **self.auth,
        )

    def make_token(self, scopes, raw_token='pls308-token-write', rate_limit_policy=None):
        client_record = ExternalMcpClient.objects.create(
            org_id=2,
            name=f'Write client {raw_token}',
            owner_user_id=10,
            owner_email='owner@example.com',
            allowed_project_ids=[self.project_a.id],
            scopes=scopes,
            rate_limit_policy=rate_limit_policy or {'per_minute': 100},
        )
        ExternalMcpToken.objects.create(
            client=client_record,
            token_hash=ExternalMcpToken.hash_token(raw_token),
            scopes=scopes,
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        return {'HTTP_AUTHORIZATION': f'Bearer {raw_token}'}

    def call_tool_with_auth(self, tool, arguments, auth, idempotency_key=None):
        headers = dict(auth)
        if idempotency_key:
            headers['HTTP_IDEMPOTENCY_KEY'] = idempotency_key
        return self.client.post(
            '/api/external-mcp/call/',
            {'tool': tool, 'arguments': arguments},
            format='json',
            **headers,
        )

    def make_focus_root(self, title, *, workflow_name='simple', priority='urgent'):
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            root = Item.objects.create(
                mode='task', project=self.project_a, title=title,
                status='open', priority=priority, workflow_name=workflow_name,
                assignee_id=10, assignee_email='owner@example.com', created_by_id=10,
            )
            create_blocked_item(
                blocker=root,
                mode='task', project=self.project_a, title=f'{title} dependent',
                status='blocked', priority='urgent', workflow_name=workflow_name,
                assignee_id=10, assignee_email='owner@example.com', created_by_id=10,
            )
        return root

    def test_discovery_returns_read_only_authorized_tools(self):
        response = self.client.get('/api/external-mcp/tools/', **self.auth)
        self.assertEqual(response.status_code, 200, response.content)
        names = {tool['name'] for tool in response.json()['tools']}
        self.assertIn('pulse_get_task', names)
        self.assertIn('pulse_list_comments', names)
        self.assertNotIn('pulse_add_comment', names)
        self.assertNotIn('pulse_execute_transition', names)
        self.assertTrue(ExternalMcpCallAudit.objects.filter(tool_name='tools.discover', result_status='success').exists())

    def test_two_tenant_scope_allows_visible_project_and_denies_cross_org_task(self):
        ok = self.call_tool('pulse_get_task', {'task_id': 'TA-1'})
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()['result']['item_key'], 'TA-1')

        denied = self.call_tool('pulse_get_task', {'task_id': 'TB-1'})
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertIn('not found', denied.json()['error'].lower())

        search = self.call_tool('pulse_search_tasks', {'query': 'task'})
        self.assertEqual(search.status_code, 200, search.content)
        keys = {item['item_key'] for item in search.json()['result']}
        self.assertEqual(keys, {'TA-1'})

    def test_read_only_token_cannot_call_write_tools(self):
        response = self.call_tool('pulse_add_comment', {'task_id': 'TA-1', 'content': 'nope'})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()['error'], 'scope_denied')
        self.assertFalse(Comment.objects.filter(item=self.item_a, content='nope').exists())
        self.assertTrue(ExternalMcpCallAudit.objects.filter(tool_name='pulse_add_comment', result_status='denied').exists())

    def test_comments_and_issue_links_are_filtered_to_visible_items(self):
        with self.assertRaises(ValidationError):
            ItemLink.objects.create(
                source_item=self.item_a,
                target_item=self.item_b,
                link_type='relates_to',
                created_by_id=1,
                created_by_email='a@example.com',
            )
        comments = self.call_tool('pulse_list_comments', {'task_id': 'TA-1'})
        self.assertEqual(comments.status_code, 200, comments.content)
        self.assertEqual(comments.json()['result'][0]['content'], 'visible comment')

        links = self.call_tool('pulse_get_issue_links', {'task_id': 'TA-1'})
        self.assertEqual(links.status_code, 200, links.content)
        self.assertEqual(links.json()['result'], [])

    def test_expired_or_revoked_token_is_rejected(self):
        self.token.revoked_at = timezone.now()
        self.token.save(update_fields=['revoked_at'])
        response = self.client.get('/api/external-mcp/tools/', **self.auth)
        self.assertEqual(response.status_code, 401, response.content)

    def test_write_discovery_filters_by_explicit_scopes(self):
        comment_auth = self.make_token(['task.read', 'comment.write'], raw_token='pls308-comment')
        response = self.client.get('/api/external-mcp/tools/', **comment_auth)
        self.assertEqual(response.status_code, 200, response.content)
        names = {tool['name'] for tool in response.json()['tools']}
        self.assertIn('pulse_get_task', names)
        self.assertIn('pulse_add_comment', names)
        self.assertNotIn('pulse_create_task', names)
        self.assertNotIn('pulse_execute_transition', names)

    def test_add_comment_requires_idempotency_and_replays_without_duplicate(self):
        auth = self.make_token(['task.read', 'comment.write'])
        missing = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'external note'},
            auth,
        )
        self.assertEqual(missing.status_code, 400, missing.content)
        self.assertEqual(missing.json()['error'], 'idempotency_key_required')

        first = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'external note'},
            auth,
            idempotency_key='comment-1',
        )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertIn('External MCP `pulse_add_comment`', Comment.objects.get(id=first.json()['result']['id']).content)

        replay = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'external note'},
            auth,
            idempotency_key='comment-1',
        )
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertTrue(replay.json()['result']['idempotent_replay'])
        self.assertEqual(
            Comment.objects.filter(item=self.item_a, content__icontains='external note').count(),
            1,
        )

        conflict = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'changed note'},
            auth,
            idempotency_key='comment-1',
        )
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(conflict.json()['error'], 'idempotency_key_conflict')
        self.assertTrue(
            ExternalMcpCallAudit.objects.filter(
                tool_name='pulse_add_comment',
                idempotency_key='comment-1',
                result_status='success',
                task_id=str(self.item_a.pk),
            ).exists()
        )

    def test_external_mcp_comment_has_one_equal_root_fifo_leader(self):
        """The external write gateway shares the native strict focus key."""
        first = self.make_focus_root('external comment first')
        second = self.make_focus_root('external comment second')
        auth = self.make_token(
            ['task.read', 'comment.write'], raw_token='pls561-comment-focus',
        )

        leading = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': str(first.pk), 'content': 'leader evidence'},
            auth,
            idempotency_key='pls561-comment-leading',
        )
        trailing = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': str(second.pk), 'content': 'later peer evidence'},
            auth,
            idempotency_key='pls561-comment-trailing',
        )

        self.assertEqual(leading.status_code, 200, leading.content)
        self.assertEqual(trailing.status_code, 409, trailing.content)
        self.assertEqual(trailing.json()['error'], 'blocker_focus_required')
        self.assertIn('owned blocker root', trailing.json()['detail'].lower())
        self.assertTrue(
            Comment.objects.filter(item=first, content__icontains='leader evidence').exists()
        )
        # PLS-722: the refusal still stands — no USER_AUTHORED comment is
        # accepted on `second`, which is what this test is actually about. What
        # changed is that the refused body is no longer DESTROYED: it is kept as
        # a mechanical FOCUS_REFUSED record so a diagnosis written against a
        # gated task is not lost. The original blanket `assertFalse` on content
        # encoded the destruction, which the PLS-722 ruling deliberately
        # overturned; the acceptance invariant it was protecting is asserted
        # explicitly below instead.
        self.assertFalse(
            Comment.objects.filter(
                item=second,
                content__icontains='later peer evidence',
                origin_kind=Comment.ORIGIN_USER_AUTHORED,
            ).exists()
        )
        preserved = Comment.objects.filter(
            item=second, origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
        ).first()
        self.assertIsNotNone(
            preserved,
            'the refused body must be preserved, not discarded (PLS-722) — and '
            'the MCP surface is the one every motivating incident came through',
        )
        self.assertEqual(
            (preserved.origin_payload or {}).get('refused_body'),
            'later peer evidence',
        )
        # Mechanical authorship is what keeps the record out of the activity
        # counters that read a comment as "this agent worked the item".
        self.assertEqual(preserved.author_email, 'workflow@pulse')

    def test_external_mcp_active_stage_can_record_settlement_evidence(self):
        """A newly assigned root cannot discard an already-started verdict."""
        self.make_focus_root('external settlement root')
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            active = Item.objects.create(
                title='external review already started',
                mode='task',
                project=self.project_a,
                status='in_review',
                priority='normal',
                workflow_name='autonomous-dev',
                assignee_id=10,
                assignee_email='owner@example.com',
                created_by_id=10,
            )
        auth = self.make_token(
            ['task.read', 'comment.write'],
            raw_token='pls561-comment-settlement',
        )

        response = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': str(active.pk), 'content': 'SHA-anchored verdict'},
            auth,
            idempotency_key='pls561-comment-settlement',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            Comment.objects.filter(
                item=active,
                content__icontains='SHA-anchored verdict',
            ).exists()
        )

    def test_external_mcp_transition_has_one_equal_root_fifo_leader(self):
        """External exact transitions cannot synthesize an equal-root cycle.

        PLS-846 semantics: the focus guard enforces a single leader while the
        elected root is genuinely UNSTARTED (AC-4). The guard does NOT keep
        refusing the second root once the elected leader has entered an active
        slot — at that point the leader is being worked, so starting a second
        root is governed by WIP-capacity policy (AC-2), not by the focus guard
        freezing the queue for the leader's whole lifetime.

        This test pins the unstarted half: two equal open roots, and only the
        FIFO leader may be started; the trailing one is focus-refused while the
        leader is still open.
        """
        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        progress_status, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress', 'position': 2},
        )
        workflow = Workflow.objects.create(
            name='PLS-561 MCP focus',
            slug='pls561-mcp-focus',
            initial_status=open_status,
            created_by_id=10,
            created_by_email='owner@example.com',
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=1,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=progress_status, position=2,
        )
        transition = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Start Progress',
            from_status='open',
            to_status='in_progress',
            position=1,
        )
        first = self.make_focus_root(
            'external transition first', workflow_name=workflow.slug,
        )
        second = self.make_focus_root(
            'external transition second', workflow_name=workflow.slug,
        )
        auth = self.make_token(
            ['task.read', 'transition.execute'], raw_token='pls561-transition-focus',
        )

        # Both roots are open/unstarted. Attempt the trailing (second) one
        # FIRST: with the elected FIFO leader still open, focus must refuse it.
        trailing_first = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': str(second.pk), 'transition_id': str(transition.id)},
            auth,
            idempotency_key='pls561-transition-trailing',
        )
        self.assertEqual(trailing_first.status_code, 400, trailing_first.content)
        self.assertIn('owned blocker root', trailing_first.json()['error'].lower())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, 'open')
        self.assertEqual(second.status, 'open')

        # Start the FIFO leader: must succeed (it is the elected root).
        leading = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': str(first.pk), 'transition_id': str(transition.id)},
            auth,
            idempotency_key='pls561-transition-leading',
        )
        self.assertEqual(leading.status_code, 200, leading.content)
        first.refresh_from_db()
        self.assertEqual(first.status, 'in_progress')

        # PLS-846: once the elected leader is in_progress, the focus guard no
        # longer freezes the second root. Starting it is no longer a
        # BLOCKER_FOCUS_REQUIRED refusal — it is capacity-governed in
        # production (this fixture configures no WIP cap, so it may proceed).
        trailing_after = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': str(second.pk), 'transition_id': str(transition.id)},
            auth,
            idempotency_key='pls561-transition-trailing-2',
        )
        self.assertEqual(
            trailing_after.status_code, 200,
            msg=(
                'once the elected root is active, focus must not keep refusing '
                f'the second equal root (PLS-846). got {trailing_after.content!r}'
            ),
        )
        second.refresh_from_db()
        self.assertEqual(second.status, 'in_progress')

    def test_external_focus_uses_token_projection_for_comment_and_transition(self):
        """PLS-826: owner email cannot widen an external token's queue.

        Production order mirrors the Ni recurrence boundary: read the visible
        target, publish one harmless comment, then execute its exact transition.
        A higher-impact same-owner root outside the token project scope must not
        be named, preempt either write, or leak through the read payload.
        """
        hidden_project = Project.objects.create(
            name='Tenant A hidden project', key='TAH', org_id=2,
        )
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            hidden_root = Item.objects.create(
                mode='task', project=hidden_project, item_key='TAH-1',
                title='hidden owner root', status='open', priority='urgent',
                workflow_name='simple', assignee_id=10,
                assignee_email='owner@example.com', created_by_id=10,
            )
            create_blocked_item(
                blocker=hidden_root,
                mode='task', project=hidden_project, item_key='TAH-2',
                title='hidden blocked descendant', status='blocked',
                priority='urgent', workflow_name='simple', assignee_id=10,
                assignee_email='owner@example.com', created_by_id=10,
            )

        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        progress_status, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress', 'position': 2},
        )
        workflow = Workflow.objects.create(
            name='PLS-826 scoped focus', slug='pls826-scoped-focus',
            initial_status=open_status, created_by_id=10,
            created_by_email='owner@example.com',
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=1,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=progress_status, position=2,
        )
        transition = WorkflowTransition.objects.create(
            workflow=workflow, name='Start Progress', from_status='open',
            to_status='in_progress', position=1,
        )
        self.item_a.workflow_name = workflow.slug
        self.item_a.status = 'open'
        self.item_a.workflow_status = 'open'
        self.item_a.priority = 'normal'
        self.item_a.assignee_id = 10
        self.item_a.assignee_email = 'owner@example.com'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item_a.save(update_fields=[
                'workflow_name', 'status', 'workflow_status', 'priority',
                'assignee_id', 'assignee_email',
            ])

        auth = self.make_token(
            ['task.read', 'comment.write', 'transition.execute'],
            raw_token='pls826-projection',
        )
        read = self.call_tool_with_auth(
            'pulse_get_task', {'task_id': 'TA-1'}, auth,
        )
        self.assertEqual(read.status_code, 200, read.content)
        self.assertNotIn('focus_guard', read.json()['result'])
        self.assertNotIn('TAH-1', read.content.decode())

        comment = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'harmless owner-path proof'},
            auth,
            idempotency_key='pls826-projection-comment',
        )
        self.assertEqual(comment.status_code, 200, comment.content)

        moved = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': 'TA-1', 'transition_id': str(transition.id)},
            auth,
            idempotency_key='pls826-projection-transition',
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.status, 'in_progress')

    def test_external_focus_excludes_future_due_recurring_root(self):
        """External comment and transition share the timer-owned predicate."""
        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        progress_status, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={
                'name': 'In Progress', 'category': 'in_progress', 'position': 2,
            },
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug='blocked',
            defaults={'name': 'Blocked', 'category': 'scheduled', 'position': 3},
        )
        workflow = Workflow.objects.create(
            name='PLS-826 future recurring focus',
            slug='pls826-future-recurring-focus',
            initial_status=open_status,
            created_by_id=10,
            created_by_email='owner@example.com',
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=1,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=progress_status, position=2,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=blocked_status, position=3,
        )
        transition = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Start Progress',
            from_status='open',
            to_status='in_progress',
            position=1,
        )
        recurring = self.make_focus_root(
            'future due recurring owner root', workflow_name=workflow.slug,
        )
        recurring.is_recurring = True
        recurring.frequency = 'daily'
        recurring.due_date = timezone.now() + timezone.timedelta(days=2)
        recurring.save(update_fields=['is_recurring', 'frequency', 'due_date'])
        self.assertTrue(ItemLink.objects.filter(
            source_item=recurring,
            target_item__status='blocked',
        ).exists())
        self.item_a.workflow_name = workflow.slug
        self.item_a.status = 'open'
        self.item_a.workflow_status = 'open'
        self.item_a.priority = 'normal'
        self.item_a.assignee_id = 10
        self.item_a.assignee_email = 'owner@example.com'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item_a.save(update_fields=[
                'workflow_name', 'status', 'workflow_status', 'priority',
                'assignee_id', 'assignee_email',
            ])

        auth = self.make_token(
            ['task.read', 'comment.write', 'transition.execute'],
            raw_token='pls826-future-recurring',
        )
        read = self.call_tool_with_auth(
            'pulse_get_task', {'task_id': 'TA-1'}, auth,
        )
        self.assertEqual(read.status_code, 200, read.content)
        self.assertNotIn('focus_guard', read.json()['result'])

        comment = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'future occurrence is not runnable'},
            auth,
            idempotency_key='pls826-future-recurring-comment',
        )
        self.assertEqual(comment.status_code, 200, comment.content)

        moved = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': 'TA-1', 'transition_id': str(transition.id)},
            auth,
            idempotency_key='pls826-future-recurring-transition',
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.status, 'in_progress')

    def test_external_focus_excludes_upstream_held_status_drift(self):
        """A stale runnable status cannot outrank its active upstream link."""
        upstream = self.make_focus_root('visible upstream root')
        middle = self.make_focus_root('status-drift middle')
        upstream.assignee_id = 99
        upstream.assignee_email = 'upstream-owner@example.com'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            upstream.save(update_fields=['assignee_id', 'assignee_email'])
        link = ItemLink(
            source_item=upstream,
            target_item=middle,
            link_type='blocks',
            created_by_id=10,
            created_by_email='owner@example.com',
        )
        link._skip_auto_block = True
        link.save()
        # Deliberately retain `open`: the link is authoritative even when the
        # automatic status mutation was interrupted.
        self.assertNotIn(
            middle.status,
            {'blocked', 'scheduled', 'waiting_external'},
        )

        self.item_a.assignee_id = 10
        self.item_a.assignee_email = 'owner@example.com'
        self.item_a.priority = 'urgent'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item_a.save(update_fields=[
                'assignee_id', 'assignee_email', 'priority',
            ])
        auth = self.make_token(
            ['task.read', 'comment.write'], raw_token='pls826-upstream-drift',
        )

        response = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'drift exclusion proof'},
            auth,
            idempotency_key='pls826-upstream-drift-comment',
        )
        self.assertEqual(response.status_code, 200, response.content)
        # Structured identity check only — bare str(pk) substring matching is
        # flaky because short numeric ids collide with ISO timestamp fractions
        # (e.g. "...59.213467..." contains "213").
        payload = response.json()
        task = payload['result']['task']
        self.assertEqual(task['item_key'], 'TA-1')
        self.assertEqual(str(task['id']), str(self.item_a.pk))
        self.assertNotEqual(str(task['id']), str(middle.pk))
        if middle.item_key:
            self.assertNotEqual(task.get('item_key'), middle.item_key)
        self.assertNotIn('focus_guard', payload.get('result', {}))

    def test_create_task_enforces_duplicate_review_and_idempotency(self):
        auth = self.make_token(['task.read', 'task.write'], raw_token='pls308-create')
        duplicate = self.call_tool_with_auth(
            'pulse_create_task',
            {'project_id': self.project_a.id, 'title': 'visible task', 'description': 'new'},
            auth,
            idempotency_key='create-dup',
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self.assertEqual(duplicate.json()['error'], 'duplicate_candidates_found')

        first = self.call_tool_with_auth(
            'pulse_create_task',
            {
                'project_id': self.project_a.id,
                'title': 'new external task',
                'description': 'from tool',
                'duplicate_acknowledged': True,
            },
            auth,
            idempotency_key='create-1',
        )
        self.assertEqual(first.status_code, 200, first.content)
        item_key = first.json()['result']['item_key']
        self.assertEqual(Item.objects.filter(title='new external task').count(), 1)
        self.assertTrue(Comment.objects.filter(item__item_key=item_key, content__icontains='Created task via scoped external write tool').exists())

        replay = self.call_tool_with_auth(
            'pulse_create_task',
            {
                'project_id': self.project_a.id,
                'title': 'new external task',
                'description': 'from tool',
                'duplicate_acknowledged': True,
            },
            auth,
            idempotency_key='create-1',
        )
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertTrue(replay.json()['result']['idempotent_replay'])
        self.assertEqual(replay.json()['result']['task']['item_key'], item_key)
        self.assertEqual(Item.objects.filter(title='new external task').count(), 1)

    def test_update_task_rejects_direct_status_mutation(self):
        # Capture the actual persisted status: the workflow engine normalizes the
        # create-time status to the workflow's canonical initial state (e.g. the
        # 'simple' workflow maps a submitted 'open' → 'todo'), so hardcoding 'open'
        # here is stale. The test's real intent is that a REJECTED direct mutation
        # leaves the status UNCHANGED — assert exactly that.
        self.item_a.refresh_from_db()
        status_before = self.item_a.status
        auth = self.make_token(['task.read', 'task.write'], raw_token='pls308-update')
        response = self.call_tool_with_auth(
            'pulse_update_task',
            {'task_id': 'TA-1', 'status': 'done'},
            auth,
            idempotency_key='update-status',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()['error'], 'status_update_forbidden')
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.status, status_before)
        self.assertNotEqual(self.item_a.status, 'done')

    def test_transition_write_uses_workflow_engine_validators(self):
        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        done_status, _ = Status.objects.get_or_create(
            slug='done',
            defaults={'name': 'Done', 'category': 'done', 'position': 2},
        )
        workflow = Workflow.objects.create(
            name='External MCP Test Workflow',
            slug='external-mcp-test-workflow',
            initial_status=open_status,
            created_by_id=1,
            created_by_email='a@example.com',
        )
        WorkflowStatus.objects.create(workflow=workflow, status=open_status, position=1)
        WorkflowStatus.objects.create(workflow=workflow, status=done_status, position=2)
        transition = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Assignee-only Done',
            from_status='open',
            to_status='done',
            assignee_only=True,
            position=1,
        )
        self.item_a.workflow_name = workflow.slug
        self.item_a.status = 'open'
        self.item_a.workflow_status = 'open'
        self.item_a.assignee_id = 999
        self.item_a.assignee_email = 'someone-else@example.com'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item_a.save(
                update_fields=[
                    'workflow_name',
                    'status',
                    'workflow_status',
                    'assignee_id',
                    'assignee_email',
                ],
            )

        auth = self.make_token(['task.read', 'transition.execute'], raw_token='pls308-transition')
        denied = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': 'TA-1', 'transition_id': str(transition.id)},
            auth,
            idempotency_key='transition-denied',
        )
        self.assertEqual(denied.status_code, 400, denied.content)
        self.assertIn('permission', denied.json()['error'].lower())
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.status, 'open')

        self.item_a.assignee_id = 10
        self.item_a.assignee_email = 'owner@example.com'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item_a.save(update_fields=['assignee_id', 'assignee_email'])
        ok = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': 'TA-1', 'transition_id': str(transition.id)},
            auth,
            idempotency_key='transition-ok',
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.status, 'done')
        self.assertTrue(Comment.objects.filter(item=self.item_a, content__icontains='Transitioned to `done` through workflow engine').exists())

    def test_write_policy_requires_confirmation_and_blocks_draft_transitions(self):
        auth = self.make_token(
            ['task.read', 'comment.write', 'transition.execute'],
            raw_token='pls308-policy',
            rate_limit_policy={'per_minute': 100, 'write_policy': {'require_confirmation': True, 'draft_only': True}},
        )
        response = self.call_tool_with_auth(
            'pulse_add_comment',
            {'task_id': 'TA-1', 'content': 'needs confirmation'},
            auth,
            idempotency_key='policy-comment',
        )
        self.assertEqual(response.status_code, 428, response.content)
        self.assertEqual(response.json()['error'], 'confirmation_required')

        transition = self.call_tool_with_auth(
            'pulse_execute_transition',
            {'task_id': 'TA-1', 'to_status': 'done', 'confirmed': True},
            auth,
            idempotency_key='policy-transition',
        )
        self.assertEqual(transition.status_code, 403, transition.content)
        self.assertEqual(transition.json()['error'], 'draft_only_policy_denied')

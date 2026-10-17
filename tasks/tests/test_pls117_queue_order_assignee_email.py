"""
PLS-117: pulse_get_my_tasks (and pulse_list_tasks with assignee_email) must not
return HTTP 500 when queue_order=true is combined with assignee_email on the
/items/ list endpoint.

Root cause investigation: non-privileged agent users trigger the full
permission-resolver path (DISTINCT + complex subquery base_q) in
get_user_item_queryset. The queue_order=true annotation (Case/When on that
DISTINCT queryset) must produce valid SQL in PostgreSQL for all user types —
superuser, staff, and unprivileged agent.

Repro scenario (PLS-117):
  Kei (non-staff, non-superuser agent) calls pulse_get_my_tasks.
  MCP server hits: /api/items/?mode=task&assignee_email=kei@shizuha.com
                   &page_size=20&queue_order=true&is_active=true
  Django returns 500 HTML.

The working path (no 500):
  /api/items/?mode=task&assignee_email=kei@shizuha.com
             &page_size=5&is_active=true  (no queue_order)  → 200 {count:0}

Acceptance criteria:
  - Non-privileged user + queue_order=true + is_active=true → 200, not 500.
  - Empty result queue (no assigned tasks) → 200 {count:0, results:[]}.
  - Non-empty result queue is ordered by priority then FIFO.
  - Staff and superuser paths are unchanged.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import AgentProjectMembership, Item, ItemLink, Project, Status
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from .base import create_blocked_item, satisfy_blocked_invariant

User = get_user_model()

ITEMS_URL = '/api/items/'


class QueueOrderAssigneeEmailNonPrivilegedTests(APITestCase):
    """
    Regression test for PLS-117.

    A non-staff, non-superuser agent user must get HTTP 200 (not 500)
    from the /items/ list endpoint when queue_order=true is combined
    with assignee_email= and is_active=true.
    """

    def setUp(self):
        self.client = APIClient()
        # The failing user is a non-privileged agent (not staff, not superuser)
        self.agent = User.objects.create_user(
            username='kei_test', email='kei_test@shizuha.com', password='x',
            is_staff=False, is_superuser=False,
        )
        self.client.force_authenticate(user=self.agent)

    def _item(self, title, **kw):
        kw.setdefault('workflow_name', 'autonomous-bug')
        factory = create_blocked_item if kw.get('status') == 'blocked' else Item.objects.create
        return factory(
            title=title, mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            **kw,
        )

    def _url(self, email=None, **extra_params):
        params = dict(
            mode='task',
            assignee_email=email or self.agent.email,
            page_size=20,
            queue_order='true',
            is_active='true',
        )
        # Allow callers to remove a default key by passing it as None.
        params.update({k: v for k, v in extra_params.items() if v is not None})
        for k, v in extra_params.items():
            if v is None:
                params.pop(k, None)
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        return f'{ITEMS_URL}?{qs}'

    def test_empty_queue_returns_200_not_500(self):
        """Core regression: non-privileged user with NO tasks must get 200."""
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 0)
        self.assertEqual(resp.data['results'], [])

    def test_non_empty_queue_returns_200(self):
        """Non-privileged user with tasks assigned must get 200 with results."""
        self._item('task-a', status='open', priority='normal')
        self._item('task-b', status='in_progress', priority='high')

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 2)
        titles = [it['title'] for it in resp.data['results']]
        self.assertIn('task-a', titles)
        self.assertIn('task-b', titles)

    def test_agent_alias_queue_item_visible_when_assignee_id_drifted(self):
        """PLS-382/SCLI-312: L3 owner scope must expand agent email aliases.

        In prod, agents authenticate as <name>@shizuha.com but assignment paths
        can store <name>@agents.shizuha.io, often with assignee_id NULL or
        drifted to an old local Pulse user. The /items/ queue endpoint filters
        by assignee_email after applying PermissionService.get_user_item_queryset;
        both layers must agree that the two email forms are one identity.
        """
        Item.objects.create(
            title='alias-task', mode='task',
            assignee_id=None,
            assignee_email='kei_test@agents.shizuha.io',
            status='open',
            priority='urgent',
            workflow_name='autonomous-bug',
        )

        resp = self.client.get(self._url(email='kei_test@shizuha.com'))
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertIn('alias-task', titles)

    def test_company_domain_queue_item_visible_to_agent_alias_identity(self):
        """Inverse alias direction: @agents.shizuha.io JWT sees @shizuha.com tasks."""
        alias_user = User.objects.create_user(
            username='alias_kei',
            email='alias_kei@agents.shizuha.io',
            password='x',
            is_staff=False,
            is_superuser=False,
        )
        self.client.force_authenticate(user=alias_user)
        Item.objects.create(
            title='company-domain-task', mode='task',
            assignee_id=None,
            assignee_email='alias_kei@shizuha.com',
            status='open',
            priority='urgent',
            workflow_name='autonomous-bug',
        )

        resp = self.client.get(self._url(email='alias_kei@agents.shizuha.io'))
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertIn('company-domain-task', titles)

    def test_queue_order_priority_respected(self):
        """High-priority ready task ranks before normal-priority ready task."""
        self._item('normal-open', status='open', priority='normal')
        self._item('high-open', status='open', priority='high')

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertLess(
            titles.index('high-open'), titles.index('normal-open'),
            f"high-priority task should rank before normal: {titles}",
        )

    def test_root_blocker_inherits_transitive_descendant_priority(self):
        """Runnable root gets the highest priority in its blocked graph."""
        root = self._item('root-normal', status='open', priority='normal')
        middle = create_blocked_item(
            blocker=root,
            title='middle-normal-blocked', mode='task',
            assignee_id=991, assignee_email='other-middle@example.com',
            status='blocked', priority='normal', workflow_name='autonomous-bug',
        )
        create_blocked_item(
            blocker=middle,
            title='leaf-urgent-blocked', mode='task',
            assignee_id=992, assignee_email='other-leaf@example.com',
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        self._item('unrelated-high', status='open', priority='high')

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertEqual(titles[:2], ['root-normal', 'unrelated-high'])

    def test_blocker_root_precedes_same_priority_closer_stage(self):
        """A resumed near-done task cannot displace owned release work."""
        root = self._item('root-urgent', status='open', priority='urgent')
        create_blocked_item(
            blocker=root,
            title='blocked-child',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )
        self._item(
            'unrelated-review',
            status='in_review',
            priority='urgent',
        )

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertEqual(titles[:2], ['root-urgent', 'unrelated-review'])

    def test_priority_precedes_blocker_root_rank(self):
        """A blocker root is escalated to urgent and wins the blocker-root
        tie-break so it can never be hidden behind unrelated runnable work."""
        root = self._item('high-root', status='open', priority='high')
        create_blocked_item(
            blocker=root,
            title='high-root-dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='high',
            workflow_name='autonomous-bug',
        )
        self._item('urgent-ordinary', status='open', priority='urgent')

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200, response.content)
        titles = [item['title'] for item in response.data['results']]
        # Operator directive 2026-08-08: the blocker root was escalated to
        # stored-urgent (it releases active blocked work), so it must not rank
        # *after* an unrelated urgent task — being a blocker root it wins the
        # equal-priority tie-break and stays visible at the top of the lane.
        self.assertLess(titles.index('high-root'), titles.index('urgent-ordinary'))
        root.refresh_from_db()
        self.assertEqual(root.priority, 'urgent')

    def test_native_ready_comment_is_focus_guarded(self):
        """The native MCP /comments/ path cannot start unrelated ready work."""
        root = self._item('native-comment-root', status='open', priority='urgent')
        create_blocked_item(
            blocker=root,
            title='native-comment-dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )
        lower = self._item(
            'native-comment-lower',
            status='open',
            priority='normal',
        )

        response = self.client.post(
            '/api/comments/',
            {'item': lower.pk, 'content': 'continuing lower task'},
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data['error'], 'blocker_focus_required')
        self.assertEqual(
            response.data['required_task_key'],
            root.item_key or str(root.pk),
        )

    def test_native_focus_does_not_cross_authorized_org_projection(self):
        """PLS-826: native comments consume the same visible owner queue."""
        self.agent.organization_memberships = {826: 'member'}
        visible_project = Project.objects.create(
            name='PLS-826 visible', key='P826V', org_id=826,
            access_level='open',
        )
        hidden_project = Project.objects.create(
            name='PLS-826 hidden', key='P826H', org_id=827,
            access_level='private',
        )
        with mock.patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            target = Item.objects.create(
                title='visible ordinary target', mode='task', project=visible_project,
                status='open', priority='normal', workflow_name='autonomous-bug',
            )
            hidden_root = Item.objects.create(
                title='foreign hidden root', mode='task', project=hidden_project,
                status='open', priority='urgent', workflow_name='autonomous-bug',
            )
            hidden_child = create_blocked_item(
                blocker=hidden_root,
                title='foreign hidden dependent', mode='task',
                project=hidden_project, status='blocked', priority='urgent',
                workflow_name='autonomous-bug',
            )
        # Install the historical same-email owners with the exact org-fence
        # proof. This deliberately does not create a hidden-org membership:
        # the fixture needs a legacy row the actor owns by key but cannot see.
        with connection.cursor() as cursor:
            for item in (target, hidden_root, hidden_child):
                if connection.vendor == 'postgresql':
                    cursor.execute(
                        "SELECT set_config('pulse.assignment_org_proof', "
                        "concat_ws('|', %s::text, %s::text, %s::text, "
                        "tasks_canonical_assignment_email(%s), "
                        "tasks_canonical_team_key(%s)), true)",
                        [
                            item.project.org_id,
                            item.project_id,
                            self.agent.id,
                            self.agent.email,
                            item.assignment_group or '',
                        ],
                    )
                cursor.execute(
                    "UPDATE tasks_task SET assignee_id=%s, assignee_email=%s "
                    "WHERE id=%s",
                    [self.agent.id, self.agent.email, item.pk],
                )
        target.refresh_from_db()
        hidden_root.refresh_from_db()
        hidden_child.refresh_from_db()
        self.assertEqual(hidden_child.status, 'blocked')

        response = self.client.post(
            '/api/comments/',
            {'item': target.pk, 'content': 'visible progress evidence'},
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.content)
        # Exact-value projection check, not raw substring: a substring
        # assertNotIn flakes when id sequences collide (needle 407 matches
        # inside "id":4076) while nothing actually leaked. Walk the serialized
        # payload and assert the hidden item's key/id is not any serialized
        # value in it.
        needle = hidden_root.item_key or str(hidden_root.pk)
        serialized_values = []
        def _collect(node):
            if isinstance(node, dict):
                for value in node.values():
                    _collect(value)
            elif isinstance(node, list):
                for item_value in node:
                    _collect(item_value)
            else:
                serialized_values.append(str(node))
        _collect(mock_response_json := response.json())
        del mock_response_json
        self.assertNotIn(needle, serialized_values)

    def test_upstream_held_status_drift_is_waiting_awareness(self):
        """Queue and guard agree that an active upstream link is not runnable."""
        upstream = self._item(
            'upstream root', status='open', priority='normal',
        )
        drifted = self._item(
            'stale-open dependent', status='open', priority='urgent',
        )
        link = ItemLink(
            source_item=upstream,
            target_item=drifted,
            link_type='blocks',
            created_by_id=self.agent.id,
            created_by_email=self.agent.email,
        )
        link._skip_auto_block = True
        link.save()
        runnable = self._item(
            'ordinary runnable', status='open', priority='high',
        )

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.content)
        ids = [row['id'] for row in response.data['results']]
        self.assertLess(ids.index(runnable.pk), ids.index(drifted.pk))

    def test_scheduled_category_slug_is_waiting_and_cannot_focus_pin(self):
        """A non-literal scheduled status shares queue/focus classification."""
        Status.objects.get_or_create(
            slug='snoozed',
            defaults={'name': 'Snoozed', 'category': 'scheduled'},
        )
        root = self._item('snoozed root', status='open', priority='urgent')
        create_blocked_item(
            blocker=root,
            title='snoozed root dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )
        Item.objects.filter(pk=root.pk).update(
            status='snoozed', workflow_status='snoozed',
        )
        try:
            runnable = self._item(
                'ordinary category-aware runnable',
                status='open',
                priority='normal',
            )

            queue = self.client.get(self._url())
            self.assertEqual(queue.status_code, 200, queue.content)
            ids = [row['id'] for row in queue.data['results']]
            self.assertIn(root.pk, ids)
            self.assertLess(ids.index(runnable.pk), ids.index(root.pk))

            comment = self.client.post(
                '/api/comments/',
                {'item': runnable.pk, 'content': 'scheduled category does not pin'},
                format='json',
            )
            self.assertEqual(comment.status_code, 201, comment.content)
            self.assertTrue(WorkflowService.validate_transition(
                runnable,
                'in_progress',
                user=self.agent,
            ))
        finally:
            # Restore the deliberately-corrupt parked root before the deferred
            # PostgreSQL invariant checks this TestCase transaction.
            Item.objects.filter(pk=root.pk).update(
                status='open', workflow_status='open',
            )

    def test_open_task_with_linked_pr_is_waiting_not_focus_root(self):
        """Authored implementation waits on Review/Merge despite stale open."""
        root = self._item('authored stale-open root', status='open', priority='urgent')
        root.metadata = {
            'pull_requests': [
                {
                    'url': 'https://origin.shizuha.com/shizuha-labs/example/pulls/8',
                    'status': 'merged',
                    'head_sha': 'historical-head',
                },
                {
                    'url': 'https://origin.shizuha.com/shizuha-labs/example/pulls/9',
                    'status': 'open',
                    'head_sha': 'abc123',
                },
            ],
        }
        root.save(update_fields=['metadata'])
        create_blocked_item(
            blocker=root,
            title='authored stale-open dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )
        runnable = self._item(
            'ordinary work while PR waits', status='open', priority='normal',
        )

        queue = self.client.get(self._url())
        self.assertEqual(queue.status_code, 200, queue.content)
        ids = [row['id'] for row in queue.data['results']]
        self.assertLess(ids.index(runnable.pk), ids.index(root.pk))

        comment = self.client.post(
            '/api/comments/',
            {'item': runnable.pk, 'content': 'linked PR belongs to review now'},
            format='json',
        )
        self.assertEqual(comment.status_code, 201, comment.content)

    def test_future_due_recurring_root_is_shared_waiting_for_queue_and_focus(self):
        """Timer-owned open occurrences cannot preempt either native write."""
        root = self._item(
            'future recurring root', status='open', priority='urgent',
            is_recurring=True, frequency='daily',
            due_date=timezone.now() + timedelta(days=2),
        )
        create_blocked_item(
            blocker=root,
            title='future recurring dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )
        runnable = self._item(
            'ordinary work before recurring boundary',
            status='open',
            priority='normal',
        )

        queue = self.client.get(self._url())
        self.assertEqual(queue.status_code, 200, queue.content)
        ids = [row['id'] for row in queue.data['results']]
        self.assertLess(ids.index(runnable.pk), ids.index(root.pk))

        comment = self.client.post(
            '/api/comments/',
            {'item': runnable.pk, 'content': 'future occurrence is timer-owned'},
            format='json',
        )
        self.assertEqual(comment.status_code, 201, comment.content)
        self.assertTrue(WorkflowService.validate_transition(
            runnable,
            'in_progress',
            user=self.agent,
        ))

    def test_terminal_or_historical_pr_metadata_keeps_open_root_runnable(self):
        """Only an active PR hands work away from its current owner."""
        for pr_status in ('merged', 'closed', None):
            with self.subTest(pr_status=pr_status or 'historical-no-status'):
                pr = {
                    'url': 'https://origin.shizuha.com/shizuha-labs/example/pulls/9',
                    'head_sha': 'terminal-head',
                }
                if pr_status is not None:
                    pr['status'] = pr_status
                root = self._item(
                    f'{pr_status or "historical"} PR root',
                    status='open',
                    priority='urgent',
                )
                root.metadata = {'pull_requests': [pr]}
                root.save(update_fields=['metadata'])
                create_blocked_item(
                    blocker=root,
                    title='terminal PR dependent',
                    mode='task',
                    assignee_id=self.agent.id,
                    assignee_email=self.agent.email,
                    status='blocked',
                    priority='urgent',
                    workflow_name='autonomous-bug',
                )
                ordinary = self._item(
                    'ordinary lower-impact work',
                    status='open',
                    priority='normal',
                )

                queue = self.client.get(self._url())
                self.assertEqual(queue.status_code, 200, queue.content)
                self.assertEqual(queue.data['results'][0]['id'], root.pk)

                comment = self.client.post(
                    '/api/comments/',
                    {'item': ordinary.pk, 'content': 'must stay behind open root'},
                    format='json',
                )
                self.assertEqual(comment.status_code, 400, comment.content)
                self.assertEqual(comment.data['error'], 'blocker_focus_required')
                self.assertEqual(
                    comment.data['required_task_key'],
                    root.item_key or str(root.pk),
                )
                with self.assertRaises(WorkflowValidationError) as raised:
                    WorkflowService.validate_transition(
                        ordinary,
                        'in_progress',
                        user=self.agent,
                    )
                self.assertEqual(raised.exception.code, 'BLOCKER_FOCUS_REQUIRED')

                ItemLink.objects.all().delete()
                Item.objects.all().delete()

    def test_new_owned_root_after_queue_snapshot_returns_refresh_diagnostic(self):
        """PLS-568: a post-snapshot root is typed as queue drift, not a lie."""
        lower = self._item(
            'selected-ordinary-task',
            status='open',
            priority='normal',
        )
        selected = self.client.get(self._url())
        self.assertEqual(selected.status_code, 200, selected.content)
        self.assertEqual(
            [row['id'] for row in selected.data['results']],
            [lower.pk],
        )

        root = self._item(
            'returned-jun-owned-root',
            status='open',
            priority='urgent',
        )
        create_blocked_item(
            blocker=root,
            title='root-dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )

        response = self.client.post(
            '/api/comments/',
            {'item': lower.pk, 'content': 'submit selected task'},
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data['error'], 'focus_root_changed')
        self.assertEqual(response.data['code'], 'FOCUS_ROOT_CHANGED')
        self.assertTrue(response.data['refresh_required'])
        self.assertEqual(response.data['refresh_tool'], 'pulse_get_my_tasks')
        self.assertEqual(
            response.data['required_task_key'],
            root.item_key or str(root.pk),
        )

        with self.assertRaises(WorkflowValidationError) as raised:
            WorkflowService.validate_transition(
                lower,
                'in_progress',
                user=self.agent,
            )
        self.assertEqual(raised.exception.code, 'FOCUS_ROOT_CHANGED')
        self.assertTrue(raised.exception.details['refresh_required'])

        refreshed = self.client.get(self._url())
        self.assertEqual(refreshed.status_code, 200, refreshed.content)
        self.assertEqual(
            refreshed.data['results'][0]['id'],
            root.pk,
        )

    def test_other_user_cannot_replace_owner_queue_snapshot(self):
        """PLS-568: diagnostics cannot be poisoned through cross-owner reads."""
        lower = self._item(
            'owner-snapshot-task',
            status='in_progress',
            priority='normal',
        )
        selected = self.client.get(self._url())
        self.assertEqual(selected.status_code, 200, selected.content)
        key = WorkflowService._queue_snapshot_cache_key(self.agent.email)
        self.assertEqual(cache.get(key)['item_ids'], [lower.pk])

        stranger = User.objects.create_user(
            username='snapshot_stranger',
            email='snapshot_stranger@shizuha.com',
            password='x',
        )
        self.client.force_authenticate(user=stranger)
        attempted = self.client.get(self._url(email=self.agent.email))

        self.assertEqual(attempted.status_code, 200, attempted.content)
        self.assertEqual(cache.get(key)['item_ids'], [lower.pk])

    def test_narrowed_owner_read_cannot_replace_unfiltered_queue_snapshot(self):
        """PLS-568: project/team/workflow filters are not scheduler snapshots."""
        lower = self._item(
            'canonical-owner-snapshot-task',
            status='in_progress',
            priority='normal',
        )
        selected = self.client.get(self._url())
        self.assertEqual(selected.status_code, 200, selected.content)
        key = WorkflowService._queue_snapshot_cache_key(self.agent.email)
        self.assertEqual(cache.get(key)['item_ids'], [lower.pk])

        narrowed = self.client.get(self._url(assignment_group='qa'))

        self.assertEqual(narrowed.status_code, 200, narrowed.content)
        self.assertEqual(narrowed.data['results'], [])
        self.assertEqual(cache.get(key)['item_ids'], [lower.pk])

    def test_native_active_stage_can_record_settlement_evidence(self):
        """A root arriving mid-review cannot erase the review verdict."""
        root = self._item('native-settlement-root', status='open', priority='urgent')
        create_blocked_item(
            blocker=root,
            title='native-settlement-dependent',
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked',
            priority='urgent',
            workflow_name='autonomous-bug',
        )
        active = self._item(
            'native-review-already-started',
            status='in_review',
            priority='normal',
        )

        response = self.client.post(
            '/api/comments/',
            {'item': active.pk, 'content': 'SHA-anchored request-changes verdict'},
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.content)

    def test_blocked_middle_root_does_not_focus_pin_native_comment(self):
        """A root with an active upstream dependency is not runnable focus."""
        upstream = Item.objects.create(
            title='upstream-root', mode='task', assignee_email='upstream@example.com',
            status='open', priority='urgent', workflow_name='autonomous-bug',
        )
        middle = create_blocked_item(
            blocker=upstream,
            title='owned-middle', mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        create_blocked_item(
            blocker=middle,
            title='owned-leaf', mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        runnable = self._item('runnable-evidence', status='open', priority='normal')

        response = self.client.post(
            '/api/comments/',
            {'item': runnable.pk, 'content': 'publish while middle waits'},
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.content)

    def test_status_drifted_middle_with_upstream_does_not_focus_pin(self):
        """Active-upstream exclusion survives a stale runnable status value."""
        upstream = Item.objects.create(
            title='drift-upstream', mode='task', assignee_email='upstream@example.com',
            status='open', priority='urgent', workflow_name='autonomous-bug',
        )
        middle = create_blocked_item(
            blocker=upstream,
            title='drift-middle', mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        create_blocked_item(
            blocker=middle,
            title='drift-leaf', mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        # Simulate an interrupted legacy auto-block write. The dependency link,
        # not the stale status field, remains the authoritative upstream hold.
        Item.objects.filter(pk=middle.pk).update(status='open', workflow_status='open')
        runnable = self._item('drift-runnable', status='open', priority='normal')

        response = self.client.post(
            '/api/comments/',
            {'item': runnable.pk, 'content': 'status drift cannot freeze writes'},
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.content)

    def test_equal_urgent_roots_have_one_native_comment_leader(self):
        """Full-key FIFO makes one root writable instead of a reciprocal cycle."""
        first = self._item('first-urgent-root', status='open', priority='urgent')
        create_blocked_item(
            blocker=first,
            title='first-dependent', mode='task',
            assignee_id=self.agent.id, assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        second = self._item('second-urgent-root', status='open', priority='urgent')
        create_blocked_item(
            blocker=second,
            title='second-dependent', mode='task',
            assignee_id=self.agent.id, assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )

        leading = self.client.post(
            '/api/comments/',
            {'item': first.pk, 'content': 'queue leader remains writable'},
            format='json',
        )
        trailing = self.client.post(
            '/api/comments/',
            {'item': second.pk, 'content': 'later peer waits'},
            format='json',
        )

        self.assertEqual(leading.status_code, 201, leading.content)
        self.assertEqual(trailing.status_code, 400, trailing.content)
        self.assertEqual(trailing.data['required_task_key'], first.item_key or str(first.pk))

    def test_inherited_target_priority_uses_same_transition_key(self):
        """The target is ranked by descendant impact, not stored priority."""
        first = self._item('inherited-first-root', status='open', priority='normal')
        create_blocked_item(
            blocker=first,
            title='inherited-urgent-dependent', mode='task',
            assignee_id=self.agent.id, assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )
        second = self._item('stored-urgent-second-root', status='open', priority='urgent')
        create_blocked_item(
            blocker=second,
            title='stored-urgent-dependent', mode='task',
            assignee_id=self.agent.id, assignee_email=self.agent.email,
            status='blocked', priority='urgent', workflow_name='autonomous-bug',
        )

        self.assertIsNotNone(WorkflowService.validate_transition(first, 'in_progress'))
        with self.assertRaisesRegex(WorkflowValidationError, 'must be worked first'):
            WorkflowService.validate_transition(second, 'in_progress')

    def test_queue_order_waiting_ranks_below_ready(self):
        """Blocked task ranks below a lower-priority ready task."""
        satisfy_blocked_invariant(
            self._item('blocked-urgent', status='blocked', priority='urgent')
        )
        self._item('ready-normal', status='open', priority='normal')

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertIn('blocked-urgent', titles)
        self.assertIn('ready-normal', titles)
        self.assertLess(
            titles.index('ready-normal'), titles.index('blocked-urgent'),
            f"ready normal task must rank above blocked urgent: {titles}",
        )

    def test_queue_order_without_is_active_also_200(self):
        """queue_order=true alone (no is_active) must also return 200."""
        self._item('some-task', status='open', priority='normal')
        resp = self.client.get(self._url(is_active=None))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_is_active_filters_terminal_tasks(self):
        """With is_active=true, terminal-status tasks must be excluded."""
        self._item('open-task', status='open', priority='normal')
        self._item('done-task', status='completed', priority='urgent')

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertIn('open-task', titles)
        self.assertNotIn('done-task', titles)

    def test_stage_rank_awaiting_merge_before_in_progress(self):
        """PLS stop-starting rule: awaiting_merge ranks above in_progress."""
        self._item('in-progress', status='in_progress', priority='normal')
        self._item('awaiting-merge', status='awaiting_merge', priority='normal')

        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['results']]
        self.assertLess(
            titles.index('awaiting-merge'), titles.index('in-progress'),
            f"awaiting_merge must rank above in_progress at same priority: {titles}",
        )


class ProjectAffinityQueueOrderTests(APITestCase):
    """PLAT-1224 / EVOL-20 Ph1a: feature-flagged project-affinity read ordering."""

    def setUp(self):
        self.client = APIClient()
        self.agent = User.objects.create_user(
            username='nagi_test', email='nagi_test@shizuha.com', password='x',
            is_staff=False, is_superuser=False,
        )
        self.agent.organization_memberships = {1: 'member'}
        self.client.force_authenticate(user=self.agent)
        self.primary = Project.objects.create(name='Hive', key='HIVE', org_id=1, access_level='open')
        self.secondary = Project.objects.create(name='Platform', key='PLAT', org_id=1, access_level='open')
        self.overflow = Project.objects.create(name='Drive', key='DRV', org_id=1, access_level='open')
        assignment_authorization = mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'fixture-org', 'fixture-org'),
        )
        assignment_authorization.start()
        self.addCleanup(assignment_authorization.stop)

    def _url(self, **extra_params):
        params = dict(
            mode='task',
            assignee_email=self.agent.email,
            page_size=20,
            queue_order='true',
            is_active='true',
        )
        params.update(extra_params)
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        return f'{ITEMS_URL}?{qs}'

    def _item(self, title, project, **kw):
        kw.setdefault('workflow_name', 'autonomous-dev')
        factory = create_blocked_item if kw.get('status') == 'blocked' else Item.objects.create
        return factory(
            title=title,
            mode='task',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
            project=project,
            **kw,
        )

    def _membership(self, project, tier):
        return AgentProjectMembership.objects.create(
            project=project,
            agent_user_id=self.agent.id,
            agent_user_email=self.agent.email,
            tier=tier,
            active=True,
            reason='PLAT-1224 test fixture',
        )

    def _titles(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200, resp.content)
        return [it['title'] for it in resp.data['results']]

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=False)
    def test_feature_flag_off_preserves_existing_priority_order(self):
        self._membership(self.primary, 'primary')
        self._membership(self.secondary, 'secondary')
        self._item('primary-normal', self.primary, status='open', priority='normal')
        self._item('secondary-urgent', self.secondary, status='open', priority='urgent')

        titles = self._titles()
        self.assertLess(titles.index('secondary-urgent'), titles.index('primary-normal'))

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_no_memberships_preserves_existing_priority_order(self):
        self._item('primary-normal', self.primary, status='open', priority='normal')
        self._item('secondary-urgent', self.secondary, status='open', priority='urgent')

        titles = self._titles()
        self.assertLess(titles.index('secondary-urgent'), titles.index('primary-normal'))

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_priority_precedes_affinity_and_affinity_breaks_priority_ties(self):
        self._membership(self.primary, 'primary')
        self._membership(self.secondary, 'secondary')
        self._item('overflow-urgent', self.overflow, status='open', priority='urgent')
        self._item('secondary-urgent', self.secondary, status='open', priority='urgent')
        self._item('primary-normal', self.primary, status='open', priority='normal')

        titles = self._titles()
        self.assertLess(titles.index('secondary-urgent'), titles.index('primary-normal'))
        self.assertLess(titles.index('secondary-urgent'), titles.index('overflow-urgent'))
        # But BOTH urgent tasks outrank the normal primary task.
        self.assertLess(titles.index('secondary-urgent'), titles.index('primary-normal'))
        self.assertLess(titles.index('overflow-urgent'), titles.index('primary-normal'))

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_pls512_urgent_overflow_outranks_every_lower_priority_ready_item(self):
        # Live-proof shape from PLS-512: an urgent task outside every membership
        # project must surface FIRST across the whole ready queue.
        self._membership(self.primary, 'primary')
        self._membership(self.secondary, 'secondary')
        self._item('primary-high', self.primary, status='open', priority='high')
        self._item('primary-normal', self.primary, status='open', priority='normal')
        self._item('secondary-high', self.secondary, status='open', priority='high')
        self._item('overflow-urgent', self.overflow, status='open', priority='urgent')

        titles = self._titles()
        self.assertEqual(titles[0], 'overflow-urgent')

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_pls512_waiting_urgent_does_not_outrank_ready_work(self):
        # Preserve blocked/future-due demotion: a blocked urgent primary task
        # still ranks below ANY ready item regardless of the new priority-first
        # ordering.
        self._membership(self.primary, 'primary')
        satisfy_blocked_invariant(
            self._item('primary-urgent-blocked', self.primary, status='blocked', priority='urgent')
        )
        self._item('overflow-low-ready', self.overflow, status='open', priority='low')

        titles = self._titles()
        self.assertLess(
            titles.index('overflow-low-ready'), titles.index('primary-urgent-blocked')
        )

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_ready_order_remains_global_before_affinity_to_avoid_idle(self):
        self._membership(self.primary, 'primary')
        self._membership(self.secondary, 'secondary')
        satisfy_blocked_invariant(
            self._item('primary-blocked', self.primary, status='blocked', priority='urgent')
        )
        self._item('secondary-ready', self.secondary, status='open', priority='low')

        titles = self._titles()
        self.assertLess(titles.index('secondary-ready'), titles.index('primary-blocked'))

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_priority_and_fifo_preserved_inside_affinity_bucket(self):
        self._membership(self.primary, 'primary')
        older = self._item('primary-normal-old', self.primary, status='open', priority='normal')
        newer = self._item('primary-normal-new', self.primary, status='open', priority='normal')
        self._item('primary-high', self.primary, status='open', priority='high')
        older_time = timezone.now() - timedelta(hours=2)
        newer_time = timezone.now() - timedelta(hours=1)
        Item.objects.filter(pk=older.pk).update(created_at=older_time)
        Item.objects.filter(pk=newer.pk).update(created_at=newer_time)

        titles = self._titles()
        self.assertLess(titles.index('primary-high'), titles.index('primary-normal-old'))
        self.assertLess(titles.index('primary-normal-old'), titles.index('primary-normal-new'))

    @override_settings(PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED=True)
    def test_backlog_scheduled_and_terminal_exclusions_unchanged(self):
        Status.objects.get_or_create(slug='backlog', defaults={'name': 'Backlog', 'category': 'backlog'})
        Status.objects.get_or_create(slug='scheduled', defaults={'name': 'Scheduled', 'category': 'scheduled'})
        self._membership(self.primary, 'primary')
        self._item('active-primary', self.primary, status='open', priority='normal')
        self._item('backlog-primary', self.primary, status='backlog', priority='urgent')
        self._item('scheduled-primary', self.primary, status='scheduled', priority='urgent')
        self._item('completed-primary', self.primary, status='completed', priority='urgent')

        titles = self._titles()
        self.assertEqual(titles, ['active-primary'])


class QueueOrderAssigneeEmailStaffTests(APITestCase):
    """
    Verify staff users also get 200 — unchanged from before PLS-117.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='staff_test', email='staff_test@shizuha.com', password='x',
            is_staff=True, is_superuser=False,
        )
        self.client.force_authenticate(user=self.user)

    def _item(self, title, **kw):
        kw.setdefault('workflow_name', 'autonomous-bug')
        factory = create_blocked_item if kw.get('status') == 'blocked' else Item.objects.create
        return factory(
            title=title, mode='task',
            assignee_id=self.user.id, assignee_email=self.user.email,
            **kw,
        )

    def test_staff_empty_queue_200(self):
        resp = self.client.get(
            f'{ITEMS_URL}?mode=task&assignee_email={self.user.email}'
            '&queue_order=true&is_active=true&page_size=20'
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 0)

    def test_staff_nonempty_queue_200(self):
        self._item('task', status='open', priority='normal')
        resp = self.client.get(
            f'{ITEMS_URL}?mode=task&assignee_email={self.user.email}'
            '&queue_order=true&is_active=true&page_size=20'
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['count'], 1)

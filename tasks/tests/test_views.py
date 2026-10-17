"""
Tests for Shizuha Hub API views.
Tests all REST API endpoints for items, alerts, actions, and rules.
"""
from django.db import connection
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from datetime import timedelta
import json

User = get_user_model()

from tasks.models import (
    Item, ApprovalRule, SilenceRule, EscalationRule,
    InhibitionRule, Subscription, FingerprintConfig, ItemLink,
    Project, SLADefinition, SLAInstance, WebhookKey, Comment,
)
from .base import AuthenticatedAPITestCase


# =============================================================================
# Item ViewSet Tests
# =============================================================================

class ItemViewSetTests(AuthenticatedAPITestCase):
    """Test ItemViewSet CRUD operations."""

    def test_list_items(self):
        """Test listing all items."""
        Item.objects.create(mode='task', title='Task 1', created_by_id=self.user.id)
        Item.objects.create(mode='action', title='Action 1', created_by_id=self.user.id)
        Item.objects.create(mode='alert', title='Alert 1', created_by_id=self.user.id)

        response = self.client.get('/api/items/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)

    def test_list_items_batches_comment_metadata_in_page_query(self):
        """A full page must not issue comment count/latest queries per item."""
        items = [
            Item.objects.create(
                mode='task',
                title=f'Commented task {index}',
                created_by_id=self.user.id,
            )
            for index in range(20)
        ]
        for item in items:
            Comment.objects.create(
                item=item,
                author_id=self.user.id,
                author_email=self.user.email,
                content='first',
            )
            Comment.objects.create(
                item=item,
                author_id=self.user.id,
                author_email=self.user.email,
                content='latest',
            )

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get('/api/items/', {'page_size': 200})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 20)
        for row in response.data['results']:
            self.assertEqual(row['comment_count'], 2)
            self.assertIsNotNone(row['last_comment_at'])

        comment_queries = [
            query['sql'] for query in ctx.captured_queries
            if 'tasks_comment' in query['sql'].lower()
        ]
        self.assertEqual(
            len(comment_queries),
            1,
            f'comment metadata must be batched into the page query: {comment_queries}',
        )

    def test_list_items_filter_by_mode(self):
        """Test filtering items by mode."""
        Item.objects.create(mode='task', title='Task 1', created_by_id=self.user.id)
        Item.objects.create(mode='task', title='Task 2', created_by_id=self.user.id)
        Item.objects.create(mode='alert', title='Alert 1', created_by_id=self.user.id)

        response = self.client.get('/api/items/', {'mode': 'task'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

    def test_list_items_exposes_roadmap_timeline_fields(self):
        """PLS-269: list payload carries the lightweight fields needed by the roadmap timeline."""
        start = timezone.now()
        due = start + timedelta(days=14)
        parent = Item.objects.create(
            mode='task',
            title='Roadmap epic',
            item_key='PLS-269',
            start_date=start,
            due_date=due,
            percent_complete=25,
            workflow_name='simple',
            created_by_id=self.user.id,
        )
        child = Item.objects.create(
            mode='task',
            title='Roadmap child',
            item_key='PLS-270',
            parent_item=parent,
            start_date=start,
            due_date=due,
            percent_complete=50,
            workflow_name='simple',
            created_by_id=self.user.id,
        )

        response = self.client.get('/api/items/', {'mode': 'task', 'search': 'Roadmap child'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data['results'][0]
        self.assertEqual(row['id'], child.id)
        self.assertIn('start_date', row)
        self.assertEqual(row['percent_complete'], 50)
        self.assertEqual(row['parent_item'], parent.id)
        self.assertEqual(row['parent_item_key'], 'PLS-269')
        self.assertEqual(row['parent_item_title'], 'Roadmap epic')

    def test_list_items_filter_by_status(self):
        """Test filtering items by status.

        Uses canonical `simple`-workflow status slugs (`in_progress`, `done`)
        rather than the legacy aliases `pending`/`completed`. A task Item with
        no explicit workflow auto-attaches the seeded `simple` system workflow
        (migration 0023), whose `_auto_attach_workflow`/`_coerce_invalid_status`
        healing deterministically remaps the legacy `pending` -> the initial
        `todo` slug and `completed` -> `done`. Creating with `status='pending'`
        therefore never persists as `pending`, so a `?status=pending` filter
        matched 0 rows and this gate test failed on every main commit, blocking
        all shizuha-tasks deploys (PLS-259 follow-up). Canonical slugs are valid
        in `simple` and preserved on save, making the filter assertion
        deterministic. (Follow-up to PLS-259; owner: ryo / shizuha-tasks CI.)
        """
        Item.objects.create(title='In Progress', status='in_progress', created_by_id=self.user.id)
        Item.objects.create(title='Done', status='done', created_by_id=self.user.id)

        response = self.client.get('/api/items/', {'status': 'in_progress'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)

    def test_list_items_filter_by_source_id(self):
        """PLAT-2688: exact source_id filter finds an item by its stable external
        identity — including terminal-status copies — so the security-ci dedup
        lookup stops re-filing duplicates on repeated CI runs."""
        Item.objects.create(
            title='Accepted finding', status='accepted',
            source='security-ci', source_id='security-ci:abc123',
            created_by_id=self.user.id,
        )
        Item.objects.create(
            title='Unrelated finding', status='open',
            source='security-ci', source_id='security-ci:def456',
            created_by_id=self.user.id,
        )

        response = self.client.get(
            '/api/items/', {'source': 'security-ci', 'source_id': 'security-ci:abc123'}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['title'], 'Accepted finding')

    def test_task_label_filter_matches_both_shapes_and_excludes_unrelated_rows(self):
        """PLS-712: bare/JSON filters narrow array and object label rows."""
        array_drive = Item.objects.create(
            mode='task', title='Drive array label', status='open',
            workflow_name='autonomous-bug', created_by_id=self.user.id,
            labels=['repo:shizuha-labs/drive', 'kind:bug'],
        )
        object_drive = Item.objects.create(
            mode='task', title='Drive object label', status='open',
            workflow_name='autonomous-bug', created_by_id=self.user.id,
            labels={'repo': 'shizuha-labs/drive', 'kind': 'bug'},
        )
        Item.objects.create(
            mode='task', title='Beta exclusion control', status='open',
            workflow_name='autonomous-bug', created_by_id=self.user.id,
            labels=['repo:shizuha-labs/shizuha-beta', 'kind:bug'],
        )

        accepted_forms = (
            'repo:shizuha-labs/drive',
            json.dumps(['repo:shizuha-labs/drive']),
            json.dumps({'repo': 'shizuha-labs/drive'}),
        )
        for label_filter in accepted_forms:
            with self.subTest(label_filter=label_filter):
                response = self.client.get('/api/tasks/', {'labels': label_filter})
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    {row['id'] for row in response.data['results']},
                    {array_drive.id, object_drive.id},
                )
                self.assertNotIn(
                    'Beta exclusion control',
                    {row['title'] for row in response.data['results']},
                )

        # Comma-separated tokens are ANDed, not ORed.
        Item.objects.create(
            mode='task', title='Drive without kind', status='open',
            workflow_name='autonomous-bug', created_by_id=self.user.id,
            labels=['repo:shizuha-labs/drive'],
        )
        response = self.client.get(
            '/api/tasks/',
            {'labels': 'repo:shizuha-labs/drive,kind:bug'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row['id'] for row in response.data['results']},
            {array_drive.id, object_drive.id},
        )

    def test_explicit_deferred_task_label_filter_stays_narrow(self):
        """The terminal-list escape hatch must apply the same predicate."""
        drive = Item.objects.create(
            mode='task', title='Deferred Drive', status='deferred',
            workflow_name='autonomous-dev', created_by_id=self.user.id,
            labels=['repo:shizuha-labs/drive'],
        )
        Item.objects.create(
            mode='task', title='Deferred Beta exclusion control', status='deferred',
            workflow_name='autonomous-dev', created_by_id=self.user.id,
            labels=['repo:shizuha-labs/shizuha-beta'],
        )

        response = self.client.get('/api/tasks/', {
            'status': 'deferred',
            'labels': 'repo:shizuha-labs/drive',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row['id'] for row in response.data['results']],
            [drive.id],
        )

    def test_malformed_task_label_filters_fail_loud(self):
        for malformed in (
            '[not-json',
            '["repo:shizuha-labs/drive", 7]',
            '{"repo": 7}',
            'repo:shizuha-labs/drive,,kind:bug',
        ):
            with self.subTest(malformed=malformed):
                response = self.client.get('/api/tasks/', {'labels': malformed})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('comma-separated', str(response.data))
                self.assertIn('JSON array', str(response.data))
                self.assertIn('JSON object', str(response.data))

    def test_is_active_excludes_rfc_implementation_linked_with_active_followups(self):
        """Implementation Linked RFCs wait while their TYPED implementation work is active.

        37c0c9a dropped the legacy free-text branch (`description__icontains=F(item_key)`,
        e.g. a description mentioning "PLS-183") because it forced Postgres into
        nested-loop ILIKE scans and ~14s of JIT on every is_active list/count.
        Typed implementation links are the durable model (PLS-189/PLS-445), so the
        parking link below is `implemented_by`, not `relates_to`. The `unrelated`
        item further down keeps a plain `relates_to` link and must NOT park the RFC.
        """
        waiting = Item.objects.create(
            mode='task',
            title='RFC waiting for implementation',
            description='Implementation follow-up PLS-183 will land the deployment ledger.',
            status='rfc_implementation_linked',
            workflow_name='feature-request-rfc',
            assignee_email=self.user.email,
            created_by_id=self.user.id,
        )
        followup = Item.objects.create(
            mode='task',
            item_key='PLS-183',
            title='Implementation follow-up',
            status='open',
            workflow_name='autonomous-dev',
            created_by_id=self.user.id,
        )
        # 37c0c9a0 made the active-queue predicate LINK-TYPE ONLY, dropping the
        # `description__icontains=F(target_item__item_key)` branch that used to
        # infer this association from the free text above ("Implementation
        # follow-up PLS-183" matching followup.item_key). `relates_to` is not in
        # IMPLEMENTATION_LINK_TYPES, so that branch was the only thing excluding
        # `waiting`, and this fixture broke the moment it was removed.
        #
        # Retyped to the durable model (typed implementation links, PLS-189/
        # PLS-445) so the case tests what its name says -- an RFC waits while its
        # implementation work is active -- rather than testing free-text matching.
        ItemLink.objects.create(
            source_item=waiting,
            target_item=followup,
            link_type='implemented_by',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        unrelated = Item.objects.create(
            mode='task',
            item_key='PLS-189',
            title='Active related bug, not implementation',
            status='open',
            workflow_name='autonomous-bug',
            created_by_id=self.user.id,
        )
        ItemLink.objects.create(
            source_item=unrelated,
            target_item=waiting,
            link_type='relates_to',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        ready = Item.objects.create(
            mode='task',
            title='Ready task',
            status='open',
            assignee_email=self.user.email,
            created_by_id=self.user.id,
        )

        response = self.client.get('/api/items/', {
            'mode': 'task',
            'assignee_email': self.user.email,
            'is_active': 'true',
            'queue_order': 'true',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {int(row['id']) for row in response.data['results']}
        self.assertIn(ready.id, ids)
        self.assertNotIn(waiting.id, ids)

        explicit = self.client.get('/api/items/', {
            'mode': 'task',
            'status': 'rfc_implementation_linked',
        })
        self.assertEqual(explicit.status_code, status.HTTP_200_OK)
        self.assertEqual(explicit.data['count'], 1)

        followup.status = 'done'
        followup.save(update_fields=['status'])
        ready_again = self.client.get('/api/items/', {
            'mode': 'task',
            'assignee_email': self.user.email,
            'is_active': 'true',
            'queue_order': 'true',
        })
        self.assertEqual(ready_again.status_code, status.HTTP_200_OK)
        ready_again_ids = {int(row['id']) for row in ready_again.data['results']}
        self.assertIn(waiting.id, ready_again_ids)

    def test_is_active_no_longer_infers_implementation_from_free_text(self):
        """37c0c9a0's contract change, pinned so it is recorded rather than implicit.

        The active-queue predicate used to OR in
        `description__icontains=F(target_item__item_key)`, so an RFC whose prose
        mentioned its follow-up's key was excluded even without a typed link.
        That branch was removed because it forced nested-loop ILIKE scans and
        ~14s of JIT on every `is_active` list.

        The consequence is deliberate but NOT cosmetic: an RFC associated only by
        free text now reports as active. This test fails if the free-text branch
        is ever restored, and its existence is the record that legacy
        free-text-linked RFCs lost their exclusion -- see the PR discussion for
        whether those need a typed-link backfill.
        """
        prose_only = Item.objects.create(
            mode='task',
            title='RFC referencing its follow-up only in prose',
            description='Implementation follow-up PLS-9001 will land this.',
            status='rfc_implementation_linked',
            workflow_name='feature-request-rfc',
            assignee_email=self.user.email,
            created_by_id=self.user.id,
        )
        followup = Item.objects.create(
            mode='task',
            item_key='PLS-9001',
            title='Implementation follow-up',
            status='open',
            workflow_name='autonomous-dev',
            created_by_id=self.user.id,
        )
        # Related, but NOT a typed implementation link -- the only association is
        # the item_key appearing in prose_only.description.
        ItemLink.objects.create(
            source_item=prose_only,
            target_item=followup,
            link_type='relates_to',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

        response = self.client.get('/api/items/', {
            'mode': 'task',
            'assignee_email': self.user.email,
            'is_active': 'true',
            'queue_order': 'true',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {int(row['id']) for row in response.data['results']}
        self.assertIn(
            prose_only.id, ids,
            'free-text association is no longer honoured; if this fails, the '
            'description__icontains branch has been restored and the ~14s JIT '
            'regression it caused is back',
        )

    def test_list_items_filter_by_priority(self):
        """Test filtering items by priority."""
        Item.objects.create(title='High', priority='high', created_by_id=self.user.id)
        Item.objects.create(title='Low', priority='low', created_by_id=self.user.id)

        response = self.client.get('/api/items/', {'priority': 'high'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)

    def test_create_item(self):
        """Test creating an item."""
        project = Project.objects.create(
            org_id=1,
            name='Test Project',
            key='TEST',
            access_level='open',
            created_by_id=self.user.id,
        )
        data = {
            'title': 'New Task',
            'description': 'Task description',
            'priority': 'high',
            'mode': 'task',
            'project': project.id,
        }

        response = self.client.post('/api/items/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(Item.objects.first().title, 'New Task')

    def test_create_item_with_labels(self):
        """Test creating an item with labels."""
        data = {
            'title': 'Labeled Item',
            'mode': 'alert',
            'labels': {'env': 'production', 'host': 'web-01'}
        }

        response = self.client.post('/api/items/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = Item.objects.first()
        self.assertEqual(item.labels['env'], 'production')

    def test_retrieve_item(self):
        """Test retrieving a single item."""
        item = Item.objects.create(
            title='Test Item', description='Details', created_by_id=self.user.id
        )

        response = self.client.get(f'/api/items/{item.id}/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'Test Item')
        self.assertEqual(response.data['description'], 'Details')

    def test_update_item(self):
        """Test updating an item."""
        item = Item.objects.create(
            title='Original', priority='low', created_by_id=self.user.id
        )

        response = self.client.patch(
            f'/api/items/{item.id}/',
            {'title': 'Updated', 'priority': 'high'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.title, 'Updated')
        self.assertEqual(item.priority, 'high')

    def test_delete_item(self):
        """Test deleting an item."""
        item = Item.objects.create(title='To Delete', created_by_id=self.user.id)

        response = self.client.delete(f'/api/items/{item.id}/')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Item.objects.count(), 0)

    def test_retrieve_nonexistent_item(self):
        """Test retrieving nonexistent item returns 404."""
        response = self.client.get('/api/items/99999/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ItemViewSetActionTests(AuthenticatedAPITestCase):
    """Test ItemViewSet custom actions."""

    def test_complete_task(self):
        """Test completing a task."""
        task = Item.objects.create(
            mode='task',
            title='Task to Complete',
            status='pending',
            created_by_id=self.user.id
        )

        response = self.client.post(f'/api/items/{task.id}/complete/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
        self.assertIsNotNone(task.completed_at)

    def test_complete_already_completed_task_is_idempotent(self):
        """Completing an already-completed task is an idempotent no-op: HTTP 200
        with a message that explicitly signals no state transition occurred
        (contains 'already completed') — the contract decided on PLS-260.

        Drive the task to completed via the endpoint first: creating an Item
        directly with status='completed' does not persist as completed for a
        fresh item (the model's status handling), so the canonical completion
        path is used to reach the already-completed state the test exercises.
        """
        task = Item.objects.create(
            mode='task',
            title='Completed',
            status='pending',
            created_by_id=self.user.id,
        )

        # First completion is a real transition (200, generic success).
        first = self.client.post(f'/api/items/{task.id}/complete/')
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')

        # Second completion must be an idempotent no-op: 200 + explicit
        # already-completed message (no fabricated "completed successfully").
        response = self.client.post(f'/api/items/{task.id}/complete/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('already completed', response.data['message'])

    def test_approve_action(self):
        """Test approving an action."""
        action = Item.objects.create(
            mode='action',
            title='Action to Approve',
            status='pending',
            requires_approval=True,
            created_by_id=self.user.id
        )

        response = self.client.post(
            f'/api/items/{action.id}/approve/',
            {'comment': 'Looks good'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        action.refresh_from_db()
        self.assertEqual(action.status, 'approved')

    def test_approve_non_pending_action_fails(self):
        """Test approving non-pending action fails."""
        action = Item.objects.create(
            mode='action',
            title='Already Approved',
            status='approved',
            created_by_id=self.user.id
        )

        response = self.client.post(f'/api/items/{action.id}/approve/')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_action(self):
        """Test rejecting an action."""
        action = Item.objects.create(
            mode='action',
            title='Action to Reject',
            status='pending',
            created_by_id=self.user.id
        )

        response = self.client.post(
            f'/api/items/{action.id}/reject/',
            {'reason': 'Not allowed'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        action.refresh_from_db()
        self.assertEqual(action.status, 'rejected')
        self.assertEqual(action.rejection_reason, 'Not allowed')

    def test_acknowledge_alert(self):
        """Test acknowledging an alert."""
        alert = Item.objects.create(
            mode='alert',
            title='Alert to Ack',
            status='firing',
            created_by_id=self.user.id
        )

        response = self.client.post(
            f'/api/items/{alert.id}/acknowledge/',
            {'comment': 'Looking into it'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'acknowledged')

    def test_acknowledge_resolved_alert_fails(self):
        """Test acknowledging resolved alert fails."""
        alert = Item.objects.create(
            mode='alert',
            title='Resolved Alert',
            status='resolved',
            created_by_id=self.user.id
        )

        response = self.client.post(f'/api/items/{alert.id}/acknowledge/')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resolve_alert(self):
        """Test resolving an alert."""
        alert = Item.objects.create(
            mode='alert',
            title='Alert to Resolve',
            status='firing',
            created_by_id=self.user.id
        )

        response = self.client.post(
            f'/api/items/{alert.id}/resolve/',
            {'resolution_note': 'Fixed the issue'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'resolved')
        self.assertEqual(alert.resolution_note, 'Fixed the issue')

    def test_silence_alert(self):
        """Test silencing an alert."""
        alert = Item.objects.create(
            mode='alert',
            title='Alert to Silence',
            status='firing',
            created_by_id=self.user.id
        )

        response = self.client.post(
            f'/api/items/{alert.id}/silence/',
            {'duration_minutes': 60, 'reason': 'Maintenance'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        alert.refresh_from_db()
        self.assertTrue(alert.is_silenced())

    def test_silence_without_duration_fails(self):
        """Test silencing without duration fails."""
        alert = Item.objects.create(
            mode='alert', title='Alert', status='firing',
            created_by_id=self.user.id
        )

        response = self.client.post(f'/api/items/{alert.id}/silence/', {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ItemViewSetFilterEndpointsTests(AuthenticatedAPITestCase):
    """Test ItemViewSet filter endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create various items
        Item.objects.create(
            mode='task', title='Task 1', status='pending', created_by_id=self.user.id
        )
        Item.objects.create(
            mode='task', title='Task 2', status='done', created_by_id=self.user.id
        )
        Item.objects.create(
            mode='action', title='Action 1', status='pending', created_by_id=self.user.id
        )
        Item.objects.create(
            mode='action', title='Action 2', status='approved', created_by_id=self.user.id
        )
        Item.objects.create(
            mode='alert', title='Alert 1', status='firing', severity='warning',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='alert', title='Alert 2', status='resolved', severity='critical',
            created_by_id=self.user.id
        )

    def test_tasks_endpoint(self):
        """Test /items/tasks/ returns only tasks."""
        response = self.client.get('/api/items/tasks/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        for item in response.data['tasks']:
            self.assertEqual(item['mode'], 'task')

    def test_actions_endpoint(self):
        """Test /items/actions/ returns only actions."""
        response = self.client.get('/api/items/actions/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        for item in response.data['actions']:
            self.assertEqual(item['mode'], 'action')

    def test_alerts_endpoint(self):
        """Test /items/alerts/ returns only active alerts (firing/acknowledged)."""
        response = self.client.get('/api/items/alerts/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Only firing/acknowledged alerts are returned by default (Alert 1 is firing, Alert 2 is resolved)
        self.assertEqual(response.data['count'], 1)
        for item in response.data['alerts']:
            self.assertEqual(item['mode'], 'alert')
            self.assertIn(item['status'], ['firing', 'acknowledged'])

    def test_pending_endpoint(self):
        """Test /items/pending/ returns pending items grouped by mode."""
        response = self.client.get('/api/items/pending/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Response is grouped by mode: tasks, actions, alerts
        self.assertIn('tasks', response.data)
        self.assertIn('actions', response.data)
        self.assertIn('alerts', response.data)

    def test_today_endpoint(self):
        """Test /items/today/ returns items due today."""
        # Create item due today
        Item.objects.create(
            title='Due Today',
            due_date=timezone.now(),
            created_by_id=self.user.id
        )

        response = self.client.get('/api/items/today/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)


# =============================================================================
# Task ViewSet Tests (Backward Compatibility)
# =============================================================================

class TaskViewSetTests(AuthenticatedAPITestCase):
    """Test TaskViewSet for backward compatibility."""

    def test_task_endpoint_creates_task_mode(self):
        """Test /tasks/ endpoint creates items with task mode."""
        project = Project.objects.create(
            org_id=1,
            name='Task Endpoint Project',
            key='TASK',
            access_level='open',
            created_by_id=self.user.id,
        )
        data = {
            'title': 'New Task',
            'priority': 'high',
            'project': project.id,
        }

        response = self.client.post('/api/tasks/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['mode'], 'task')

    def test_task_endpoint_returns_task_mode_only_by_default(self):
        """PLS-293: /tasks/ must not include alert/action rows by default."""
        Item.objects.create(mode='task', title='Task', created_by_id=self.user.id)
        Item.objects.create(mode='action', title='Action', created_by_id=self.user.id)
        Item.objects.create(mode='alert', title='Alert', created_by_id=self.user.id)

        response = self.client.get('/api/tasks/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['mode'], 'task')

    def test_task_endpoint_can_filter_by_mode(self):
        """Test /tasks/ endpoint can filter by mode parameter."""
        Item.objects.create(mode='task', title='Task', created_by_id=self.user.id)
        Item.objects.create(mode='action', title='Action', created_by_id=self.user.id)
        Item.objects.create(mode='alert', title='Alert', created_by_id=self.user.id)

        response = self.client.get('/api/tasks/?mode=task')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['mode'], 'task')

    def test_task_endpoint_rejects_alert_mode_leak_even_if_mode_requested(self):
        """PLS-293: explicit mode=alert must not turn /tasks/ into /items/."""
        Item.objects.create(mode='task', title='Task', created_by_id=self.user.id)
        Item.objects.create(mode='alert', title='Alert', created_by_id=self.user.id)

        response = self.client.get('/api/tasks/?mode=alert')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['mode'], 'task')


# =============================================================================
# Alert Submit View Tests
# =============================================================================

class AlertSubmitViewTests(APITestCase):
    """Test AlertSubmitView for alert submission (AllowAny)."""

    def setUp(self):
        """Set up test client."""
        self.client = APIClient()

    def test_submit_new_alert(self):
        """Test submitting a new alert."""
        data = {
            'title': 'High CPU Usage',
            'source': 'prometheus',
            'severity': 'warning',
            'labels': {'host': 'web-01'}
        }

        response = self.client.post('/api/alerts/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['message'], 'Alert created')
        self.assertIn('fingerprint', response.data)

    def test_submit_duplicate_alert(self):
        """Test submitting duplicate alert deduplicates."""
        data = {
            'title': 'High CPU',
            'source': 'prometheus',
            'severity': 'warning',
            'labels': {'host': 'web-01'}
        }

        response1 = self.client.post('/api/alerts/', data, format='json')
        response2 = self.client.post('/api/alerts/', data, format='json')

        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.data['message'], 'Alert deduplicated')
        self.assertEqual(response2.data['occurrence_count'], 2)

    def test_submit_alert_without_title_fails(self):
        """Test submitting alert without title fails."""
        data = {
            'source': 'prometheus',
            'severity': 'warning'
        }

        response = self.client.post('/api/alerts/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_alert_without_source_fails(self):
        """Test submitting alert without source fails."""
        data = {
            'title': 'Alert',
            'severity': 'warning'
        }

        response = self.client.post('/api/alerts/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_alert_with_correlation_id(self):
        """Test submitting alert with correlation_id."""
        data = {
            'title': 'Correlated Alert',
            'source': 'test',
            'severity': 'warning',
            'correlation_id': 'test-123'
        }

        response = self.client.post('/api/alerts/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = Item.objects.first()
        self.assertEqual(item.correlation_id, 'test-123')

    def test_correlation_id_only_alerts_do_not_share_empty_label_fingerprint(self):
        """Correlation-id-only alerts keep distinct dedupe identities."""
        first = {
            'title': 'Correlated Alert 1',
            'source': 'test',
            'severity': 'warning',
            'correlation_id': 'test-123',
        }
        second = {
            'title': 'Correlated Alert 2',
            'source': 'test',
            'severity': 'warning',
            'correlation_id': 'test-456',
        }

        response1 = self.client.post('/api/alerts/', first, format='json')
        response2 = self.client.post('/api/alerts/', second, format='json')

        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Item.objects.filter(mode='alert', source='test').count(), 2)

    def test_identityless_alert_returns_400_without_creating_item(self):
        data = {
            'title': 'Identityless k3s alert',
            'source': 'k3s-alertmanager',
            'severity': 'warning',
            'labels': {},
        }

        response = self.client.post('/api/alerts/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'alert_identity_error')
        self.assertFalse(Item.objects.filter(mode='alert', source='k3s-alertmanager').exists())


class AlertBulkViewsTests(AuthenticatedAPITestCase):
    """Test bulk alert operations."""

    def setUp(self):
        """Set up test alerts."""
        super().setUp()
        self.alert1 = Item.objects.create(
            mode='alert', title='Alert 1', status='firing',
            created_by_id=self.user.id
        )
        self.alert2 = Item.objects.create(
            mode='alert', title='Alert 2', status='firing',
            created_by_id=self.user.id
        )
        self.alert3 = Item.objects.create(
            mode='alert', title='Alert 3', status='resolved',
            created_by_id=self.user.id
        )

    def test_bulk_acknowledge(self):
        """Test bulk acknowledging alerts."""
        data = {'item_ids': [self.alert1.id, self.alert2.id]}

        response = self.client.post(
            '/api/alerts/bulk-acknowledge/',
            data,
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

        self.alert1.refresh_from_db()
        self.alert2.refresh_from_db()
        self.assertEqual(self.alert1.status, 'acknowledged')
        self.assertEqual(self.alert2.status, 'acknowledged')

    def test_bulk_acknowledge_skips_resolved(self):
        """Test bulk acknowledge skips resolved alerts."""
        data = {'item_ids': [self.alert1.id, self.alert3.id]}

        response = self.client.post(
            '/api/alerts/bulk-acknowledge/',
            data,
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)

    def test_bulk_resolve(self):
        """Test bulk resolving alerts."""
        data = {
            'item_ids': [self.alert1.id, self.alert2.id],
            'resolution_note': 'Bulk resolved'
        }

        response = self.client.post(
            '/api/alerts/bulk-resolve/',
            data,
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

        self.alert1.refresh_from_db()
        self.assertEqual(self.alert1.status, 'resolved')


# =============================================================================
# Action Submit View Tests
# =============================================================================

class ActionSubmitViewTests(APITestCase):
    """Test ActionSubmitView for action submission (AllowAny)."""

    def setUp(self):
        """Set up test client."""
        self.client = APIClient()

    def test_submit_action(self):
        """Test submitting an action."""
        data = {
            'title': 'Execute Trade',
            'source': 'arbiter',
            'item_type': 'action.hedging.short',
            'payload': {'symbol': 'BTCUSDT', 'quantity': 0.1},
            'requires_approval': True
        }

        response = self.client.post('/api/actions/submit/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['message'], 'Action submitted')
        self.assertTrue(response.data['requires_approval'])

    def test_submit_action_with_callback(self):
        """Test submitting action with callback URL."""
        data = {
            'title': 'Webhook Action',
            'source': 'test',
            'callback_url': 'https://example.com/webhook',
            'callback_method': 'POST',
            'callback_headers': {'Authorization': 'Bearer token'}
        }

        response = self.client.post('/api/actions/submit/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = Item.objects.first()
        self.assertEqual(item.callback_url, 'https://example.com/webhook')
        self.assertEqual(item.callback_method, 'POST')

    def test_submit_action_without_title_fails(self):
        """Test submitting action without title fails."""
        data = {'source': 'test'}

        response = self.client.post('/api/actions/submit/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# =============================================================================
# Rule ViewSet Tests
# =============================================================================

class ApprovalRuleViewSetTests(AuthenticatedAPITestCase):
    """Test ApprovalRuleViewSet."""

    def test_list_approval_rules(self):
        """Test listing approval rules."""
        ApprovalRule.objects.create(name='Rule 1')
        ApprovalRule.objects.create(name='Rule 2')

        response = self.client.get('/api/rules/approval/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_create_approval_rule(self):
        """Test creating an approval rule."""
        data = {
            'name': 'Auto Approve Low Priority',
            'item_types': ['action.hedging.*'],
            'auto_approve': True,
            'priority': 10
        }

        response = self.client.post('/api/rules/approval/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ApprovalRule.objects.count(), 1)

    def test_update_approval_rule(self):
        """Test updating an approval rule."""
        rule = ApprovalRule.objects.create(name='Original')

        response = self.client.patch(
            f'/api/rules/approval/{rule.id}/',
            {'name': 'Updated'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rule.refresh_from_db()
        self.assertEqual(rule.name, 'Updated')

    def test_delete_approval_rule(self):
        """Test deleting an approval rule."""
        rule = ApprovalRule.objects.create(name='To Delete')

        response = self.client.delete(f'/api/rules/approval/{rule.id}/')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(ApprovalRule.objects.count(), 0)


class SilenceRuleViewSetTests(AuthenticatedAPITestCase):
    """Test SilenceRuleViewSet."""

    def test_list_silence_rules(self):
        """Test listing silence rules."""
        SilenceRule.objects.create(name='Rule 1')
        SilenceRule.objects.create(name='Rule 2')

        response = self.client.get('/api/rules/silence/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_create_silence_rule(self):
        """Test creating a silence rule."""
        data = {
            'name': 'Maintenance Silence',
            'modes': ['alert'],
            'sources': ['prometheus'],
            'severities': ['info', 'warning'],
            'reason': 'Scheduled maintenance'
        }

        response = self.client.post('/api/rules/silence/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        rule = SilenceRule.objects.first()
        self.assertIn('alert', rule.modes)

    def test_create_silence_rule_with_time_bounds(self):
        """Test creating silence rule with time bounds."""
        data = {
            'name': 'Timed Silence',
            'starts_at': timezone.now().isoformat(),
            'ends_at': (timezone.now() + timedelta(hours=2)).isoformat()
        }

        response = self.client.post('/api/rules/silence/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        rule = SilenceRule.objects.first()
        self.assertIsNotNone(rule.starts_at)
        self.assertIsNotNone(rule.ends_at)

    def test_create_silence_rule_with_label_matchers(self):
        """Test creating silence rule with label matchers."""
        data = {
            'name': 'Label Silence',
            'label_matchers': {'env': 'production', 'host': 'web-*'}
        }

        response = self.client.post('/api/rules/silence/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        rule = SilenceRule.objects.first()
        self.assertEqual(rule.label_matchers['env'], 'production')


class EscalationRuleViewSetTests(AuthenticatedAPITestCase):
    """Test EscalationRuleViewSet."""

    def test_list_escalation_rules(self):
        """Test listing escalation rules."""
        EscalationRule.objects.create(name='Rule 1')
        EscalationRule.objects.create(name='Rule 2')

        response = self.client.get('/api/rules/escalation/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_create_escalation_rule(self):
        """Test creating an escalation rule."""
        data = {
            'name': 'Critical Escalation',
            'modes': ['alert'],
            'min_severity': 'warning',
            'stages': [
                {
                    'delay_minutes': 5,
                    'actions': [{'type': 'bump_severity', 'to': 'error'}]
                }
            ]
        }

        response = self.client.post('/api/rules/escalation/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        rule = EscalationRule.objects.first()
        self.assertEqual(len(rule.stages), 1)


class InhibitionRuleViewSetTests(AuthenticatedAPITestCase):
    """Test InhibitionRuleViewSet."""

    def test_list_inhibition_rules(self):
        """Test listing inhibition rules."""
        InhibitionRule.objects.create(name='Rule 1')
        InhibitionRule.objects.create(name='Rule 2')

        response = self.client.get('/api/rules/inhibition/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_create_inhibition_rule(self):
        """Test creating an inhibition rule."""
        data = {
            'name': 'Database Cascade',
            'source_item_types': ['alert.database.*'],
            'source_severities': ['critical'],
            'target_item_types': ['alert.api.*'],
            'equal_labels': ['env', 'cluster']
        }

        response = self.client.post('/api/rules/inhibition/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        rule = InhibitionRule.objects.first()
        self.assertIn('alert.database.*', rule.source_item_types)


# =============================================================================
# Subscription ViewSet Tests
# =============================================================================

class SubscriptionViewSetTests(AuthenticatedAPITestCase):
    """Test SubscriptionViewSet."""

    def test_list_subscriptions(self):
        """Test listing subscriptions."""
        Subscription.objects.create(name='Sub 1')
        Subscription.objects.create(name='Sub 2')

        response = self.client.get('/api/subscriptions/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_create_webhook_subscription(self):
        """Test creating a webhook subscription."""
        data = {
            'name': 'Alert Webhook',
            'modes': ['alert'],
            'notify_type': 'webhook',
            'webhook_url': 'https://example.com/webhook'
        }

        response = self.client.post('/api/subscriptions/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_shizuha_notify_subscription(self):
        """Test creating a shizuha_notify subscription."""
        data = {
            'name': 'Telegram Alerts',
            'modes': ['alert'],
            'notify_type': 'shizuha_notify',
            'notify_channels': ['telegram']
        }

        response = self.client.post('/api/subscriptions/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class GrafanaWebhookViewTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.webhook = WebhookKey.objects.create(
            user_id=1,
            name='k3s grafana',
            key='grafana-key',
            integration_type='grafana',
            source_name='k3s-alertmanager',
            default_severity='warning',
        )

    def test_identityless_grafana_alert_is_failed_without_500_or_item(self):
        response = self.client.post('/api/webhooks/grafana/grafana-key/', {
            'alerts': [{
                'status': 'firing',
                'labels': {},
                'annotations': {'summary': 'Identityless k3s alert'},
            }],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['failed'], 1)
        self.assertEqual(response.data['results'][0]['error'], 'alert_identity_error')
        self.assertFalse(Item.objects.filter(mode='alert', source='k3s-alertmanager').exists())


class GenericWebhookViewTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.webhook = WebhookKey.objects.create(
            user_id=1,
            name='k3s generic',
            key='generic-key',
            integration_type='generic',
            source_name='k3s-alertmanager',
            default_severity='warning',
        )

    def test_identityless_generic_webhook_returns_400_without_creating_item(self):
        response = self.client.post('/api/webhooks/generic/generic-key/', {
            'title': 'Identityless k3s alert',
            'source': 'k3s-alertmanager',
            'severity': 'warning',
            'labels': {},
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'alert_identity_error')
        self.assertFalse(Item.objects.filter(mode='alert', source='k3s-alertmanager').exists())


# =============================================================================
# FingerprintConfig ViewSet Tests
# =============================================================================

class FingerprintConfigViewSetTests(AuthenticatedAPITestCase):
    """Test FingerprintConfigViewSet."""

    def test_list_fingerprint_configs(self):
        """Test listing fingerprint configs."""
        FingerprintConfig.objects.create(source='prometheus')
        FingerprintConfig.objects.create(source='grafana')

        response = self.client.get('/api/fingerprint-configs/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_create_fingerprint_config(self):
        """Test creating a fingerprint config."""
        data = {
            'source': 'custom-source',
            'label_fields': ['alertname', 'instance'],
            'payload_fields': ['summary'],
            'description': 'Custom dedup config'
        }

        response = self.client.post('/api/fingerprint-configs/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        config = FingerprintConfig.objects.first()
        self.assertIn('alertname', config.label_fields)

    def test_create_duplicate_source_fails(self):
        """Test creating config with duplicate source fails."""
        FingerprintConfig.objects.create(source='prometheus')

        data = {'source': 'prometheus'}

        response = self.client.post('/api/fingerprint-configs/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# =============================================================================
# Statistics and Summary Endpoint Tests
# =============================================================================

class StatisticsEndpointTests(AuthenticatedAPITestCase):
    """Test statistics endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create various items
        Item.objects.create(
            mode='task', title='T1', status='pending', priority='high',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='task', title='T2', status='done', priority='low',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='action', title='A1', status='pending',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='alert', title='AL1', status='firing', severity='warning',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='alert', title='AL2', status='firing', severity='critical',
            created_by_id=self.user.id
        )

    def test_statistics_endpoint(self):
        """Test /items/statistics/ endpoint."""
        response = self.client.get('/api/items/statistics/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('total_items', response.data)
        self.assertIn('by_mode', response.data)
        self.assertIn('by_status', response.data)

    def test_daily_summary_endpoint_not_implemented(self):
        """Test /items/daily-summary/ endpoint returns 404 (not implemented)."""
        # This endpoint is not yet implemented
        response = self.client.get('/api/items/daily-summary/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PLS830FiringAlertKPITests(AuthenticatedAPITestCase):
    """PLS-830: statistics alerts.firing must be ORG-WIDE and count only
    status='firing', agreeing with by_status.firing and mode=alert&status=firing.
    No setUp fixtures — each test builds exactly the rows it asserts on."""

    def test_firing_kpi_matches_by_status_and_items_api(self):
        """Two firing alerts (assigned to a DIFFERENT user) + acknowledged/resolved
        controls: KPI must be 2, agree with by_status.firing and the items API,
        and acknowledged/resolved must NOT inflate firing."""
        other = User.objects.create_user(
            username='other', email='other@example.com', password='x', is_staff=True,
        )
        # Two firing alerts assigned to ANOTHER user (proves org-wide scope —
        # the caller has none of their own and must NOT see an all-clear 0).
        for i in range(2):
            Item.objects.create(
                mode='alert', title=f'Firing alert {i}', status='firing',
                severity='critical', assignee_id=other.id, assignee_email=other.email,
                created_by_id=self.user.id,
            )
        # Controls: one acknowledged, one resolved — must NOT inflate firing.
        Item.objects.create(
            mode='alert', title='Acknowledged alert', status='acknowledged',
            severity='warning', assignee_id=other.id, assignee_email=other.email,
            created_by_id=self.user.id,
        )
        Item.objects.create(
            mode='alert', title='Resolved alert', status='resolved',
            severity='info', assignee_id=other.id, assignee_email=other.email,
            created_by_id=self.user.id,
        )

        response = self.client.get('/api/items/statistics/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['alerts']['firing'], 2)
        self.assertEqual(response.data['alerts']['acknowledged'], 1)
        # KPI agrees with the org-wide status aggregation.
        self.assertEqual(response.data['by_status'].get('firing'), 2)
        # KPI agrees with mode=alert&status=firing on the items API.
        items = self.client.get('/api/items/', {'mode': 'alert', 'status': 'firing'})
        self.assertEqual(items.status_code, status.HTTP_200_OK)
        self.assertEqual(items.data['count'], 2)

    def test_firing_kpi_respects_tenant_scope(self):
        """A non-staff user without access to another org's alerts must not see
        them in alerts.firing (tenant/auth scoping preserved)."""
        other = User.objects.create_user(
            username='other', email='other@example.com', password='x', is_staff=True,
        )
        Item.objects.create(
            mode='alert', title='Foreign firing alert', status='firing',
            severity='critical', assignee_id=other.id, assignee_email=other.email,
            created_by_id=other.id,
        )
        # Non-staff caller with no projects/org membership.
        limited = User.objects.create_user(
            username='limited', email='limited@example.com', password='x', is_staff=False,
        )
        self.client.force_authenticate(user=limited)
        response = self.client.get('/api/items/statistics/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['alerts']['firing'], 0)
        self.assertEqual(response.data['by_status'].get('firing', 0), 0)


class SLAInstanceViewSetTests(AuthenticatedAPITestCase):
    """PLAT-2690: /api/sla-instances/ filtering by item."""

    def test_filter_by_item_key_does_not_500(self):
        """GET ?item=<KEY> must resolve the item key, not cast it to the integer
        FK (which raised ValueError -> HTTP 500 for every MCP/UI caller)."""
        item = Item.objects.create(
            mode='task', item_key='PLS-237', title='SLA target',
            created_by_id=self.user.id,
        )

        # The regression: a human item key must return 200 (empty when no SLA
        # instance is configured), NOT a 500.
        by_key = self.client.get('/api/sla-instances/', {'item': 'PLS-237'})
        self.assertEqual(by_key.status_code, status.HTTP_200_OK)
        self.assertEqual(by_key.data['count'], 0)

        # The numeric FK path still works unchanged.
        by_id = self.client.get('/api/sla-instances/', {'item': str(item.id)})
        self.assertEqual(by_id.status_code, status.HTTP_200_OK)
        self.assertEqual(by_id.data['count'], 0)

    def test_filter_by_sla_definition_name_does_not_500(self):
        """GET ?sla_definition=<NAME> must resolve by the real name field, not
        cast the non-numeric ref to the UUID PK."""
        project = Project.objects.create(org_id=1, name='SLA Project', key='SLA')
        sla_definition = SLADefinition.objects.create(
            project=project,
            name='P1 Bug Response',
            metric='time_to_first_response',
            targets={'urgent': 60},
        )
        item = Item.objects.create(
            mode='task', item_key='PLS-238', title='SLA tracked item',
            created_by_id=self.user.id,
        )
        instance = SLAInstance.objects.create(
            item=item,
            sla_definition=sla_definition,
            target_minutes=60,
        )

        by_name = self.client.get('/api/sla-instances/', {'sla_definition': 'P1 Bug Response'})
        self.assertEqual(by_name.status_code, status.HTTP_200_OK)
        self.assertEqual(by_name.data['count'], 1)
        self.assertEqual(by_name.data['results'][0]['id'], str(instance.id))

        by_id = self.client.get('/api/sla-instances/', {'sla_definition': str(sla_definition.id)})
        self.assertEqual(by_id.status_code, status.HTTP_200_OK)
        self.assertEqual(by_id.data['count'], 1)
        self.assertEqual(by_id.data['results'][0]['id'], str(instance.id))

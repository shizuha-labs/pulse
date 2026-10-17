"""
Regression tests for PLS-226: hidden_by_permission must use the same
is_active/is_actionable semantics as the visible queryset.

Before the fix, ItemViewSet.list() computed the unscoped hidden count via
_apply_narrowing_filters(Item.objects.all()) WITHOUT the is_active terminal
exclusion, so a non-privileged user querying another assignee saw terminal
history (done/cancelled/deferred) reported as invisible actionable backlog
(e.g. revi=50, rei=75), masking real review load. The count must exclude
terminal rows when is_active=true / is_actionable=true is present.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item

User = get_user_model()


class HiddenByPermissionActiveSemantics(APITestCase):
    def setUp(self):
        self.client = APIClient()
        # Non-privileged viewer: permission scoping applies, so another user's
        # personal (no-project) items are hidden -> the hidden-count path runs.
        self.viewer = User.objects.create_user(
            username='viewer', email='viewer@example.com', password='pw', is_staff=False,
        )
        self.client.force_authenticate(user=self.viewer)
        self.other = 'revi@shizuha.com'

    def _list(self, **params):
        return self.client.get('/api/items/', params)

    def test_terminal_only_hidden_rows_are_not_counted(self):
        # 3 terminal items assigned to another user, 0 active.
        for st in ('done', 'cancelled', 'deferred'):
            Item.objects.create(mode='task', title=f'{st} item', status=st,
                                 assignee_email=self.other, created_by_id=999)
        resp = self._list(assignee_email=self.other, is_active='true')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 0)  # viewer sees none (scoped out)
        # The bug reported hidden_by_permission=3 (the terminal rows). Fixed:
        # terminal rows are excluded from the active count, so no hidden delta.
        self.assertEqual(resp.data.get('hidden_by_permission', 0), 0)

    def test_only_active_hidden_rows_are_counted(self):
        # 2 terminal + 1 active, all hidden from the viewer.
        Item.objects.create(mode='task', title='done', status='done',
                            assignee_email=self.other, created_by_id=999)
        Item.objects.create(mode='task', title='cancelled', status='cancelled',
                            assignee_email=self.other, created_by_id=999)
        Item.objects.create(mode='task', title='live', status='in_progress',
                            assignee_email=self.other, created_by_id=999)
        resp = self._list(assignee_email=self.other, is_active='true')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 0)
        # Only the single active hidden row counts, not the 2 terminal rows.
        self.assertEqual(resp.data.get('hidden_by_permission', 0), 1)

    def test_without_is_active_terminal_rows_still_counted(self):
        # No is_active filter -> the visible queryset would include terminal
        # rows too, so the hidden count legitimately includes them.
        for st in ('done', 'cancelled'):
            Item.objects.create(mode='task', title=st, status=st,
                                assignee_email=self.other, created_by_id=999)
        resp = self._list(assignee_email=self.other)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get('hidden_by_permission', 0), 2)

    def test_control_plane_caller_can_skip_unused_hidden_count(self):
        """MCP queue reads must not execute the duplicate unscoped COUNT."""
        Item.objects.create(
            mode='task',
            title='hidden active row',
            status='in_progress',
            assignee_email=self.other,
            created_by_id=999,
        )
        with mock.patch(
            'tasks.views.ItemViewSet._apply_narrowing_filters',
            side_effect=AssertionError('permission hint query must be skipped'),
        ):
            resp = self._list(
                mode='task',
                assignee_email=self.other,
                is_active='true',
                queue_order='true',
                permission_hint='false',
            )

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('hidden_by_permission', resp.data)
        self.assertEqual(resp['X-Permission-Hint-Skipped'], '1')
        self.assertIn('pulse-active-queue;dur=', resp['Server-Timing'])


class ActiveQueueOwnerIndexContract(APITestCase):
    def test_canonical_owner_queue_index_is_declared(self):
        index = next(
            candidate
            for candidate in Item._meta.indexes
            if candidate.name == 'task_owner_active_queue_idx'
        )
        self.assertEqual(
            index.fields,
            ['assignee_email', 'mode', 'is_archived', 'status'],
        )

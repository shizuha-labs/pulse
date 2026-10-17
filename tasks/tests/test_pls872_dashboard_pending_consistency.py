"""PLS-872 — the dashboard "Pending Tasks" stat card must agree with the
"My Tasks · Active" list directly beneath it.

The card previously counted with only TERMINAL_STATUS_SLUGS + future-scheduled
recurring exclusions, while the Active list uses the shared active-queue
definition (filter_active_queue_items) which ALSO drops backlog, scheduled and
waiting_external rows. That produced "Pending Tasks 13" vs "My Tasks 12" in one
viewport — two authoritative-looking numbers the user could not reconcile.

Fix: the card now uses the same filter_active_queue_items definition, so the
two counts agree by construction. These tests assert the agreement and the
specific statuses that used to diverge.
"""
from rest_framework import status

from tasks.models import Item, Status
from .base import AuthenticatedAPITestCase


def _ensure_status(slug, category):
    status_obj, _ = Status.objects.get_or_create(
        slug=slug,
        defaults={'name': slug.title(), 'category': category, 'is_system': True},
    )
    return status_obj


class DashboardPendingConsistencyTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        # Ensure the statuses we exercise exist with the categories the active
        # queue semantics depend on.
        _ensure_status('todo', 'todo')
        _ensure_status('backlog', 'backlog')
        _ensure_status('scheduled', 'scheduled')
        _ensure_status('done', 'done')

    def _task(self, title, status_slug):
        """Create a task assigned to the user, then force the parked status via
        a direct DB update — the model's save-time status healing (which remaps
        unknown-to-workflow slugs like backlog/scheduled to the workflow's
        initial todo) would otherwise erase the very status we're testing."""
        item = Item.objects.create(
            mode='task',
            title=title,
            status='todo',
            assignee_id=self.user.id,
            assignee_email=self.user.email,
            created_by_id=self.user.id,
        )
        if status_slug != 'todo':
            Item.objects.filter(pk=item.pk).update(status=status_slug)
        return item

    def _stat_pending(self):
        response = self.client.get('/api/items/statistics/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data['tasks']['pending']

    def _active_list_count(self):
        response = self.client.get(
            '/api/items/',
            {'mode': 'task', 'assigned_to_me': 'true', 'is_active': 'true'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data['count']

    def test_pending_card_matches_active_list(self):
        """With a mix of active and parked rows, the card and list agree."""
        self._task('active-1', 'todo')
        self._task('active-2', 'todo')
        self._task('parked-backlog', 'backlog')
        self._task('parked-scheduled', 'scheduled')
        self._task('done-1', 'done')

        self.assertEqual(self._stat_pending(), self._active_list_count())

    def test_backlog_row_is_not_pending(self):
        """PLS-872 root cause: a backlog row used to inflate the card only."""
        self._task('active-1', 'todo')
        self._task('parked-backlog', 'backlog')

        self.assertEqual(self._stat_pending(), 1)
        self.assertEqual(self._active_list_count(), 1)

    def test_scheduled_row_is_not_pending(self):
        self._task('active-1', 'todo')
        self._task('parked-scheduled', 'scheduled')

        self.assertEqual(self._stat_pending(), 1)
        self.assertEqual(self._active_list_count(), 1)

    def test_terminal_row_is_not_pending(self):
        self._task('active-1', 'todo')
        self._task('done-1', 'done')

        self.assertEqual(self._stat_pending(), 1)
        self.assertEqual(self._active_list_count(), 1)

    def test_all_active_rows_are_pending(self):
        """The happy path: every active row is counted in both."""
        self._task('active-1', 'todo')
        self._task('active-2', 'todo')
        self._task('active-3', 'todo')

        self.assertEqual(self._stat_pending(), 3)
        self.assertEqual(self._active_list_count(), 3)


# Note: the dashboard "Pending Tasks" card and the "My Tasks · Active" list must
# agree under the default filter. The card applies the same
# filter_active_queue_items definition as the list, so the two counts agree by
# construction (PLS-872). The mcp-contract-tests gate for this PR is exercised
# by the in-cluster Origin clone in .forgejo/workflows/mcp-contract-tests.yml.

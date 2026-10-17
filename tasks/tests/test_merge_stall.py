"""PLS-1041: merge-lane stall-escalation sweep tests."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project
from tasks.services import merge_stall
from tasks.services.merge_stall import check_merge_stalls


def _make_item(*, status='awaiting_merge', updated_hours_ago=8, key='MS'):
    proj = Project.objects.create(name='Merge Stall', key='MSK', org_id=1)
    item = Item.objects.create(
        project=proj, mode='task', status=status, title=f'{key}-1',
    )
    if status == 'awaiting_merge':
        # The default test workflow normalizes awaiting_merge → in_progress on
        # model save, and Item.save() auto_now overrides updated_at. Force the
        # real merge-lane status + assignment_group + stall age via a queryset
        # update (no auto_now, no normalization) so the sweep's status filter,
        # threshold, and the assignee write-guard match.
        Item.objects.filter(pk=item.pk).update(
            status='awaiting_merge',
            assignment_group='merge',
            updated_at=timezone.now() - timedelta(hours=updated_hours_ago),
            created_at=timezone.now() - timedelta(hours=updated_hours_ago),
        )
        item.refresh_from_db()
    return item


class CheckMergeStallsTests(TestCase):
    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger')
    @mock.patch('tasks.services.workflow_service.WorkflowService._apply_resolved_assignee')
    def test_reassigns_stalled_item_to_available_merger(self, mock_apply, mock_resolve, mock_mergeable):
        item = _make_item(updated_hours_ago=8)
        mock_resolve.return_value = (42, 'merger@shizuha.com')
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['reassigned'], 1)
        self.assertEqual(result['escalated'], 0)
        mock_apply.assert_called_once()
        args = mock_apply.call_args[0]
        self.assertEqual(args[0].pk, item.pk)
        self.assertEqual(args[1], 42)
        self.assertEqual(args[2], 'merger@shizuha.com')

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger', return_value=(None, None))
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_fires_alert_when_no_distinct_merger(self, mock_alert, mock_resolve, mock_mergeable):
        item = _make_item(updated_hours_ago=8)
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['escalated'], 1)
        self.assertEqual(result['reassigned'], 0)
        mock_alert.assert_called_once()
        args = mock_alert.call_args[0]
        self.assertEqual(args[0].pk, item.pk)

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger')
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_skips_item_within_threshold(self, mock_alert, mock_resolve, mock_mergeable):
        _make_item(updated_hours_ago=2)  # within 6h threshold
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['reassigned'], 0)
        self.assertEqual(result['escalated'], 0)
        mock_resolve.assert_not_called()
        mock_alert.assert_not_called()

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=False)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger')
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_skips_item_without_mergeable_pr(self, mock_alert, mock_resolve, mock_mergeable):
        _make_item(updated_hours_ago=8)
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['reassigned'], 0)
        self.assertEqual(result['escalated'], 0)
        mock_resolve.assert_not_called()
        mock_alert.assert_not_called()

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger')
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_skips_non_awaiting_merge_items(self, mock_alert, mock_resolve, mock_mergeable):
        _make_item(status='todo', updated_hours_ago=8)
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['checked'], 0)
        mock_resolve.assert_not_called()
        mock_alert.assert_not_called()

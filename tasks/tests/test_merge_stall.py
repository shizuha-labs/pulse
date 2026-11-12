"""PLS-1041: merge-lane stall-escalation sweep tests."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project
from tasks.services import merge_stall
from tasks.services.merge_stall import check_merge_stalls


def _make_item(*, status='awaiting_merge', updated_hours_ago=8, key='MS'):
    # get_or_create: tests that stage MULTIPLE items (PLAT-10108 consolidated
    # ready-queue) call this helper twice — a second blind create would violate
    # the (org_id, key) unique on tasks_project.
    proj, _ = Project.objects.get_or_create(
        name='Merge Stall', key='MSK', org_id=1,
    )
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

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger', return_value=(None, None))
    @mock.patch('tasks.services.merge_stall._failover_merger', return_value=(43, 'aoi@shizuha.com'))
    @mock.patch('tasks.services.workflow_service.WorkflowService._apply_resolved_assignee')
    def test_failover_assigns_to_build13_seat_when_rung_offline(self, mock_apply, mock_failover, mock_resolve, mock_mergeable):
        """PLAT-10108: an offline single-rung merge lane auto-fails-over to the
        next BUILD-13 seat instead of stalling."""
        item = _make_item(updated_hours_ago=8)
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['reassigned'], 1)
        self.assertEqual(result['escalated'], 0)
        mock_failover.assert_called_once()
        args = mock_apply.call_args[0]
        self.assertEqual(args[0].pk, item.pk)
        self.assertEqual(args[1], 43)
        self.assertEqual(args[2], 'aoi@shizuha.com')
        item.refresh_from_db()
        self.assertEqual(item.metadata['merge_failover']['to_email'], 'aoi@shizuha.com')
        self.assertEqual(item.metadata['merge_failover']['from_email'], '')

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger', return_value=(None, None))
    @mock.patch('tasks.services.merge_stall._failover_merger', return_value=(None, None))
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_fires_devops_routed_alert_when_no_build13_seat(self, mock_alert, mock_failover, mock_resolve, mock_mergeable):
        """PLAT-10108: no available BUILD-13 seat — the fail-loud zero-click
        alert still fires (devops-routed)."""
        _make_item(updated_hours_ago=8)
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['escalated'], 1)
        self.assertEqual(result['reassigned'], 0)
        mock_alert.assert_called_once()

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger', return_value=(None, None))
    @mock.patch('tasks.services.merge_stall._failover_merger', return_value=(None, None))
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_alert_carries_consolidated_ready_queue(self, mock_alert, mock_failover, mock_resolve, mock_mergeable):
        """PLAT-10108: the consolidated ready-queue (other stalled keys) rides
        with the hand-off alert, not a per-item stall."""
        # item_key is minted by the INGESTION layer, never by Item.save() —
        # raw test creates leave it NULL, so stamp project-scoped keys here.
        item1 = _make_item(updated_hours_ago=8)
        item2 = _make_item(updated_hours_ago=9)
        Item.objects.filter(pk=item1.pk).update(item_key=f'MSK-{item1.pk}')
        Item.objects.filter(pk=item2.pk).update(item_key=f'MSK-{item2.pk}')
        item1.refresh_from_db()
        item2.refresh_from_db()
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['escalated'], 2)
        self.assertEqual(mock_alert.call_count, 2)
        queues = [call[1]['ready_queue'] for call in mock_alert.call_args_list]
        # Each per-item queue carries exactly the OTHER stalled row's key.
        self.assertEqual(
            sorted(map(sorted, queues)),
            sorted([[item1.item_key], [item2.item_key]]),
        )

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch(
        'tasks.services.merge_stall._re_resolve_merger',
        return_value=(43, 'aoi@shizuha.com'),
    )
    @mock.patch('tasks.services.merge_stall._failover_merger')
    @mock.patch('tasks.services.workflow_service.WorkflowService._apply_resolved_assignee')
    def test_normal_resolution_records_via_normal(self, mock_apply, mock_failover, mock_resolve, mock_mergeable):
        """PLAT-10108 P3 (reika review 6879): a normal phase-1 re-resolution
        records via=normal (not a failover) in the merge_reassignment metadata,
        and never invokes the failover path."""
        item = _make_item(updated_hours_ago=8)
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['reassigned'], 1)
        self.assertEqual(result['escalated'], 0)
        mock_failover.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.metadata['merge_failover']['via'], 'normal')

    @mock.patch('tasks.services.merge_stall._mergeable_pr', return_value=True)
    @mock.patch('tasks.services.merge_stall._re_resolve_merger')
    @mock.patch('tasks.services.merge_stall._fire_stall_alert')
    def test_failover_passes_follow_build13_order(self, mock_alert, mock_resolve, mock_mergeable):
        """PLAT-10108 P3 (reika review 6879): the failover passes CONSUME the
        BUILD-13 order — the first pass excludes the later candidate (ryo) so
        the resolver can only land on aoi or another rights-holding merge-lane
        member; ryo is reachable only when the aoi pass found nothing."""
        item = _make_item(updated_hours_ago=8)
        Item.objects.filter(pk=item.pk).update(
            assignee_email='kei@shizuha.com',       # the offline rung
            created_by_email='haru@shizuha.com',    # the author
        )
        # Phase-1 normal resolve finds nothing; pass 1 (aoi-only, ryo
        # hard-excluded) finds nothing; pass 2 lands ryo.
        mock_resolve.side_effect = [
            (None, None), (None, None), (55, 'ryo@shizuha.com'),
        ]
        result = check_merge_stalls(threshold_hours=6)
        self.assertEqual(result['reassigned'], 1)
        calls = mock_resolve.call_args_list
        self.assertEqual(
            len(calls), 3,
            'phase-1 normal resolve + two BUILD-13 failover passes',
        )
        first_pass = calls[1].kwargs['extra_exclude_emails']
        self.assertIn('ryo@shizuha.com', first_pass)
        self.assertIn('kei@shizuha.com', first_pass)
        self.assertIn('haru@shizuha.com', first_pass)
        second_pass = calls[2].kwargs['extra_exclude_emails']
        self.assertNotIn('ryo@shizuha.com', second_pass)
        item.refresh_from_db()
        self.assertEqual(item.metadata['merge_failover']['via'], 'failover')
        self.assertEqual(item.metadata['merge_failover']['to_email'], 'ryo@shizuha.com')

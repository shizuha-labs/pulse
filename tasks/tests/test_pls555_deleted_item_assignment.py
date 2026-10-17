"""PLS-555: a deleted disposable task must not emit an actionable [Task Assigned].

The Hive rollback create-permission preflight creates then IMMEDIATELY deletes a
disposable HIVE task. On assignee set/change the post_save signal dispatches
``send_item_notification(item_id, 'assignment_changed', ctx)`` asynchronously;
that task fetches the row, passes the PLAT-337 (terminal) and PLAT-1357
(reassigned) / PLS-275 (status) guards, then can lose a race to the delete and
deliver an actionable Connect ``[Task Assigned]`` DM for an item that no longer
exists — each stale DM burned a whole agent turn (HIVE-1054 / HIVE-1061).

Contract (regression of PLAT-1357): a delivery-time existence re-check treats a
vanished row as stale — no ``notify_assignment_changed``, observable in logs.
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item
from tasks.tasks import send_item_notification


def _live_task():
    """A persisted, assigned, non-terminal disposable task."""
    return Item.objects.create(
        item_key='HIVE-PLS555',
        title='disposable rollback preflight',
        mode='task',
        status='in_progress',
        assignee_id=808,
        assignee_email='agent@agents.shizuha.io',
        created_by_id=808,
        created_by_email='agent@agents.shizuha.io',
    )


def _ctx(task):
    # Matching dispatched assignee + status so the PLAT-1357/PLS-275 guards pass
    # and execution reaches the PLS-555 existence check.
    return {
        'dispatched_assignee_id': task.assignee_id,
        'dispatched_status': task.status,
        'changed_by': 'system@pulse',
    }


class DeletedItemAssignmentSuppressionTests(TestCase):
    @patch('tasks.services.notifications.notification_service.notify_assignment_changed')
    def test_deleted_before_delivery_sends_nothing(self, mock_notify):
        """The fetch→delete→deliver race: the top fetch returns the row, but it is
        gone by the delivery existence check → zero delivery."""
        task = _live_task()
        task_id = task.id
        ctx = _ctx(task)

        # Simulate the race: the top-of-task fetch still returns the row (as in
        # production — pulse_get_task only 404'd at delivery), but the DB row is
        # already deleted, so the PLS-555 existence guard sees it vanished.
        # `Item` is local-imported inside send_item_notification via
        # `from .models import Item`, so patch the SOURCE manager
        # (`tasks.models.Item.objects.get`), not `tasks.tasks.Item`. The patch
        # only overrides `.get` (the top fetch); `.filter(...).exists()` in the
        # PLS-555 guard stays real and sees the deleted row → suppression.
        Item.objects.filter(id=task_id).delete()
        with patch('tasks.models.Item.objects.get', return_value=task):
            result = send_item_notification(task_id, 'assignment_changed', ctx)

        self.assertIn('deleted before delivery', result)
        mock_notify.assert_not_called()

    @patch('tasks.services.notifications.notification_service.notify_assignment_changed')
    def test_fully_deleted_before_dispatch_also_suppressed(self, mock_notify):
        """Belt-and-suspenders: a row already gone before the top fetch is
        suppressed by the existing DoesNotExist handler (no delivery)."""
        task = _live_task()
        task_id = task.id
        Item.objects.filter(id=task_id).delete()
        result = send_item_notification(task_id, 'assignment_changed', _ctx(task))
        self.assertIn('not found', result)
        mock_notify.assert_not_called()

    @patch('tasks.services.notifications.notification_service.notify_assignment_changed')
    def test_live_task_still_delivers(self, mock_notify):
        """A genuinely live assigned task still delivers — the guard is scoped to
        the vanished-row case only."""
        task = _live_task()
        result = send_item_notification(task.id, 'assignment_changed', _ctx(task))
        mock_notify.assert_called_once()
        self.assertNotIn('deleted', result)

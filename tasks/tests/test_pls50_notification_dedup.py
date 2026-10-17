"""
Regression tests for PLS-50 notification dedup, staleness guard, and attribution.

PLS-50 fixed three issues in the notification pipeline:
  1. Double DM on status/assignee/priority PATCH — signal + direct view call = 2 messages
  2. Stale status_changed Celery task delivered an out-of-order "[In Progress]" DM for
     an item that had already moved to merged/completed
  3. Auto-sweep transitions mis-credited to the task's creator instead of 'system'

PLS-51: this file adds regression coverage so each fix cannot silently revert.
"""
from unittest.mock import patch, call

from django.db import transaction
from django.test import TestCase

from tasks.models import Item, ItemActivity

SCHEDULER_USER_ID = 0
SCHEDULER_USER_EMAIL = 'scheduler@pulse.shizuha.com'


def _make_item(**kwargs):
    """Helper: create an Item with a minimal actor context so signals don't skip activity."""
    defaults = dict(
        mode='task',
        title='Test Item',
        status='open',
        # Unseeded workflow → Item.save applies no status coercion, so a status
        # like 'in_review' survives (under 'simple' it would coerce to
        # 'in_progress' and read as a stale status_changed notification).
        workflow_name='autonomous-dev',
        created_by_id=1,
        created_by_email='creator@test.com',
    )
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


class NotificationDedupTests(TestCase):
    """PLS-50 regression: a single field PATCH must fire exactly one send_item_notification.delay."""

    def setUp(self):
        self.item = _make_item(status='open')

    @patch('tasks.tasks.send_item_notification')
    def test_single_status_change_fires_one_delay(self, mock_task):
        """Changing status once produces exactly one delay call — not two."""
        self.item.status = 'in_progress'
        with self.captureOnCommitCallbacks(execute=True):
            self.item.save()

        mock_task.delay.assert_called_once()
        args = mock_task.delay.call_args[0]
        self.assertEqual(args[0], str(self.item.pk))
        self.assertEqual(args[1], 'status_changed')

    @patch('tasks.tasks.send_item_notification')
    def test_priority_change_fires_one_delay(self, mock_task):
        """Changing priority once produces exactly one delay call."""
        self.item.priority = 'high'
        with self.captureOnCommitCallbacks(execute=True):
            self.item.save()

        mock_task.delay.assert_called_once()
        args = mock_task.delay.call_args[0]
        self.assertEqual(args[0], str(self.item.pk))
        self.assertEqual(args[1], 'priority_changed')

    @patch('tasks.tasks.send_item_notification')
    def test_title_change_fires_no_delay(self, mock_task):
        """Updating a non-notification field (title) fires no delay — no spurious DM."""
        self.item.title = 'Updated title'
        with self.captureOnCommitCallbacks(execute=True):
            self.item.save()

        mock_task.delay.assert_not_called()

    @patch('tasks.tasks.send_item_notification')
    def test_simultaneous_status_and_title_change_fires_one_delay(self, mock_task):
        """Status + title change in one save fires exactly one delay (for status only)."""
        self.item.status = 'in_review'
        self.item.title = 'Also renamed'
        with self.captureOnCommitCallbacks(execute=True):
            self.item.save()

        mock_task.delay.assert_called_once()
        args = mock_task.delay.call_args[0]
        self.assertEqual(args[1], 'status_changed')

    @patch('tasks.tasks.send_item_notification')
    def test_atomic_handoff_notifications_wait_for_commit(self, mock_task):
        """Workers cannot observe the old stage/owner for a current handoff."""
        self.item.assignee_id = 1
        self.item.assignee_email = 'old-owner@shizuha.com'
        with self.captureOnCommitCallbacks(execute=True):
            self.item.save(
                update_fields=['assignee_id', 'assignee_email', 'updated_at'],
            )
        mock_task.reset_mock()

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                self.item.status = 'in_review'
                self.item.assignee_id = 2
                self.item.assignee_email = 'new-owner@shizuha.com'
                self.item.save()
                mock_task.delay.assert_not_called()
            # TestCase owns an outer transaction, so capture executes the
            # callbacks after this block. They must still be pending here.
            mock_task.delay.assert_not_called()

        self.assertEqual(len(callbacks), 2)
        events = [entry.args[1] for entry in mock_task.delay.call_args_list]
        self.assertEqual(events, ['status_changed', 'assignment_changed'])
        assignment_context = mock_task.delay.call_args_list[1].args[2]
        self.assertEqual(assignment_context['dispatched_assignee_id'], '2')
        self.assertEqual(assignment_context['dispatched_status'], 'in_review')

    @patch('tasks.tasks.send_item_notification')
    def test_rolled_back_handoff_discards_notifications(self, mock_task):
        """A failed atomic handoff must not publish ghost status/owner events."""
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaisesRegex(RuntimeError, 'rollback handoff'):
                with transaction.atomic():
                    self.item.status = 'in_review'
                    self.item.assignee_id = 2
                    self.item.assignee_email = 'new-owner@shizuha.com'
                    self.item.save()
                    raise RuntimeError('rollback handoff')

        self.assertEqual(callbacks, [])
        mock_task.delay.assert_not_called()


class StalenessGuardTests(TestCase):
    """PLS-50 regression: a stale status_changed Celery task must be silently discarded."""

    def setUp(self):
        # Item has already moved to 'completed' — any in-flight 'status_changed'
        # task dispatched for an earlier transition is now stale.
        self.item = _make_item(status='completed')

    @patch('tasks.services.notifications.notification_service')
    def test_stale_status_changed_is_skipped(self, mock_notif):
        """send_item_notification with new_status != item.status must not call notify_status_changed."""
        from tasks.tasks import send_item_notification

        result = send_item_notification.apply(args=[
            str(self.item.pk),
            'status_changed',
            {
                'new_status': 'in_review',   # stale — item is now 'completed'
                'old_status': 'open',
                'changed_by': 'agent@shizuha.com',
                'exclude_user_id': None,
            },
        ]).get()

        mock_notif.notify_status_changed.assert_not_called()
        self.assertIn('Stale status_changed skipped', result)
        self.assertIn(str(self.item.pk), result)

    @patch('tasks.services.notifications.notification_service')
    def test_current_status_changed_is_delivered(self, mock_notif):
        """send_item_notification with new_status == item.status must call notify_status_changed."""
        item = _make_item(status='in_review')
        from tasks.tasks import send_item_notification

        send_item_notification.apply(args=[
            str(item.pk),
            'status_changed',
            {
                'new_status': 'in_review',   # matches current status — not stale
                'old_status': 'open',
                'changed_by': 'agent@shizuha.com',
                'exclude_user_id': None,
            },
        ])

        mock_notif.notify_status_changed.assert_called_once()

    @patch('tasks.services.notifications.notification_service')
    def test_completed_event_is_never_stale(self, mock_notif):
        """'completed' event bypasses staleness guard entirely — it always delivers."""
        item = _make_item(status='completed')
        from tasks.tasks import send_item_notification

        send_item_notification.apply(args=[
            str(item.pk),
            'completed',
            {'changed_by': 'agent@shizuha.com', 'exclude_user_id': None},
        ])

        mock_notif.notify_task_completed.assert_called_once()


class AssignmentReplayGuardTests(TestCase):
    """PLAT-1357 regression: a stale assignment_changed Celery task must be
    suppressed when the item has since been reassigned or reached a terminal
    state, so agents don't receive replayed actionable '[Task Assigned]' DMs."""

    @patch('tasks.services.notifications.notification_service')
    def test_stale_reassigned_assignment_is_skipped(self, mock_notif):
        """Item reassigned to B after the event was queued for A: don't deliver."""
        item = _make_item(status='in_progress', assignee_id=2)  # current assignee = B(2)
        from tasks.tasks import send_item_notification

        result = send_item_notification.apply(args=[
            str(item.pk),
            'assignment_changed',
            {
                'dispatched_assignee_id': '1',   # queued for A(1) — now stale
                'changed_by': 'agent@shizuha.com',
                'exclude_user_id': None,
            },
        ]).get()

        mock_notif.notify_assignment_changed.assert_not_called()
        self.assertIn('Stale assignment skipped', result)
        self.assertIn(str(item.pk), result)


    @patch('tasks.services.notifications.notification_service')
    def test_stale_assignment_status_is_skipped_even_when_assignee_matches(self, mock_notif):
        """PLS-275: active-but-advanced assignment replays must not notify."""
        item = _make_item(status='in_review', assignee_id=1)
        from tasks.tasks import send_item_notification

        result = send_item_notification.apply(args=[
            str(item.pk),
            'assignment_changed',
            {
                'dispatched_assignee_id': '1',
                'dispatched_status': 'needs_triage',
                'changed_by': 'workflow',
                'exclude_user_id': None,
            },
        ]).get()

        mock_notif.notify_assignment_changed.assert_not_called()
        self.assertIn('Stale assignment skipped', result)
        self.assertIn('status now in_review', result)

    @patch('tasks.services.notifications.notification_service')
    def test_current_assignment_is_delivered(self, mock_notif):
        """dispatched assignee matches current assignee: deliver as normal."""
        item = _make_item(status='in_progress', assignee_id=1)
        from tasks.tasks import send_item_notification

        send_item_notification.apply(args=[
            str(item.pk),
            'assignment_changed',
            {
                'dispatched_assignee_id': '1',   # matches current assignee
                'dispatched_status': 'in_progress',
                'changed_by': 'agent@shizuha.com',
                'exclude_user_id': None,
            },
        ]).get()

        mock_notif.notify_assignment_changed.assert_called_once()

    @patch('tasks.services.notifications.notification_service')
    def test_terminal_assignment_is_skipped(self, mock_notif):
        """PLAT-337/PLAT-1357: a terminal item never fires an actionable assignment."""
        item = _make_item(status='completed', assignee_id=1)
        from tasks.tasks import send_item_notification

        result = send_item_notification.apply(args=[
            str(item.pk),
            'assignment_changed',
            {
                'dispatched_assignee_id': '1',
                'changed_by': 'agent@shizuha.com',
                'exclude_user_id': None,
            },
        ]).get()

        mock_notif.notify_assignment_changed.assert_not_called()
        self.assertIn('terminal item', result)

    @patch('tasks.services.notifications.notification_service')
    def test_assignment_without_dispatched_id_still_delivers(self, mock_notif):
        """Backward-compat: events queued before PLAT-1357 (no dispatched_assignee_id)
        are not spuriously suppressed — the guard only fires when the field is present."""
        item = _make_item(status='in_progress', assignee_id=7)
        from tasks.tasks import send_item_notification

        send_item_notification.apply(args=[
            str(item.pk),
            'assignment_changed',
            {'changed_by': 'agent@shizuha.com', 'exclude_user_id': None},
        ]).get()

        mock_notif.notify_assignment_changed.assert_called_once()


class SystemAttributionTests(TestCase):
    """PLS-50 regression: scheduler-fired transitions are credited to 'system', not the task creator."""

    @patch('tasks.tasks.send_item_notification')
    def test_activity_actor_override_credited_to_system(self, _mock_task):
        """_activity_actor sentinel (0, scheduler email) produces a system-attributed activity row."""
        item = _make_item(status='open', created_by_id=42, created_by_email='creator@example.com')

        item._activity_actor = (SCHEDULER_USER_ID, SCHEDULER_USER_EMAIL)
        item.status = 'in_progress'
        item.save()

        activity = ItemActivity.objects.filter(
            item=item,
            action='status_changed',
        ).first()

        self.assertIsNotNone(activity, 'Expected a status_changed activity row')
        self.assertEqual(activity.user_email, SCHEDULER_USER_EMAIL)
        self.assertEqual(activity.user_id, SCHEDULER_USER_ID)

    @patch('tasks.tasks.send_item_notification')
    def test_no_actor_override_falls_back_to_created_by(self, _mock_task):
        """Without _activity_actor, the activity is credited to created_by (HTTP-less fallback)."""
        creator_id = 77
        creator_email = 'creator@example.com'
        item = _make_item(
            status='open',
            created_by_id=creator_id,
            created_by_email=creator_email,
        )

        # No _activity_actor, no request threadlocal → signal falls back to created_by_*
        item.status = 'in_progress'
        item.save()

        activity = ItemActivity.objects.filter(
            item=item,
            action='status_changed',
        ).first()

        self.assertIsNotNone(activity, 'Expected a status_changed activity row')
        self.assertEqual(activity.user_id, creator_id)
        self.assertEqual(activity.user_email, creator_email)

    @patch('tasks.tasks.send_item_notification')
    def test_system_actor_not_fallback_to_creator(self, _mock_task):
        """
        When _activity_actor is explicitly set to system, the creator's ID must NOT appear.
        This was the pre-PLS-50 bug: SCHEDULER_USER_ID = 0 was falsy, so `if not user_id`
        silently fell back to created_by, re-crediting every scheduler fire to the task creator.
        """
        creator_id = 99
        item = _make_item(
            status='open',
            created_by_id=creator_id,
            created_by_email='creator@example.com',
        )

        item._activity_actor = (SCHEDULER_USER_ID, SCHEDULER_USER_EMAIL)
        item.status = 'in_progress'
        item.save()

        activity = ItemActivity.objects.filter(
            item=item,
            action='status_changed',
        ).first()

        self.assertIsNotNone(activity)
        # Must be system (0), NOT the creator (99)
        self.assertEqual(activity.user_id, SCHEDULER_USER_ID)
        self.assertNotEqual(activity.user_id, creator_id)
        self.assertEqual(activity.user_email, SCHEDULER_USER_EMAIL)

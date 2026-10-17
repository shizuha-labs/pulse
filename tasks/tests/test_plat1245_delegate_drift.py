"""
Regression for PLAT-1245: the `tasks_notification_delegate` config table was
dropped by the PLS-223 0077/0078 merge-migration leaves (no model, no
migration, no writer), but `UserNotificationService._get_delegates` still ran a
raw SELECT against it. On Postgres that raises ProgrammingError mid-transaction,
poisoning the whole transaction — which errored the ci-test suite and blocked
every pulse deploy. The read path must degrade to "no delegates" when the table
is absent, without raising.
"""
from unittest import mock

from django.db import connection
from django.test import TestCase

from tasks.models import Item, Project, Notification
from tasks.services.notifications import notification_service


class DelegateTableDriftTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, lead_id=11, default_assignee_id=22,
        )

    def test_delegate_table_is_present_in_the_schema(self):
        # PLS-228 ADOPTED the table: NotificationDelegate model + 0080
        # create-if-absent migration guarantee it exists on every DB (the
        # original PLAT-1245 premise — "no migration creates this table" —
        # no longer holds). Guard the new invariant instead so a future
        # migration dropping it without reworking _get_delegates fails here.
        self.assertIn('tasks_notification_delegate', connection.introspection.table_names())

    def test_get_delegates_returns_empty_when_no_rows(self):
        # The real (unmocked) read path must return [] and NOT raise.
        self.assertEqual(notification_service._get_delegates(999), [])
        self.assertEqual(notification_service._get_delegates(999, event_type='alert'), [])

    def test_notify_alert_does_not_error_on_missing_delegate_table(self):
        # End-to-end: notify_alert -> _create_delegated_notifications ->
        # _get_delegates must complete cleanly and produce no delegated copies,
        # never poisoning the transaction (the ci-test cascade failure).
        alert = Item.objects.create(
            mode='alert', item_type='alert.warning', title='t', description='d',
            source='alertmanager', status='firing', severity='warning',
            priority='high', project=self.project, assignment_group='DevOps',
        )
        with mock.patch.object(notification_service, '_send_push', return_value=True), \
             mock.patch.object(notification_service, '_deliver_to_agent'), \
             mock.patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            result = notification_service.notify_alert(alert, user_ids=[101])
        self.assertIn(101, result['recipients'])
        # No delegated notifications (no config table => no delegates).
        self.assertFalse(
            Notification.objects.filter(item=alert, delegated_from_id__isnull=False).exists()
        )

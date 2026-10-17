"""PLS-228: NotificationDelegate model + ORM _get_delegates on a FRESH DB.

Before this fix, _get_delegates ran raw SQL against tasks_notification_delegate —
a prod-only table with no model/migration. On a fresh DB (this test DB, CI, new
envs) the table did not exist, so the query raised ProgrammingError and aborted
the enclosing transaction (which OOM-killed the PLAT-1219 CI gate). These tests
run against a fresh test DB and would have failed with ProgrammingError before
the model/migration existed.
"""
import json

from django.test import TestCase

from tasks.models import NotificationDelegate
from tasks.services.notifications import UserNotificationService


class GetDelegatesFreshDBTests(TestCase):
    def setUp(self):
        self.svc = UserNotificationService()

    def test_no_delegates_returns_empty_not_error(self):
        # The key regression: a query against the table on a fresh DB must NOT
        # raise (it used to ProgrammingError on the missing table).
        self.assertEqual(self.svc._get_delegates(42), [])

    def test_active_delegate_without_event_scope_matches_any_event(self):
        NotificationDelegate.objects.create(
            user_id=1, delegate_id=2, delegate_email='d@x.com',
            delegated_events='', is_active=True)
        self.assertEqual(self.svc._get_delegates(1, 'assignment'),
                         [{'id': 2, 'email': 'd@x.com'}])
        self.assertEqual(self.svc._get_delegates(1), [{'id': 2, 'email': 'd@x.com'}])

    def test_event_scoped_delegate_filters(self):
        NotificationDelegate.objects.create(
            user_id=1, delegate_id=2, delegate_email='d@x.com',
            delegated_events=json.dumps(['assignment', 'comment']), is_active=True)
        self.assertEqual(self.svc._get_delegates(1, 'assignment'),
                         [{'id': 2, 'email': 'd@x.com'}])
        self.assertEqual(self.svc._get_delegates(1, 'overdue'), [])

    def test_inactive_delegate_excluded(self):
        NotificationDelegate.objects.create(
            user_id=1, delegate_id=2, delegate_email='d@x.com',
            delegated_events='', is_active=False)
        self.assertEqual(self.svc._get_delegates(1), [])

    def test_only_delegating_users_rows_returned(self):
        NotificationDelegate.objects.create(user_id=1, delegate_id=2,
                                            delegate_email='a@x.com', is_active=True)
        NotificationDelegate.objects.create(user_id=9, delegate_id=3,
                                            delegate_email='b@x.com', is_active=True)
        out = self.svc._get_delegates(1)
        self.assertEqual(out, [{'id': 2, 'email': 'a@x.com'}])

    def test_malformed_delegated_events_does_not_crash(self):
        NotificationDelegate.objects.create(
            user_id=1, delegate_id=2, delegate_email='d@x.com',
            delegated_events='{not json', is_active=True)
        # bad JSON parses to [] (no scope) → matches any event.
        self.assertEqual(self.svc._get_delegates(1, 'assignment'),
                         [{'id': 2, 'email': 'd@x.com'}])

    def test_table_is_managed_by_the_orm(self):
        # Sanity: the model maps to the adopted table name.
        self.assertEqual(NotificationDelegate._meta.db_table, 'tasks_notification_delegate')

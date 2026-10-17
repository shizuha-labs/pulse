"""ID-39 first-wave Pulse lifecycle cleanup consumer coverage."""
import hashlib
import hmac
import json
import time
import uuid

from django.test import TestCase, override_settings
from django.urls import reverse

from tasks.models import Item, ItemActivity, UserLifecycleEventReceipt


SECRET = 'test-lifecycle-secret'


def _signed_headers(body, *, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    sig = hmac.new(
        SECRET.encode('utf-8'),
        ts.encode('utf-8') + b'.' + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        'HTTP_X_SHIZUHA_SERVICE': 'shizuha-id',
        'HTTP_X_SHIZUHA_TIMESTAMP': ts,
        'HTTP_X_SHIZUHA_SIGNATURE': f'sha256={sig}',
    }


def _json_body(payload):
    return json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')


@override_settings(PULSE_USER_LIFECYCLE_CONSUMER_SECRET=SECRET)
class InternalUserLifecycleConsumerTests(TestCase):
    def _assigned_item(self, user_id=4242, email='deleted-user@shizuha.com', title='Assigned to deleted user'):
        return Item.objects.create(
            mode='task',
            title=title,
            assignee_id=user_id,
            assignee_email=email,
            created_by_id=1,
            created_by_email='system@pulse',
            workflow_name='simple',
            status='todo',
        )

    def _post_event(self, payload, **header_overrides):
        body = _json_body(payload)
        headers = _signed_headers(body)
        headers.update(header_overrides)
        return self.client.generic(
            'POST',
            reverse('internal-user-lifecycle-events'),
            data=body,
            content_type='application/json',
            **headers,
        )

    def test_unauthenticated_legacy_unassign_is_rejected(self):
        item = self._assigned_item()
        response = self.client.post(reverse('internal-user-unassign', args=[4242]), data={}, content_type='application/json')

        self.assertEqual(response.status_code, 401)
        item.refresh_from_db()
        self.assertEqual(item.assignee_id, 4242)
        self.assertEqual(item.assignee_email, 'deleted-user@shizuha.com')

    def test_lifecycle_event_clears_assignee_atomically_and_is_idempotent(self):
        item = self._assigned_item()
        event_id = str(uuid.uuid4())
        payload = {
            'event_id': event_id,
            'event_type': UserLifecycleEventReceipt.EVENT_DELETED,
            'user_id': 4242,
            'email': 'deleted-user@shizuha.com',
        }

        first = self._post_event(payload)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()['duplicate'])
        self.assertEqual(first.json()['items_unassigned'], 1)
        item.refresh_from_db()
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)
        self.assertTrue(
            ItemActivity.objects.filter(item=item, action='unassigned').exists(),
            'consumer should audit the cleanup through item.unassign()/signals',
        )
        self.assertTrue(UserLifecycleEventReceipt.objects.filter(event_id=event_id).exists())

        second = self._post_event(payload)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(second.json()['items_unassigned'], 1)
        self.assertEqual(UserLifecycleEventReceipt.objects.filter(event_id=event_id).count(), 1)
        self.assertEqual(ItemActivity.objects.filter(item=item, action='unassigned').count(), 1)

    def test_lifecycle_event_clears_email_only_and_alias_assignees(self):
        email_only = self._assigned_item(
            user_id=None,
            email='deleted-user@shizuha.com',
            title='Email-only assignee',
        )
        stale_id = self._assigned_item(
            user_id=9999,
            email='deleted-user@agents.shizuha.io',
            title='Alias with stale user id',
        )
        id_only_legacy = self._assigned_item(
            user_id=4242,
            email='',
            title='Legacy id-only assignee',
        )
        unrelated_same_id_with_email = self._assigned_item(
            user_id=4242,
            email='someone-else@shizuha.com',
            title='Different canonical assignee',
        )
        payload = {
            'event_id': str(uuid.uuid4()),
            'event_type': UserLifecycleEventReceipt.EVENT_DEACTIVATED,
            'user_id': 4242,
            'email': 'deleted-user@shizuha.com',
        }

        response = self._post_event(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items_unassigned'], 3)
        for item in (email_only, stale_id, id_only_legacy):
            item.refresh_from_db()
            self.assertIsNone(item.assignee_id)
            self.assertIsNone(item.assignee_email)
        unrelated_same_id_with_email.refresh_from_db()
        self.assertEqual(unrelated_same_id_with_email.assignee_id, 4242)
        self.assertEqual(unrelated_same_id_with_email.assignee_email, 'someone-else@shizuha.com')

    def test_reactivated_event_is_authenticated_deduped_noop(self):
        item = self._assigned_item()
        payload = {
            'event_id': str(uuid.uuid4()),
            'event_type': UserLifecycleEventReceipt.EVENT_REACTIVATED,
            'user_id': 4242,
            'email': 'deleted-user@shizuha.com',
        }

        response = self._post_event(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items_unassigned'], 0)
        item.refresh_from_db()
        self.assertEqual(item.assignee_id, 4242)
        self.assertEqual(UserLifecycleEventReceipt.objects.get().event_type, UserLifecycleEventReceipt.EVENT_REACTIVATED)

    def test_bad_signature_rejected(self):
        payload = {
            'event_id': str(uuid.uuid4()),
            'event_type': UserLifecycleEventReceipt.EVENT_DEACTIVATED,
            'user_id': 4242,
            'email': 'deleted-user@shizuha.com',
        }
        body = _json_body(payload)
        response = self.client.generic(
            'POST',
            reverse('internal-user-lifecycle-events'),
            data=body,
            content_type='application/json',
            HTTP_X_SHIZUHA_SERVICE='shizuha-id',
            HTTP_X_SHIZUHA_TIMESTAMP=str(int(time.time())),
            HTTP_X_SHIZUHA_SIGNATURE='sha256=bad',
        )

        self.assertEqual(response.status_code, 401)

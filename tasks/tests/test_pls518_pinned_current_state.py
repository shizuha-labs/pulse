"""
PLS-518: pinned "current state" handoff summary — backend tests.

Product finding (PLAT-528): task truth accumulates as an append-only comment
thread; every failover/rebalance seat must reconstruct "where are we now" by
paging the head of a very long thread. PLS-518 adds:
1. a pinned `current_state` field on Item (distinct from description), editable
   by the current assignee (or staff), timestamped on write;
2. comment classing — `is_system` on CommentSerializer derived from the
   existing origin_kind provenance (workflow-lifecycle/audit/focus-refused are
   system; user-authored + legacy-untyped are substantive).

Invariants (EVOL-54):
- single-writer: only the current assignee (or staff) may update the pin;
  a stale/former assignee is denied.
- timestamped: every pin write stamps current_state_updated_at.
- the pin is a handoff aid, NOT authoritative task state (workflow/status is).
"""
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import Comment, Item
from tasks.serializers import CommentSerializer, ItemSerializer

User = get_user_model()


class PinnedCurrentStateModelTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='nagi', email='nagi@shizuha.com', password='x',
            is_staff=True, is_superuser=True,
        )
        self.item = Item.objects.create(
            title='Handoff demo', mode='task',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )

    def test_fields_default_empty(self):
        self.assertEqual(self.item.current_state, '')
        self.assertIsNone(self.item.current_state_updated_at)

    def test_serializer_exposes_current_state(self):
        self.item.current_state = 'Next = write tests.'
        self.item.current_state_updated_at = timezone.now()
        self.item.save()
        data = ItemSerializer(self.item).data
        self.assertEqual(data['current_state'], 'Next = write tests.')
        self.assertIsNotNone(data['current_state_updated_at'])


class CommentIsSystemTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='nagi', email='nagi@shizuha.com', password='x',
            is_staff=True, is_superuser=True,
        )
        self.item = Item.objects.create(
            title='Comment classing demo', mode='task',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )

    def _comment(self, origin_kind):
        return Comment.objects.create(
            item=self.item, author_id=self.user.id,
            author_email=self.user.email, content='x',
            origin_kind=origin_kind,
        )

    def test_system_kinds_classified_system(self):
        for kind in (
            Comment.ORIGIN_WORKFLOW_LIFECYCLE,
            Comment.ORIGIN_VALIDATOR_AUDIT,
            Comment.ORIGIN_RESOLVER_AUDIT,
            Comment.ORIGIN_FOCUS_REFUSED,
        ):
            self.assertTrue(
                CommentSerializer(self._comment(kind)).data['is_system'],
                f'{kind} should be system',
            )

    def test_substantive_kinds_classified_not_system(self):
        for kind in (
            Comment.ORIGIN_USER_AUTHORED,
            Comment.ORIGIN_LEGACY_UNTYPED,
        ):
            self.assertFalse(
                CommentSerializer(self._comment(kind)).data['is_system'],
                f'{kind} should be substantive',
            )


class PinnedCurrentStateApiTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.assignee = User.objects.create_user(
            username='sara', email='sara@shizuha.com', password='x',
        )
        self.other = User.objects.create_user(
            username='kai', email='kai@shizuha.com', password='x',
        )
        self.staff = User.objects.create_user(
            username='staff', email='staff@shizuha.com', password='x',
            is_staff=True, is_superuser=True,
        )
        self.item = Item.objects.create(
            title='Pinned state API demo', mode='task',
            assignee_id=self.assignee.id, assignee_email=self.assignee.email,
            workflow_name='autonomous-dev', status='in_progress',
        )

    def test_assignee_can_set_current_state_and_gets_timestamp(self):
        self.client.force_authenticate(user=self.assignee)
        resp = self.client.patch(
            f'/shizuha-pulse/api/items/{self.item.id}/',
            {'current_state': 'Implementing the pin; next = tests.'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_state, 'Implementing the pin; next = tests.')
        self.assertIsNotNone(self.item.current_state_updated_at)

    def test_assignee_can_clear_current_state(self):
        self.item.current_state = 'Old pin'
        self.item.save()
        self.client.force_authenticate(user=self.assignee)
        resp = self.client.patch(
            f'/shizuha-pulse/api/items/{self.item.id}/',
            {'current_state': ''},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_state, '')
        self.assertIsNotNone(self.item.current_state_updated_at)

    def test_non_assignee_denied(self):
        self.client.force_authenticate(user=self.other)
        resp = self.client.patch(
            f'/shizuha-pulse/api/items/{self.item.id}/',
            {'current_state': 'Hijacked pin'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_state, '')
        self.assertIsNone(self.item.current_state_updated_at)

    def test_staff_can_set_current_state(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.patch(
            f'/shizuha-pulse/api/items/{self.item.id}/',
            {'current_state': 'Staff-set pin'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_state, 'Staff-set pin')

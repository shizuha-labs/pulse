"""PLAT-11156 mechanism 3: pin-staleness flag on the listing surface.

A substantive (non-system) comment newer than the pinned current_state means
the pin predates live work-state — the exact stale-read class that parked
held tasks on pins instead of comments (SCLI-76 / PLAT-10526). The flag is
exposed as ``current_state_stale`` on ItemListSerializer so seats see it in
listings without opening the item.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import Comment, Item
from tasks.serializers import ItemListSerializer

User = get_user_model()


class PinStalenessListingTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='pin-tester', email='pin-tester@shizuha.com',
            password='x', is_staff=True, is_superuser=True,
        )
        self.item = Item.objects.create(
            title='pin staleness specimen', mode='task',
            assignee_id=self.user.id, assignee_email=self.user.email,
        )

    def _pin(self, minutes_ago):
        self.item.current_state = 'handoff pin'
        self.item.current_state_updated_at = timezone.now() - timedelta(minutes=minutes_ago)
        self.item.save(update_fields=['current_state', 'current_state_updated_at'])

    def _comment(self, minutes_ago, origin_kind=Comment.ORIGIN_USER_AUTHORED):
        # created_at is auto_now_add — backdate after the insert.
        c = Comment.objects.create(
            item=self.item, author_id=self.user.id,
            author_email=self.user.email, author_name='pin-tester',
            content='substantive reply', origin_kind=origin_kind,
        )
        Comment.objects.filter(pk=c.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        c.refresh_from_db()
        return c

    def test_substantive_comment_after_pin_is_stale(self):
        """The PLAT-10526 shape: pin landed, then a substantive reply — stale."""
        self._pin(60)
        self._comment(30)
        data = ItemListSerializer(self.item).data
        self.assertTrue(data['current_state_stale'])

    def test_system_comment_after_pin_is_not_stale(self):
        """Failover/rebalance chatter is exactly the noise the pin cuts
        through — system-origin comments never make the pin stale."""
        self._pin(60)
        self._comment(30, origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE)
        data = ItemListSerializer(self.item).data
        self.assertFalse(data['current_state_stale'])

    def test_comment_before_pin_is_not_stale(self):
        self._pin(30)
        self._comment(60)
        data = ItemListSerializer(self.item).data
        self.assertFalse(data['current_state_stale'])

    def test_null_pin_is_never_stale(self):
        self._comment(10)
        data = ItemListSerializer(self.item).data
        self.assertFalse(data['current_state_stale'])

    def test_my_tasks_endpoint_exposes_flag_with_annotation(self):
        """The my_tasks listing sets `_substantive_after_pin` (single query,
        no N+1) and the flag flows through to the response payload."""
        self._pin(60)
        self._comment(30)
        self.client.force_authenticate(user=self.user)
        resp = self.client.get('/api/items/my_tasks/')
        self.assertEqual(resp.status_code, 200)
        rows = [r for r in resp.data['items'] if r['id'] == self.item.id]
        self.assertTrue(rows, 'specimen missing from my_tasks response')
        self.assertTrue(rows[0]['current_state_stale'])

    def test_annotated_zero_count_is_not_stale(self):
        """Annotated path with count 0 (comment older than pin) → not stale."""
        self._pin(30)
        self._comment(60)
        self.item._substantive_after_pin = 0
        self.assertFalse(ItemListSerializer(self.item).data['current_state_stale'])

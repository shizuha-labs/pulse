"""
PLS-88: pulse_get_my_tasks (my_tasks endpoint) must expose last_comment_at +
comment_count per item, so an agent's heartbeat loop can detect new reviewer
feedback on held tasks without a per-task pulse_list_comments fetch (the
SCLI-76 churn pattern).

Acceptance:
- my_tasks includes last_comment_at and comment_count for each item
- fields are populated from the task's comment thread (not nested issues)
- last_comment_at is null when a task has zero comments
- response format stays backward-compatible (fields are additive)
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item, Comment

User = get_user_model()

MY_TASKS_URL = '/shizuha-pulse/api/items/my_tasks/?queue_order=true'


class CommentMetaInMyTasksTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='ryo', email='ryo@shizuha.com', password='x',
            is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(user=self.user)

    def _item(self, title, **kw):
        kw.setdefault('workflow_name', 'software-development')
        kw.setdefault('status', 'in_review')
        return Item.objects.create(
            title=title, mode='task',
            assignee_id=self.user.id, assignee_email=self.user.email,
            **kw,
        )

    def _comment(self, item, content, when=None):
        c = Comment.objects.create(
            item=item, author_id=self.user.id,
            author_email=self.user.email, content=content,
        )
        if when is not None:
            # created_at is auto_now_add; override explicitly to test ordering.
            Comment.objects.filter(pk=c.pk).update(created_at=when)
        return c

    def _by_title(self):
        resp = self.client.get(MY_TASKS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        return {it['title']: it for it in resp.data['items']}

    def test_fields_present_and_additive(self):
        self._item('plain-task')
        item = self._by_title()['plain-task']
        # Additive: both new fields present alongside the existing ones.
        self.assertIn('last_comment_at', item)
        self.assertIn('comment_count', item)
        self.assertIn('status', item)  # existing field still there

    def test_count_and_last_comment_at_reflect_thread(self):
        now = timezone.now()
        task = self._item('reviewed-task')
        self._comment(task, 'first', when=now - timedelta(hours=2))
        self._comment(task, 'newest', when=now - timedelta(minutes=5))

        item = self._by_title()['reviewed-task']
        self.assertEqual(item['comment_count'], 2)
        # last_comment_at is the most-recent comment, not the oldest.
        self.assertIsNotNone(item['last_comment_at'])
        self.assertTrue(
            item['last_comment_at'].startswith((now - timedelta(minutes=5)).isoformat()[:16]),
            f"last_comment_at {item['last_comment_at']} is not the newest comment",
        )

    def test_zero_comments_is_null_and_zero(self):
        self._item('untouched-task')
        item = self._by_title()['untouched-task']
        self.assertEqual(item['comment_count'], 0)
        self.assertIsNone(item['last_comment_at'])

    def test_count_is_per_task_not_shared(self):
        a = self._item('task-a')
        b = self._item('task-b')
        self._comment(a, 'only on a')
        self._comment(a, 'second on a')
        self._comment(b, 'only on b')

        by_title = self._by_title()
        self.assertEqual(by_title['task-a']['comment_count'], 2)
        self.assertEqual(by_title['task-b']['comment_count'], 1)

    def _system_comment(self, item, content, when=None):
        """PLS-1294: mint a system lifecycle comment the way the
        MechanicalCommentFactory does (typed origin_kind, not USER_AUTHORED)."""
        c = Comment.objects.create(
            item=item, author_id=self.user.id,
            author_email=self.user.email, content=content,
            origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        )
        if when is not None:
            Comment.objects.filter(pk=c.pk).update(created_at=when)
        return c

    def test_pls1294_system_comments_excluded_from_attention_count(self):
        # Epic evidence: ~30 lifecycle entries on ONE task in a 3-day window.
        # 28 system events + 5 substantive comments must show 5 everywhere.
        now = timezone.now()
        task = self._item('failover-storm-task')
        for i in range(5):
            self._comment(task, f'substantive {i}', when=now - timedelta(hours=6 - i))
        for i in range(28):
            self._system_comment(
                task, f'failover/rebalance chatter {i}',
                when=now - timedelta(minutes=30 - i),
            )

        item = self._by_title()['failover-storm-task']
        self.assertEqual(item['comment_count'], 5)
        # last_comment_at is the newest SUBSTANTIVE comment — a system-only
        # tail must not falsely signal new reviewer feedback. The seeded
        # newest substantive is `substantive 4` at now-2h (hours=6-i, i=0..4).
        # Compare parsed datetimes with a bounded tolerance instead of a
        # minute-truncated startswith: the truncation made the assertion
        # boundary-straddle flaky, and the earlier literal expected now-1h
        # while the data seeds now-2h (the deterministic red saki recorded
        # on run b7ffc64e). The endpoint path is synchronous ORM — there is
        # no async recompute to flush; pinning the exact seeded value with
        # tolerance is the deterministic assertion.
        newest_substantive = now - timedelta(hours=2)
        self.assertIsNotNone(item['last_comment_at'])
        returned = parse_datetime(item['last_comment_at'])
        self.assertIsNotNone(
            returned,
            f"last_comment_at {item['last_comment_at']} not ISO-parseable",
        )
        self.assertLess(
            abs(returned - newest_substantive), timedelta(seconds=5),
            f"last_comment_at {item['last_comment_at']} reflects system "
            f"chatter (expected ~{newest_substantive.isoformat()})",
        )

    def test_pls1294_system_only_task_shows_zero_attention(self):
        task = self._item('system-only-task')
        self._system_comment(task, 'lifecycle entry 1')
        self._system_comment(task, 'lifecycle entry 2')

        item = self._by_title()['system-only-task']
        self.assertEqual(item['comment_count'], 0)
        self.assertIsNone(item['last_comment_at'])

    def test_pls1294_legacy_untyped_counts_as_substantive(self):
        # PLS-518 classing: LEGACY_UNTYPED is substantive (is_system=False).
        now = timezone.now()
        task = self._item('legacy-task')
        legacy = Comment.objects.create(
            item=task, author_id=self.user.id,
            author_email=self.user.email, content='pre-PLS-518 comment',
            origin_kind=Comment.ORIGIN_LEGACY_UNTYPED,
        )
        Comment.objects.filter(pk=legacy.pk).update(created_at=now)
        self._system_comment(task, 'lifecycle entry')

        item = self._by_title()['legacy-task']
        self.assertEqual(item['comment_count'], 1)

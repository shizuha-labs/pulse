"""
PLS-73: future-due recurring tasks must NOT rank first in the queue-ordered
my_tasks (pulse_get_my_tasks) result.

Root cause: the queue policy's _queue_wait_rank bucketed only
WAITING_QUEUE_STATUS_SLUGS as "waiting". A recurring task reset for a future
period keeps status 'open' (a ready status) with a future due_date, so it got
wait_rank 0 (ready) and — being high priority — sorted to #1, causing agents to
re-run the just-closed recurring task every heartbeat.

Fix: a recurring task with due_date in the future is bucketed as waiting, so it
ranks BELOW genuinely-ready work. Overdue / due-now recurring tasks stay ready.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item

User = get_user_model()

MY_TASKS_URL = '/shizuha-pulse/api/items/my_tasks/?queue_order=true'


class FutureDueRecurringQueueTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        # Superuser so get_queryset returns all items regardless of project ACL;
        # the test is about ORDER, not visibility.
        self.user = User.objects.create_user(
            username='ryo', email='ryo@shizuha.com', password='x',
            is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(user=self.user)

    def _item(self, title, **kw):
        kw.setdefault('workflow_name', 'software-development')
        return Item.objects.create(
            title=title, mode='task',
            assignee_id=self.user.id, assignee_email=self.user.email,
            **kw,
        )

    def test_future_due_recurring_ranks_below_ready_work(self):
        now = timezone.now()
        # The bug repro: high-priority recurring task reset to a FUTURE due date.
        self._item(
            'recurring-future', status='open', priority='high',
            is_recurring=True, frequency='daily', due_date=now + timedelta(days=21),
        )
        # A genuinely-ready normal task.
        self._item('normal-ready', status='open', priority='normal')

        resp = self.client.get(MY_TASKS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['items']]
        self.assertIn('normal-ready', titles)
        self.assertIn('recurring-future', titles)
        # Ready work must come before the future-due recurring task, despite the
        # recurring one having higher priority.
        self.assertLess(
            titles.index('normal-ready'), titles.index('recurring-future'),
            f"future-due recurring task ranked too high: {titles}",
        )

    def test_due_now_recurring_stays_ready(self):
        now = timezone.now()
        # An overdue/due-now recurring task is genuinely ready — must NOT be
        # demoted. Higher priority than the normal task, so it should rank first.
        self._item(
            'recurring-due', status='open', priority='high',
            is_recurring=True, frequency='daily', due_date=now - timedelta(hours=1),
        )
        self._item('normal-ready', status='open', priority='normal')

        resp = self.client.get(MY_TASKS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['items']]
        self.assertLess(
            titles.index('recurring-due'), titles.index('normal-ready'),
            f"due-now recurring task should stay ready/first: {titles}",
        )

    def test_in_progress_recurring_future_due_not_demoted(self):
        """PLS-87: the wait-rank demotion is status='open'-only. An actively-worked
        (in_progress/in_review) recurring task with a future due_date must keep its
        real priority rank — not be bucketed as waiting below ready work."""
        now = timezone.now()
        # The PLS-87 repro: urgent in_progress recurring task due in 2 days
        # (EVOL-8-shaped). It must rank ABOVE a lower-priority ready task.
        self._item(
            'recurring-inprogress', status='in_progress', priority='urgent',
            is_recurring=True, frequency='daily', due_date=now + timedelta(days=2),
        )
        self._item('normal-ready', status='open', priority='normal')

        resp = self.client.get(MY_TASKS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['items']]
        self.assertIn('recurring-inprogress', titles)
        self.assertLess(
            titles.index('recurring-inprogress'), titles.index('normal-ready'),
            f"in_progress recurring task was wrongly demoted by the wait-rank: {titles}",
        )

    def test_open_recurring_future_due_still_demoted(self):
        """PLS-87 guard must NOT regress PLS-73: an OPEN future-due recurring task
        is still demoted below ready work."""
        now = timezone.now()
        self._item(
            'recurring-open-future', status='open', priority='high',
            is_recurring=True, frequency='daily', due_date=now + timedelta(days=21),
        )
        self._item('normal-ready', status='open', priority='normal')

        resp = self.client.get(MY_TASKS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        titles = [it['title'] for it in resp.data['items']]
        self.assertLess(
            titles.index('normal-ready'), titles.index('recurring-open-future'),
            f"open future-due recurring task should still be demoted (PLS-73): {titles}",
        )

"""
Regression tests for PLS-60: sprint burndown/velocity with rolling sprints.

Covers:
- committed_points counts ALL sprint tasks (not only pending/in_progress)
- get_burndown_data() reflects tasks that entered 'done' without completed_at stamp
- get_velocity() returns correct count when story_points=0
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, Sprint


def _make_project():
    return Project.objects.create(
        name='Test Proj',
        key='TST',
        org_id=9999,
    )


def _make_sprint(project, *, start_date=None, end_date=None):
    today = date.today()
    return Sprint.objects.create(
        project=project,
        name='S1',
        status='planning',
        start_date=start_date or today - timedelta(days=6),
        end_date=end_date or today,
    )


def _add_task(sprint, *, status='open', story_points=3, completed_at=None, updated_at=None):
    item = Item.objects.create(
        mode='task',
        title='task',
        sprint=sprint,
        status=status,
        story_points=story_points,
        completed_at=completed_at,
    )
    if updated_at is not None:
        Item.objects.filter(pk=item.pk).update(updated_at=updated_at)
        item.refresh_from_db()
    return item


class SprintStartCommittedPointsTest(TestCase):
    """Sprint.start() must count all sprint tasks regardless of status."""

    def test_counts_awaiting_merge_tasks(self):
        proj = _make_project()
        sprint = _make_sprint(proj)
        _add_task(sprint, status='in_progress', story_points=3)
        _add_task(sprint, status='awaiting_merge', story_points=5)
        _add_task(sprint, status='done', story_points=3)

        sprint.start()

        # All three tasks → 11 points committed
        self.assertEqual(sprint.committed_points, 11)

    def test_counts_all_tasks_when_no_story_points(self):
        proj = _make_project()
        sprint = _make_sprint(proj)
        for st in ('open', 'awaiting_merge', 'done'):
            _add_task(sprint, status=st, story_points=None)

        sprint.start()

        # Count-based fallback: 3 tasks → 3 committed
        self.assertEqual(sprint.committed_points, 3)

    def test_does_not_undercount_rolling_sprint(self):
        """A rolling sprint with 1 pending + 4 done must commit 5, not 1."""
        proj = _make_project()
        sprint = _make_sprint(proj)
        _add_task(sprint, status='pending', story_points=3)
        for _ in range(4):
            _add_task(sprint, status='done', story_points=3)

        sprint.start()

        self.assertEqual(sprint.committed_points, 15)


class SprintBurndownNullCompletedAtTest(TestCase):
    """get_burndown_data() must count tasks that entered done without completed_at."""

    def test_done_task_without_completed_at_burns_down(self):
        proj = _make_project()
        today = date.today()
        sprint = _make_sprint(proj, start_date=today - timedelta(days=6), end_date=today)
        sprint.committed_points = 6
        sprint.status = 'active'
        sprint.started_at = timezone.now() - timedelta(days=6)
        sprint.save()

        # Task in 'done' with completed_at=None; updated_at set to 3 days ago
        three_days_ago = timezone.now() - timedelta(days=3)
        _add_task(sprint, status='done', story_points=3, completed_at=None,
                  updated_at=three_days_ago)
        _add_task(sprint, status='open', story_points=3)

        data = sprint.get_burndown_data()
        self.assertTrue(len(data) > 0, "burndown must not be empty")

        # On today's row, 3 points should be burned (the done task)
        today_row = next((d for d in data if d['date'] == today.isoformat()), None)
        self.assertIsNotNone(today_row)
        # 6 total - 3 burned = 3 remaining
        self.assertEqual(today_row['actual'], 3.0)

    def test_done_task_with_completed_at_also_burns_down(self):
        proj = _make_project()
        today = date.today()
        sprint = _make_sprint(proj, start_date=today - timedelta(days=4), end_date=today)
        sprint.committed_points = 6
        sprint.status = 'active'
        sprint.started_at = timezone.now() - timedelta(days=4)
        sprint.save()

        two_days_ago = timezone.now() - timedelta(days=2)
        _add_task(sprint, status='done', story_points=3, completed_at=two_days_ago)
        _add_task(sprint, status='open', story_points=3)

        data = sprint.get_burndown_data()
        today_row = next(d for d in data if d['date'] == today.isoformat())
        self.assertEqual(today_row['actual'], 3.0)

    def test_all_done_tasks_null_completed_at_burns_to_zero(self):
        """All tasks done with completed_at=None → remaining reaches 0."""
        proj = _make_project()
        today = date.today()
        sprint = _make_sprint(proj, start_date=today - timedelta(days=6), end_date=today)
        sprint.committed_points = 6
        sprint.status = 'active'
        sprint.started_at = timezone.now() - timedelta(days=6)
        sprint.save()

        yesterday = timezone.now() - timedelta(days=1)
        _add_task(sprint, status='done', story_points=3, completed_at=None,
                  updated_at=yesterday)
        _add_task(sprint, status='done', story_points=3, completed_at=None,
                  updated_at=yesterday)

        data = sprint.get_burndown_data()
        today_row = next(d for d in data if d['date'] == today.isoformat())
        self.assertEqual(today_row['actual'], 0.0)


class SprintVelocityTest(TestCase):
    """get_velocity() returns done count when story_points=0."""

    def test_velocity_with_done_status_and_no_story_points(self):
        proj = _make_project()
        sprint = _make_sprint(proj)
        sprint.status = 'active'
        sprint.started_at = timezone.now()
        sprint.save()

        _add_task(sprint, status='done', story_points=None)
        _add_task(sprint, status='done', story_points=None)
        _add_task(sprint, status='open', story_points=None)

        self.assertEqual(sprint.get_velocity(), 2)

    def test_velocity_with_done_status_and_story_points(self):
        proj = _make_project()
        sprint = _make_sprint(proj)
        sprint.status = 'active'
        sprint.started_at = timezone.now()
        sprint.save()

        _add_task(sprint, status='done', story_points=5)
        _add_task(sprint, status='done', story_points=3)
        _add_task(sprint, status='in_progress', story_points=3)

        self.assertEqual(sprint.get_velocity(), 8)

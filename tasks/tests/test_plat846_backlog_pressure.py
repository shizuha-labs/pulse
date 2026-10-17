"""PLAT-846: backlog-pressure detector must match readiness semantics."""
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from tasks.tasks import _org_health_backlog_item_ready_for_pressure


class OrgHealthBacklogPressureReadinessTests(SimpleTestCase):
    def _item(self, **overrides):
        data = {
            'id': 1,
            'mode': 'task',
            'status': 'backlog',
            'resolution': None,
            'is_archived': False,
            'due_date': None,
        }
        data.update(overrides)
        return SimpleNamespace(**data)

    def test_future_due_backlog_items_do_not_count_as_pressure(self):
        now = timezone.now()
        future = self._item(due_date=now + timezone.timedelta(days=7))
        ready = self._item(id=2, due_date=now - timezone.timedelta(hours=1))

        self.assertFalse(_org_health_backlog_item_ready_for_pressure(future, now))
        self.assertTrue(_org_health_backlog_item_ready_for_pressure(ready, now))

    def test_backlog_items_with_active_linked_blocker_do_not_count(self):
        now = timezone.now()
        blocked = self._item(id=10)
        normal = self._item(id=11)

        self.assertFalse(_org_health_backlog_item_ready_for_pressure(blocked, now, {10}))
        self.assertTrue(_org_health_backlog_item_ready_for_pressure(normal, now, {10}))

    def test_terminal_blocker_not_in_active_blocker_set_does_not_suppress_pressure(self):
        now = timezone.now()
        item = self._item(id=20)

        self.assertTrue(_org_health_backlog_item_ready_for_pressure(item, now, set()))

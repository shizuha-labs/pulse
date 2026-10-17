"""
Regression tests for PLS-98: tasks with a far-future due_date must not surface
as actionable in pulse_get_my_tasks / pulse_get_user_tasks.

Root cause: _filter_out_terminal only removes terminal *statuses*. A regular
'open' task with a due_date months out (e.g. KURA-8, due 2026-11-20) is not
terminal, so it surfaced as ready work on every heartbeat → triage churn. Prior
fixes (PLS-70/PLS-73) parked KURA-8 behind a blocker, which un-did itself when
the blocker resolved. This fix suppresses it in code, keyed on due_date.

PLS-838: _filter_far_future_due now returns the hidden ROWS, not a count, so the
annotation can state the real distance to the soonest one. Assertions below use
len(hidden) — the counts themselves are unchanged.

Rule (_filter_far_future_due): drop NOT-yet-started tasks whose due_date is more
than FAR_FUTURE_DUE_DAYS out; keep in-flight work and tasks with no due_date.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


def _iso(days_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).isoformat()


class FarFutureDueSuppressionTests(unittest.TestCase):
    def test_open_far_future_task_is_hidden(self):
        tasks = [{"id": 1, "item_key": "KURA-8", "status": "open", "due_date": _iso(150)}]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, [], "open task due 150d out must be suppressed")
        self.assertEqual(len(hidden), 1)

    def test_open_due_within_window_is_kept(self):
        tasks = [{"id": 2, "item_key": "AT-2", "status": "open", "due_date": _iso(10)}]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(len(kept), 1, "open task due 10d out is actionable")
        self.assertEqual(len(hidden), 0)

    def test_boundary_just_inside_30d_is_kept(self):
        tasks = [{"id": 3, "item_key": "AT-3", "status": "open", "due_date": _iso(29)}]
        kept, _ = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(len(kept), 1, "due 29d out (< 30d) stays actionable")

    def test_in_progress_far_future_is_kept(self):
        # Active WIP must NOT be hidden regardless of due date.
        tasks = [{"id": 4, "item_key": "AT-4", "status": "in_progress", "due_date": _iso(200)}]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(len(kept), 1, "in_progress task stays regardless of due date")
        self.assertEqual(len(hidden), 0)

    def test_no_due_date_is_kept(self):
        tasks = [{"id": 5, "item_key": "AT-5", "status": "open"}]
        kept, _ = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(len(kept), 1, "task with no due_date is actionable as before")

    def test_date_only_far_future_is_hidden(self):
        far = (datetime.now(timezone.utc) + timedelta(days=120)).date().isoformat()  # YYYY-MM-DD
        tasks = [{"id": 6, "item_key": "AT-6", "status": "needs_triage", "due_date": far}]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, [], "date-only far-future due must also be suppressed")
        self.assertEqual(len(hidden), 1)

    def test_trailing_z_timestamp_far_future_is_hidden(self):
        far = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tasks = [{"id": 7, "item_key": "AT-7", "status": "open", "due_date": far}]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, [], "Z-suffixed far-future timestamp must be suppressed")
        self.assertEqual(len(hidden), 1)

    def test_mixed_list_partitions_correctly(self):
        tasks = [
            {"id": 1, "item_key": "KURA-8", "status": "open", "due_date": _iso(150)},   # hide
            {"id": 2, "item_key": "AT-2", "status": "open", "due_date": _iso(5)},        # keep
            {"id": 3, "item_key": "AT-3", "status": "in_progress", "due_date": _iso(99)},# keep
            {"id": 4, "item_key": "AT-4", "status": "open"},                              # keep
        ]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(len(hidden), 1)
        self.assertEqual({k["item_key"] for k in kept}, {"AT-2", "AT-3", "AT-4"})


class FutureRecurringDueFilterTest(unittest.TestCase):
    def test_future_due_recurring_open_within_30_days_is_hidden(self):
        tasks = [{
            "id": 8,
            "item_key": "EVOL-22",
            "status": "open",
            "is_recurring": True,
            "due_date": _iso(2),
        }]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, [])
        self.assertEqual(len(hidden), 1)

    def test_future_due_recurring_blocked_is_hidden(self):
        tasks = [{
            "id": 9,
            "item_key": "KURA-4",
            "status": "blocked",
            "is_recurring": True,
            "due_date": _iso(2),
        }]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, [])
        self.assertEqual(len(hidden), 1)

    def test_future_due_recurring_in_progress_stays_visible(self):
        tasks = [{
            "id": 10,
            "item_key": "AT-10",
            "status": "in_progress",
            "is_recurring": True,
            "due_date": _iso(2),
        }]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, tasks)
        self.assertEqual(len(hidden), 0)

    def test_due_now_recurring_open_stays_visible(self):
        tasks = [{
            "id": 11,
            "item_key": "AT-11",
            "status": "open",
            "is_recurring": True,
            "due_date": _iso(-1),
        }]
        kept, hidden = pulse_server._filter_far_future_due(tasks)
        self.assertEqual(kept, tasks)
        self.assertEqual(len(hidden), 0)


if __name__ == "__main__":
    unittest.main()

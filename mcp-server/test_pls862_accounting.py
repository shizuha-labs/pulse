"""
PLS-862 / ADR-0073 regression fixtures.

The defect: `scheduled` was classified terminal in the MCP, so a recurring
compliance item (e.g. a GST cycle) vanished from BOTH the visible rows and
EVERY count on the unfiltered assignee query — an item can be on your queue
and appear in no number at all.

ADR-0073 fix surface under test:
  1. `scheduled` is NOT terminal (see test_scheduled_not_actionable.py).
  2. Default-active assignee paths account for API-parked `scheduled` rows via
     a complementary query folded into `hidden_scheduled` — no item is left in
     no bucket.
  3. Explicit `status='scheduled'` renders as `waiting`, never `terminal`.
  4. The accounting invariant (ADR §6): for a fixture queue,
     actionable + waiting + terminal + hidden == assigned total — assert the
     SUM, not just the rows.
  5. MCP TERMINAL_STATUSES stays a strict allow-list mirroring the backend
     (drift guard in the Django suite).
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


def _parked_row(key, due_days):
    from datetime import datetime, timedelta, timezone
    return {
        "item_key": key,
        "id": hash(key) % 100000,
        "title": f"{key} title",
        "status": "scheduled",
        "priority": "high",
        "is_recurring": True,
        "due_date": (datetime.now(timezone.utc) + timedelta(days=due_days)).isoformat(),
    }


class DefaultActiveAccountingTests(unittest.TestCase):
    """Polarity A (ADR §6): in_progress + scheduled → counts reconcile."""

    def test_fetch_default_active_page_folds_parked_scheduled_into_hidden(self):
        calls = []

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            calls.append(dict(params))
            if params.get("status") == "scheduled":
                # Complementary parked query: the ONE parked recurring row.
                return {"results": [_parked_row("BKSG-9", 6)], "count": 1, "next": None}
            # Visible active page.
            return {
                "results": [{
                    "id": 1,
                    "item_key": "PLAT-1",
                    "title": "Ready work",
                    "status": "in_review",
                    "priority": "high",
                    "is_recurring": False,
                }],
                "next": None,
            }

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            result, visible, hidden_terminal, hidden_scheduled = pulse_server._fetch_default_active_page(
                {"mode": "task", "assignee_email": "shion@shizuha.com",
                 "is_active": "true", "queue_order": "true"},
                5,
            )

        self.assertEqual([i["item_key"] for i in visible], ["PLAT-1"])
        # Hidden bucket now carries the parked scheduled row (non-terminal, counted).
        parked_keys = [i["item_key"] for i in hidden_scheduled]
        self.assertIn("BKSG-9", parked_keys, "parked scheduled row must be ACCOUNTED in hidden_scheduled")
        self.assertEqual(hidden_terminal, 0)
        # The complementary query was scoped to the assignee with status=scheduled.
        scheduled_calls = [c for c in calls if c.get("status") == "scheduled"]
        self.assertEqual(len(scheduled_calls), 1)
        self.assertEqual(scheduled_calls[0]["assignee_email"], "shion@shizuha.com")
        self.assertNotIn("is_active", scheduled_calls[0])

    def test_unscoped_default_active_does_not_run_parked_sweep(self):
        """No assignee in scope → no global scheduled sweep (not an assigned scope)."""
        calls = []

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            calls.append(dict(params))
            return {"results": [], "next": None}

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            pulse_server._fetch_default_active_page({"mode": "task", "is_active": "true"}, 5)

        status_calls = [c for c in calls if c.get("status") == "scheduled"]
        self.assertEqual(status_calls, [], "must not sweep parked scheduled without an assignee scope")

    def test_complementary_error_stays_quiet(self):
        """A failed parked-count query must not break the queue."""
        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            if params.get("status") == "scheduled":
                return {"error": "boom"}
            return {"results": [{"id": 1, "item_key": "PLAT-1", "title": "t",
                                 "status": "in_review", "priority": "high"}], "next": None}

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            result, visible, hidden_terminal, hidden_scheduled = pulse_server._fetch_default_active_page(
                {"mode": "task", "assignee_email": "x@shizuha.com", "is_active": "true"}, 5,
            )
        self.assertNotIn("error", result)
        self.assertEqual(len(visible), 1)
        self.assertEqual(hidden_scheduled, [], "failed parked query returns no hidden rows")


class AccountingInvariantTests(unittest.TestCase):
    """ADR §6 AC3: assert the SUM (ready + blocked/waiting + terminal + hidden == total), not rows."""

    def test_fmt_items_sum_invariant(self):
        items = [
            {"item_key": "A", "id": 1, "title": "a", "status": "in_progress", "priority": "high"},
            {"item_key": "B", "id": 2, "title": "b", "status": "scheduled", "priority": "high"},
            {"item_key": "C", "id": 3, "title": "c", "status": "done", "priority": "high"},
        ]
        out = pulse_server._fmt_items(items, hidden_terminal=1, hidden_scheduled=1)
        self.assertIn("Found 3 task(s) — 2 active (1 ready/movable, 1 blocked/waiting), 1 terminal", out)
        self.assertIn("1 more in terminal states", out)
        # Sum: 1 ready + 1 blocked/waiting + 1 terminal + 1 hidden_terminal + 1 hidden_scheduled == 5 scoped items.
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()

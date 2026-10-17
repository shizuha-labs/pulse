"""
Regression tests for PLS-405: small MCP limits must apply after local
visibility filters.

The Pulse API excludes terminal/scheduled statuses for is_active=true, but the
MCP server still filters future recurring/far-future due rows locally. A
`page_size=limit` API call let one locally-hidden row consume `limit=1`, causing
`pulse_get_my_tasks(limit=1)` to report no actionable work even though the next
API row was ready.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


def _iso(days_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).isoformat()


class LimitAfterVisibilityTests(unittest.TestCase):
    def test_fetch_default_active_page_skips_hidden_first_row(self):
        calls = []

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            calls.append(dict(params or {}))
            # PLS-862: the default-active path now runs a complementary
            # status=scheduled accounting query — this fixture has no parked
            # rows, so return none.
            if params.get("status") == "scheduled":
                return {"results": [], "count": 0, "next": None}
            if params.get("page") == 1:
                return {
                    "results": [{
                        "id": 1,
                        "item_key": "PLS-HIDDEN",
                        "title": "Future recurring",
                        "status": "open",
                        "priority": "urgent",
                        "is_recurring": True,
                        "due_date": _iso(5),
                    }],
                    "next": "page-2",
                }
            return {
                "results": [{
                    "id": 2,
                    "item_key": "PLS-READY",
                    "title": "Ready queue item",
                    "status": "in_review",
                    "priority": "high",
                    "is_recurring": False,
                    "due_date": None,
                }],
                "next": None,
            }

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            result, visible, hidden_terminal, hidden_scheduled = pulse_server._fetch_default_active_page(
                {"mode": "task", "assignee_email": "zen@shizuha.com", "is_active": "true", "queue_order": "true"},
                1,
            )

        self.assertNotIn("error", result)
        self.assertEqual([item["item_key"] for item in visible], ["PLS-READY"])
        self.assertEqual(hidden_terminal, 0)
        self.assertEqual(len(hidden_scheduled), 1)  # PLS-838: rows, not a count
        # PLS-862: the complementary status=scheduled accounting call has no
        # page; the main page walk still advances 1 → 2.
        self.assertEqual([call["page"] for call in calls if call.get("page")], [1, 2])
        self.assertGreaterEqual(calls[0]["page_size"], 20)

    def test_get_my_tasks_limit_one_returns_ready_item_after_hidden_row(self):
        calls = []

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            calls.append(dict(params or {}))
            # Backlog lane lookup should be empty; it is not the behavior under test.
            if params.get("status") == "backlog":
                return {"results": [], "count": 0, "next": None}
            # PLS-862: complementary parked-scheduled accounting query — no
            # parked rows in this fixture so return none.
            if params.get("status") == "scheduled":
                return {"results": [], "count": 0, "next": None}
            if params.get("page") == 1:
                return {
                    "results": [{
                        "id": 1,
                        "item_key": "PLS-HIDDEN",
                        "title": "Future recurring",
                        "status": "open",
                        "priority": "urgent",
                        "is_recurring": True,
                        "due_date": _iso(3),
                    }],
                    "next": "page-2",
                }
            return {
                "results": [{
                    "id": 2,
                    "item_key": "PLS-43",
                    "title": "Pulse MCP reliability check",
                    "status": "in_review",
                    "priority": "normal",
                    "is_recurring": False,
                }],
                "next": None,
            }

        with patch.object(pulse_server, "_get_my_user_id", return_value=(123, "zen@shizuha.com")), \
             patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_my_tasks(limit=1)

        self.assertIn("PLS-43", out)
        self.assertNotIn("No ready/movable tasks found", out)
        self.assertNotIn("No actionable tasks found", out)
        self.assertIn("+1 not yet due", out)
        active_calls = [call for call in calls if call.get("is_active") == "true"]
        self.assertTrue(active_calls)
        self.assertTrue(all(
            call.get("permission_hint") == "false" for call in active_calls
        ))


if __name__ == "__main__":
    unittest.main()

"""Default pulse_get_my_tasks limit is a short queue-ordered head (not the full queue)."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


def _task(i: int, priority: str = "normal") -> dict:
    return {
        "id": i,
        "item_key": f"PLAT-{i}",
        "title": f"Task {i}",
        "status": "in_review",
        "priority": priority,
        "is_recurring": False,
        "due_date": None,
    }


class MyTasksDefaultLimitTests(unittest.TestCase):
    def test_default_limit_is_five(self):
        self.assertEqual(pulse_server.MY_TASKS_DEFAULT_LIMIT, 5)

    def test_clamp_limit(self):
        self.assertEqual(pulse_server._clamp_my_tasks_limit(0), 1)
        self.assertEqual(pulse_server._clamp_my_tasks_limit(5), 5)
        self.assertEqual(pulse_server._clamp_my_tasks_limit(9999), pulse_server.MY_TASKS_MAX_LIMIT)
        self.assertEqual(pulse_server._clamp_my_tasks_limit("nope"), 5)

    def test_get_my_tasks_default_returns_top_five_of_total(self):
        results = [_task(i) for i in range(1, 11)]

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            # PLS-862: default-active path runs a complementary status=scheduled
            # accounting query — this fixture has no parked rows.
            if params.get("status") == "scheduled":
                return {"results": [], "count": 0, "next": None}
            if params.get("status") == "backlog":
                return {"results": [], "count": 0, "next": None}
            page_size = int(params.get("page_size") or 20)
            page = int(params.get("page") or 1)
            start = (page - 1) * page_size
            chunk = results[start : start + page_size]
            return {
                "results": chunk,
                "count": len(results),
                "next": "next" if start + page_size < len(results) else None,
            }

        with patch.object(pulse_server, "_get_my_user_id", return_value=(1, "rei@shizuha.com")), \
             patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_my_tasks()

        self.assertIn("showing top 5 of 10 active", out)
        self.assertIn("limit=5", out)
        self.assertIn("5 more remain", out)
        for i in range(1, 6):
            self.assertIn(f"PLAT-{i}", out)
        for i in range(6, 11):
            self.assertNotIn(f"PLAT-{i}", out)

    def test_get_my_tasks_explicit_limit_override(self):
        results = [_task(i) for i in range(1, 9)]

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            if params.get("status") == "scheduled":
                return {"results": [], "count": 0, "next": None}
            if params.get("status") == "backlog":
                return {"results": [], "count": 0, "next": None}
            page_size = int(params.get("page_size") or 20)
            page = int(params.get("page") or 1)
            start = (page - 1) * page_size
            chunk = results[start : start + page_size]
            return {
                "results": chunk,
                "count": len(results),
                "next": "next" if start + page_size < len(results) else None,
            }

        with patch.object(pulse_server, "_get_my_user_id", return_value=(1, "rei@shizuha.com")), \
             patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_my_tasks(limit=8)

        self.assertIn("showing 8 of 8 active", out)
        self.assertIn("limit=8", out)
        self.assertNotIn("more remain", out)
        for i in range(1, 9):
            self.assertIn(f"PLAT-{i}", out)


if __name__ == "__main__":
    unittest.main()

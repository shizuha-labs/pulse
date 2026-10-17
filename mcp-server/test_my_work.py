"""pulse_get_my_work returns alerts and tasks in one snapshot."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


class MyWorkTests(unittest.TestCase):
    def test_combines_alerts_and_tasks_without_picking_an_item(self):
        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            if params.get("mode") == "alert":
                return {
                    "results": [{
                        "id": 41,
                        "item_key": "PLAT-41",
                        "mode": "alert",
                        "title": "Origin CI failed",
                        "status": "firing",
                        "priority": "high",
                    }],
                    "count": 1,
                }
            if params.get("status") in ("scheduled", "backlog"):
                return {"results": [], "count": 0, "next": None}
            return {
                "results": [{
                    "id": 7824,
                    "item_key": "PLAT-7824",
                    "title": "tls-sync VAP",
                    "status": "awaiting_deploy",
                    "priority": "urgent",
                    "is_recurring": False,
                    "due_date": None,
                }],
                "count": 1,
                "next": None,
            }

        with patch.object(
            pulse_server, "_get_my_user_id", return_value=(7, "aoi@shizuha.com"),
        ), patch.object(
            pulse_server, "api_request", side_effect=fake_api_request,
        ):
            out = pulse_server.get_my_work()

        self.assertIn("## Alerts", out)
        self.assertIn("## Tasks", out)
        self.assertIn("Origin CI failed", out)
        self.assertIn("PLAT-7824", out)
        self.assertIn("You choose what to advance", out)
        self.assertNotIn("Work the highest-priority ready/movable item", out)
        self.assertNotIn("FIRST", out)

    def test_empty_alerts_still_show_tasks(self):
        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            params = params or {}
            if params.get("mode") == "alert":
                return {"results": [], "count": 0}
            if params.get("status") in ("scheduled", "backlog"):
                return {"results": [], "count": 0, "next": None}
            return {
                "results": [{
                    "id": 1,
                    "item_key": "PLAT-1",
                    "title": "Ready work",
                    "status": "todo",
                    "priority": "urgent",
                    "is_recurring": False,
                    "due_date": None,
                }],
                "count": 1,
                "next": None,
            }

        with patch.object(
            pulse_server, "_get_my_user_id", return_value=(7, "aoi@shizuha.com"),
        ), patch.object(
            pulse_server, "api_request", side_effect=fake_api_request,
        ):
            out = pulse_server.get_my_work()

        self.assertIn("No active alerts assigned to aoi@shizuha.com", out)
        self.assertIn("PLAT-1", out)
        self.assertIn("Empty alerts does not mean an empty queue", out)


if __name__ == "__main__":
    unittest.main()

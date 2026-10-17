"""Heartbeat alert reconciliation must be independent of the task queue."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class MyAlertsTests(unittest.TestCase):
    def test_queries_only_active_alerts_for_current_agent(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen.update({"method": method, "endpoint": endpoint, "params": params})
            return {
                "results": [{
                    "id": 41,
                    "item_key": "PLAT-41",
                    "mode": "alert",
                    "title": "Origin CI failed",
                    "status": "firing",
                    "severity": "error",
                    "priority": "high",
                }],
            }

        with patch.object(
            pulse_server,
            "_get_my_user_id",
            return_value=(7, "nagi@shizuha.com"),
        ), patch.object(
            pulse_server,
            "api_request",
            side_effect=fake_api_request,
        ):
            output = pulse_server.get_my_alerts(limit=3)

        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["endpoint"], "/items/")
        self.assertEqual(seen["params"]["mode"], "alert")
        self.assertEqual(seen["params"]["assignee_email"], "nagi@shizuha.com")
        self.assertEqual(seen["params"]["status"], "firing,acknowledged")
        self.assertEqual(seen["params"]["page_size"], 3)
        self.assertNotIn("ordering", seen["params"])
        self.assertIn("Origin CI failed", output)

    def test_empty_alert_inbox_is_explicit(self):
        with patch.object(
            pulse_server,
            "_get_my_user_id",
            return_value=(7, "nagi@shizuha.com"),
        ), patch.object(
            pulse_server,
            "api_request",
            return_value={"results": []},
        ):
            output = pulse_server.get_my_alerts()

        self.assertEqual(
            output,
            "No active alerts assigned to nagi@shizuha.com.",
        )


if __name__ == "__main__":
    unittest.main()

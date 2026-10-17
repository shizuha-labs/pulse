import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class Gx104ThermalSuppressionTests(unittest.TestCase):
    def test_create_task_refuses_gx10_4_thermal_followup(self):
        result = pulse_server.create_task(
            title="Inspect gx10-4 NIC cooling - thermal spike to 94C (GB10NicTempWarm)",
            description="Prometheus node_hwmon_temp_celsius for 10.200.10.4:9100 is hot.",
            project_id=331,
            workflow="autonomous-dev",
            assignment_group="human-ops",
        )

        self.assertIn("PLAT-666", result)
        self.assertIn("operator-suppressed", result)

    def test_create_task_allows_non_thermal_gx10_4_work(self):
        with mock.patch.object(pulse_server, "_find_existing_task_candidates", return_value=[]), \
             mock.patch.object(pulse_server, "api_request", return_value={
                 "id": 123,
                 "item_key": "PLAT-2000",
                 "title": "Repair gx10-4 kubelet route",
                 "status": "open",
             }):
            result = pulse_server.create_task(
                title="Repair gx10-4 kubelet route",
                description="Non-thermal network reachability failure.",
                project_id=331,
                workflow="autonomous-dev",
                assignment_group="devops",
            )

        self.assertIn("PLAT-2000", result)


class ExpandedTaskSearchTests(unittest.TestCase):
    def test_search_expands_alert_phrase_to_node_specific_probe(self):
        calls = []

        def fake_api(method, path, params=None, **kwargs):
            calls.append(params.get("search"))
            if params.get("search") == "gx10-4 NIC cooling":
                return {
                    "results": [{
                        "id": 666,
                        "item_key": "PLAT-666",
                        "title": "Inspect gx10-4 NIC cooling fault: ConnectX sensors 92 C",
                        "status": "deferred",
                        "priority": "high",
                    }]
                }
            return {"results": []}

        with mock.patch.object(pulse_server, "api_request", side_effect=fake_api):
            result = pulse_server.search_tasks(
                "gx10-4 NIC cooling thermal GB10NicTempWarm",
                limit=20,
            )

        self.assertIn("PLAT-666", result)
        self.assertIn("gx10-4 NIC cooling", calls)


if __name__ == "__main__":
    unittest.main()

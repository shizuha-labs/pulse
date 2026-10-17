"""
Regression tests for PLS-265: pulse_create_task(parent_item_id=...) should
accept the same human-readable task keys that read/path tools accept, not only
numeric primary keys.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class CreateTaskParentItemKeyTests(unittest.TestCase):
    @patch("pulse_server.api_request")
    def test_parent_item_id_accepts_task_key_and_posts_numeric_parent(self, mock_api):
        posted_payloads = []

        def _api_side_effect(method, path, **kwargs):
            if method == "GET" and path == "/items/HIVE-435/":
                return {"id": 435, "item_key": "HIVE-435"}
            if method == "POST" and path == "/items/":
                posted_payloads.append(kwargs["data"])
                return {
                    "id": 9001,
                    "item_key": "HIVE-436",
                    "title": kwargs["data"]["title"],
                    "workflow_name": "autonomous-dev",
                    "workflow_status": "Open",
                    "status": "open",
                }
            return {}

        mock_api.side_effect = _api_side_effect

        result = pulse_server.create_task(
            title="Child task under HIVE-435",
            description="Verify task-key parent resolution",
            project_id=331,
            workflow="autonomous-dev",
            parent_item_id="HIVE-435",
            allow_duplicate=True,
        )

        self.assertIn("Task created: [HIVE-436]", result)
        self.assertEqual(posted_payloads[0]["parent_item"], 435)

    @patch("pulse_server.api_request")
    def test_numeric_parent_item_id_still_works_when_lookup_fails(self, mock_api):
        posted_payloads = []

        def _api_side_effect(method, path, **kwargs):
            if method == "GET" and path == "/items/123/":
                return {"error": "not found", "status_code": 404}
            if method == "POST" and path == "/items/":
                posted_payloads.append(kwargs["data"])
                return {
                    "id": 9002,
                    "item_key": "PLAT-124",
                    "title": kwargs["data"]["title"],
                    "workflow_name": "autonomous-dev",
                    "workflow_status": "Open",
                    "status": "open",
                }
            return {}

        mock_api.side_effect = _api_side_effect

        result = pulse_server.create_task(
            title="Child task under numeric parent",
            project_id=331,
            workflow="autonomous-dev",
            parent_item_id="123",
            allow_duplicate=True,
        )

        self.assertIn("Task created: [PLAT-124]", result)
        self.assertEqual(posted_payloads[0]["parent_item"], 123)


if __name__ == "__main__":
    unittest.main()

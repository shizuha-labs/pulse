"""Regression coverage for pulse_create_task's documented tag-list contract."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class CreateTaskTagsTests(unittest.TestCase):
    @patch("pulse_server.api_request")
    def test_native_tag_list_is_normalized_for_text_field_api(self, mock_api):
        posted_payloads = []

        def _api_side_effect(method, path, **kwargs):
            if method == "POST" and path == "/items/":
                posted_payloads.append(kwargs["data"])
                return {
                    "id": 21435,
                    "item_key": "BKSD-27",
                    "title": kwargs["data"]["title"],
                    "workflow_name": "autonomous-dev",
                    "workflow_status": "To Do",
                    "status": "todo",
                }
            return {}

        mock_api.side_effect = _api_side_effect

        result = pulse_server.create_task(
            title="Normalize tag array",
            project_id=356,
            workflow="autonomous-dev",
            tags=["shizuha-digital", " fy2025-26 ", "", "wiki"],
            allow_duplicate=True,
        )

        self.assertIn("Task created: [BKSD-27]", result)
        self.assertEqual(
            posted_payloads[0]["tags"],
            "shizuha-digital,fy2025-26,wiki",
        )

    @patch("pulse_server.api_request")
    def test_comma_separated_tag_string_remains_supported(self, mock_api):
        posted_payloads = []

        def _api_side_effect(method, path, **kwargs):
            if method == "POST" and path == "/items/":
                posted_payloads.append(kwargs["data"])
                return {
                    "id": 21435,
                    "item_key": "BKSD-27",
                    "title": kwargs["data"]["title"],
                    "workflow_name": "autonomous-dev",
                    "workflow_status": "To Do",
                    "status": "todo",
                }
            return {}

        mock_api.side_effect = _api_side_effect

        pulse_server.create_task(
            title="Preserve tag string",
            project_id=356,
            workflow="autonomous-dev",
            tags="shizuha-digital,fy2025-26,wiki",
            allow_duplicate=True,
        )

        self.assertEqual(
            posted_payloads[0]["tags"],
            "shizuha-digital,fy2025-26,wiki",
        )


if __name__ == "__main__":
    unittest.main()

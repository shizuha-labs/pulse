"""PLS-900 / ADR-0052 conflict authority — MCP create/update surface tests.

The MCP create_task/update_task tools must expose the conflict-exclusion input
and normalize it (list or comma-separated string) into the task's
`conflicted_parties` field on the wire, so the no-schema-gap acceptance holds
from any seat via the public API.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402

_ITEM_OK = {
    "id": 21435,
    "item_key": "PLAT-9999",
    "title": "t",
    "workflow_name": "autonomous-dev",
    "workflow_status": "To Do",
    "status": "todo",
}


def _create_side_effect(posted_payloads):
    def _api(method, path, **kwargs):
        if method == "POST" and path == "/items/":
            posted_payloads.append(kwargs["data"])
            return {
                "id": 21435,
                "item_key": "PLAT-9999",
                "title": kwargs["data"]["title"],
                "workflow_name": "autonomous-dev",
                "workflow_status": "To Do",
                "status": "todo",
            }
        return {}

    return _api


class CreateTaskConflictedPartiesTests(unittest.TestCase):
    @patch("pulse_server.api_request")
    def test_native_list_is_normalized_to_lowercase(self, mock_api):
        posted = []
        mock_api.side_effect = _create_side_effect(posted)
        pulse_server.create_task(
            title="conflict list",
            project_id=356,
            workflow="autonomous-bug",
            conflicted_parties=["Nova@shizuha.com", "  kai@shizuha.com ", "", None],
            allow_duplicate=True,
        )
        self.assertEqual(
            posted[0]["conflicted_parties"],
            ["nova@shizuha.com", "kai@shizuha.com"],
        )

    @patch("pulse_server.api_request")
    def test_comma_separated_string_is_normalized(self, mock_api):
        posted = []
        mock_api.side_effect = _create_side_effect(posted)
        pulse_server.create_task(
            title="conflict string",
            project_id=356,
            workflow="autonomous-bug",
            conflicted_parties="nova@shizuha.com, kai@shizuha.com,",
            allow_duplicate=True,
        )
        self.assertEqual(
            posted[0]["conflicted_parties"],
            ["nova@shizuha.com", "kai@shizuha.com"],
        )


class UpdateTaskConflictedPartiesTests(unittest.TestCase):
    @patch("pulse_server.api_request")
    def test_update_task_passes_and_clears_field(self, mock_api):
        posted = []
        backup = []
        current = {"id": 5, "item_key": "PLAT-1", "title": "t", "status": "todo"}

        def _api(method, path, **kwargs):
            if method == "GET" and path == "/items/5/":
                return current
            if method == "PATCH" and path == "/items/5/":
                posted.append(kwargs["data"])
                return current
            return {}

        backup.append(posted)
        mock_api.side_effect = _api
        # set
        pulse_server.update_task(
            task_id="5",
            conflicted_parties=["nova@shizuha.com", "KAI@SHIZUHA.COM "],
        )
        self.assertEqual(
            posted[0]["conflicted_parties"],
            ["nova@shizuha.com", "kai@shizuha.com"],
        )
        # clear with empty list
        posted.clear()
        pulse_server.update_task(task_id="5", conflicted_parties=[])
        self.assertEqual(posted[0]["conflicted_parties"], [])


if __name__ == "__main__":
    unittest.main()

"""
PLS-518 contract tests: pinned "current state" handoff summary + comment
classing on the MCP surface.

Product finding (PLAT-528 dogfood): task truth accumulates as an append-only
comment thread; every failover/rebalance seat must reconstruct "where are we
now" by paging the head of a long thread. PLS-518 adds:
1. a pinned current_state field, shown ABOVE comments in pulse_get_task output;
2. comment classing — system/lifecycle comments (failover, rebalance,
   moved-to-backlog, auto-block/unblock, routability holds) are segregated from
   substantive content in pulse_list_comments (hidden by default, tagged
   [system] when shown);
3. a staleness nudge when substantive comments are newer than the pin.

The backend owns the single-writer (assignee-or-staff) gate + timestamp; these
tests pin the MCP rendering/contract.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


def _task(**overrides):
    base = {
        "id": 42,
        "item_key": "PLS-518",
        "title": "Pinned current state",
        "status": "in_progress",
        "priority": "normal",
        "workflow_name": "autonomous-dev",
        "workflow_status": "In Progress",
        "assignment_group": "engineering",
        "assignee_email": "nagi@shizuha.com",
        "description": "Long description",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
        "current_state": "",
        "current_state_updated_at": None,
        "labels": [],
        "tags": "",
    }
    base.update(overrides)
    return base


class GetTaskCurrentStateTests(unittest.TestCase):
    def test_get_task_shows_pinned_current_state_above_description(self):
        t = _task(
            current_state="Implementing the pinned summary; next = serializer fields.",
            current_state_updated_at="2026-08-19T09:00:00Z",
        )

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            if endpoint == "/items/PLS-518/":
                return t
            if endpoint == "/comments/":
                return {"results": [], "count": 0, "next": None}
            raise AssertionError(f"unexpected {method} {endpoint}")

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_task("PLS-518")

        self.assertIn("## Current state", out)
        self.assertIn("Implementing the pinned summary; next = serializer fields.", out)
        self.assertIn("_Updated: 2026-08-19T09:00:00Z_", out)
        # The pin is shown ABOVE the description (handoff-first reading).
        self.assertLess(out.index("## Current state"), out.index("## Description"))

    def test_get_task_no_pin_renders_no_section(self):
        t = _task()

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            if endpoint == "/items/PLS-518/":
                return t
            if endpoint == "/comments/":
                return {"results": [], "count": 0, "next": None}
            raise AssertionError(f"unexpected {method} {endpoint}")

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_task("PLS-518")

        self.assertNotIn("## Current state", out)
        self.assertNotIn("may be stale", out)

    def test_staleness_nudge_when_substantive_comment_newer_than_pin(self):
        t = _task(
            current_state="Pinned yesterday.",
            current_state_updated_at="2026-08-18T09:00:00Z",
        )

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            if endpoint == "/items/PLS-518/":
                return t
            if endpoint == "/comments/":
                return {
                    "results": [
                        {
                            "id": 1,
                            "author_email": "sara@shizuha.com",
                            "content": "Reviewer feedback — act on this.",
                            "created_at": "2026-08-19T10:00:00Z",
                            "is_system": False,
                        },
                        {
                            "id": 2,
                            "author_email": "system@pulse",
                            "content": "Auto-failover to nagi.",
                            "created_at": "2026-08-19T11:00:00Z",
                            "is_system": True,
                        },
                    ],
                    "count": 2,
                    "next": None,
                }
            raise AssertionError(f"unexpected {method} {endpoint}")

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_task("PLS-518")

        # The system comment (newer) does NOT trigger the nudge; the substantive
        # reviewer comment (newer than the pin) DOES.
        self.assertIn("may be stale", out)
        self.assertIn("1 substantive comment(s) landed after the pin", out)

    def test_no_nudge_when_only_system_comments_newer(self):
        t = _task(
            current_state="Pinned yesterday.",
            current_state_updated_at="2026-08-18T09:00:00Z",
        )

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            if endpoint == "/items/PLS-518/":
                return t
            if endpoint == "/comments/":
                return {
                    "results": [
                        {
                            "id": 2,
                            "author_email": "system@pulse",
                            "content": "Auto-failover to nagi.",
                            "created_at": "2026-08-19T11:00:00Z",
                            "is_system": True,
                        },
                    ],
                    "count": 1,
                    "next": None,
                }
            raise AssertionError(f"unexpected {method} {endpoint}")

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_task("PLS-518")

        self.assertNotIn("may be stale", out)


class ListCommentsSegregationTests(unittest.TestCase):
    COMMENTS = [
        {
            "id": 1,
            "author_email": "sara@shizuha.com",
            "content": "Substantive reviewer feedback.",
            "created_at": "2026-08-19T10:00:00Z",
            "is_system": False,
        },
        {
            "id": 2,
            "author_email": "system@pulse",
            "content": "Auto-failover to nagi.",
            "created_at": "2026-08-19T11:00:00Z",
            "is_system": True,
        },
        {
            "id": 3,
            "author_email": "system@pulse",
            "content": "Moved to Backlog.",
            "created_at": "2026-08-19T12:00:00Z",
            "is_system": True,
        },
    ]

    def _fake(self, method, endpoint, params=None, data=None, as_user=False):
        if endpoint == "/comments/":
            return {"results": list(self.COMMENTS), "count": len(self.COMMENTS), "next": None}
        if endpoint == "/items/PLS-518/":
            return {"id": 42, "item_key": "PLS-518", "title": "Pinned current state"}
        raise AssertionError(f"unexpected {method} {endpoint}")

    def test_system_comments_hidden_by_default_with_count_note(self):
        with patch.object(pulse_server, "api_request", side_effect=self._fake):
            out = pulse_server.list_comments("PLS-518")

        self.assertIn("2 system/lifecycle comment(s) hidden", out)
        self.assertIn("Substantive reviewer feedback.", out)
        self.assertNotIn("Auto-failover to nagi.", out)
        self.assertNotIn("Moved to Backlog.", out)

    def test_include_system_shows_and_tags_system_comments(self):
        with patch.object(pulse_server, "api_request", side_effect=self._fake):
            out = pulse_server.list_comments("PLS-518", include_system=True)

        self.assertIn("includes 2 system/lifecycle comment(s)", out)
        self.assertIn("**[system]**", out)
        self.assertIn("Auto-failover to nagi.", out)
        self.assertIn("Moved to Backlog.", out)
        self.assertIn("Substantive reviewer feedback.", out)

    def test_all_substantive_no_hidden_note(self):
        def fake(method, endpoint, params=None, data=None, as_user=False):
            if endpoint == "/comments/":
                return {
                    "results": [self.COMMENTS[0]],
                    "count": 1,
                    "next": None,
                }
            if endpoint == "/items/PLS-518/":
                return {"id": 42, "item_key": "PLS-518", "title": "Pinned current state"}
            raise AssertionError(f"unexpected {method} {endpoint}")

        with patch.object(pulse_server, "api_request", side_effect=fake):
            out = pulse_server.list_comments("PLS-518")

        self.assertNotIn("system/lifecycle comment(s) hidden", out)
        self.assertIn("Substantive reviewer feedback.", out)


class UpdateTaskCurrentStateTests(unittest.TestCase):
    def test_update_task_passes_current_state_to_patch(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen["method"] = method
            seen["endpoint"] = endpoint
            seen["data"] = data
            return {
                "item_key": "PLS-518",
                "title": "Pinned current state",
                "status": "in_progress",
            }

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.update_task("PLS-518", current_state="Next = write tests.")

        self.assertIn("Updated: [PLS-518]", out)
        self.assertEqual(seen["method"], "PATCH")
        self.assertEqual(seen["data"].get("current_state"), "Next = write tests.")

    def test_update_task_clears_current_state_with_empty_string(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen["data"] = data
            return {"item_key": "PLS-518", "title": "Pinned current state", "status": "in_progress"}

        with patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            pulse_server.update_task("PLS-518", current_state="")

        self.assertIn("current_state", seen["data"])
        self.assertEqual(seen["data"]["current_state"], "")


if __name__ == "__main__":
    unittest.main()

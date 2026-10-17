"""
Regression tests for PLAT-2691: pulse_mark_notification_read returned a bare 404
when given a task key, and pulse_list_notifications never surfaced the
notification UUID the tool needs.

The Pulse API marks a notification read by its UUID (/notifications/<uuid>/read/).
These tests pin: UUID passthrough, task-key -> UUID resolution, a clear (not raw
404) error for an unknown UUID, and that the listing exposes the UUID + task key.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402

_UUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


class MarkNotificationReadTests(unittest.TestCase):
    def test_uuid_is_posted_to_read_endpoint(self):
        with mock.patch.object(pulse_server, "api_request", return_value={"success": True}) as api:
            result = pulse_server.mark_notification_read(_UUID)
        api.assert_called_once_with("POST", f"/notifications/{_UUID}/read/")
        self.assertIn("marked as read", result)

    def test_task_key_resolves_to_notification_uuid(self):
        listing = {"results": [
            {"id": _UUID, "item_key": "PLS-237", "is_read": False, "message": "m"},
            {"id": "other-uuid", "item_key": "PLAT-9", "is_read": False, "message": "n"},
        ]}
        with mock.patch.object(pulse_server, "api_request",
                               side_effect=[listing, {"success": True}]) as api:
            result = pulse_server.mark_notification_read("PLS-237")
        # First call lists unread notifications; second POSTs read for the match only.
        self.assertEqual(api.call_args_list[0][0][0], "GET")
        self.assertEqual(api.call_args_list[0][0][1], "/notifications/")
        self.assertEqual(api.call_args_list[1][0], ("POST", f"/notifications/{_UUID}/read/"))
        self.assertEqual(len(api.call_args_list), 2)  # PLAT-9 not touched
        self.assertIn("Marked 1 notification(s) for task PLS-237", result)

    def test_task_key_with_no_unread_match_returns_clear_error(self):
        with mock.patch.object(pulse_server, "api_request",
                               return_value={"results": []}) as api:
            result = pulse_server.mark_notification_read("PLS-999")
        # Only the GET happened; no read POST for a non-match.
        self.assertEqual(len(api.call_args_list), 1)
        self.assertIn("No unread notification found for task PLS-999", result)
        self.assertIn("pulse_list_notifications", result)

    def test_unknown_uuid_404_gives_actionable_error_not_raw_404(self):
        err = {"error": "404 Client Error", "detail": "Notification not found", "status_code": 404}
        with mock.patch.object(pulse_server, "api_request", return_value=err):
            result = pulse_server.mark_notification_read(_UUID)
        self.assertIn("not found", result)
        self.assertIn("pulse_list_notifications", result)  # points to the fix
        self.assertIn("not a task key", result)


class ListNotificationsTests(unittest.TestCase):
    def test_listing_surfaces_uuid_and_task_key(self):
        listing = {"results": [
            {"id": _UUID, "item_key": "PLS-237", "is_read": False,
             "message": "Assigned to you", "created_at": "2026-07-03"},
        ]}
        with mock.patch.object(pulse_server, "api_request", return_value=listing):
            out = pulse_server.list_notifications()
        self.assertIn(_UUID, out)          # the UUID is now discoverable
        self.assertIn("task PLS-237", out)
        self.assertIn("[UNREAD]", out)


if __name__ == "__main__":
    unittest.main()

"""PLS-858 regression: the MCP error formatter must SURFACE the API's `message`
key, not truncate to the machine `error` code.

Measurement (HIVE-758, 2026-08-03): the wip_capacity_denied body at
tasks/views.py carries `error` (code) + `message` (the human remedy). The old
`_error_detail_text` consulted `detail` -> `error` -> raw dict and never
`message`, so agents saw only `wip_capacity_denied` while the server told them
exactly how to unblock. Two agents each published (then retracted) wrong
mechanism analyses because the discarded sentence was the remedy.
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class ErrorDetailMessageTests(unittest.TestCase):
    def test_error_plus_message_surfaces_the_message(self):
        """The exact wip_capacity_denied body shape: remedy text must reach the
        caller (combined with the code so code-matching callers keep working)."""
        body = {
            "error": "wip_capacity_denied",
            "message": (
                "sara@shizuha.com is at the configured WIP cap; route to the "
                "team queue instead of assigning directly."
            ),
        }
        out = pulse_server._error_detail_text({"detail": body})
        # The remedy sentence (the discarded payload) must be present.
        self.assertIn("route to the team queue instead of assigning directly", out)
        # The code is retained alongside so code-matching callers do not regress.
        self.assertIn("wip_capacity_denied", out)

    def test_error_only_still_surfaces_code(self):
        """Polarity 1: a body with only `error` still surfaces the code."""
        out = pulse_server._error_detail_text({"detail": {"error": "some_code"}})
        self.assertEqual(out, "some_code")

    def test_message_only_surfaces_message(self):
        """A body carrying only the human text surfaces it directly."""
        out = pulse_server._error_detail_text(
            {"detail": {"message": "please try again later"}}
        )
        self.assertIn("please try again later", out)

    def test_human_detail_path_unchanged(self):
        """The existing `detail`-preference path (PLS-859/PLS-523) is intact.

        `_error_detail_text` prefers the human detail; the structured
        `code`/`marker` layer markers are surfaced by `_fmt_error` (PLS-523),
        and the detail path must not be disturbed by the new message handling.
        """
        out = pulse_server._error_detail_text(
            {
                "detail": {
                    "detail": "Pulse project list failed unexpectedly.",
                    "code": "projects_unexpected_error",
                    "marker": "PLS358_PROJECTS_UNEXPECTED_ERROR",
                }
            }
        )
        self.assertIn("Pulse project list failed unexpectedly.", out)
        # _fmt_error is the marker-surfacing boundary (existing PLS-523 fixture).
        fmt = pulse_server._fmt_error({
            "error": "500 Server Error: Internal Server Error",
            "status_code": 500,
            "detail": {
                "detail": "Pulse project list failed unexpectedly.",
                "code": "projects_unexpected_error",
                "marker": "PLS358_PROJECTS_UNEXPECTED_ERROR",
            },
        })
        self.assertIn("Pulse project list failed unexpectedly.", fmt)
        self.assertIn("code=projects_unexpected_error", fmt)
        self.assertIn("marker=PLS358_PROJECTS_UNEXPECTED_ERROR", fmt)

    def test_fmt_error_end_to_end_message_surfaced(self):
        """_fmt_error on the exact denial body shows the remedy plus code."""
        out = pulse_server._fmt_error({
            "error": "409 Client Error: Conflict for url...",
            "status_code": 409,
            "detail": {
                "error": "wip_capacity_denied",
                "message": (
                    "sara@shizuha.com is at the configured WIP cap; route to the "
                    "team queue instead of assigning directly."
                ),
            },
        })
        self.assertIn("route to the team queue instead of assigning directly", out)
        self.assertIn("wip_capacity_denied", out)


if __name__ == "__main__":
    unittest.main()

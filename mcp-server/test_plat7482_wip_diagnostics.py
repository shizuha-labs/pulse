"""PLAT-7482 regression: the MCP error formatter must render the WIP-capacity
diagnostics snapshot when a denial body carries it.

ichi's live incident (PLAT-7461): a Start Work refusal rendered to the agent as
only "Active WIP capacity is full; finish or stop the current task before
starting another." — the typed ``_wip_capacity_diagnostics`` payload (actual /
pending / cap / reservation_owner / owner_kind) was flattened into the REST body
by the view but dropped by _fmt_error, so a seat provably holding zero
in_progress tasks could not self-diagnose the mismatch.
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class FmtErrorWipDiagnosticsTests(unittest.TestCase):
    BODY = {
        "error": "Active WIP capacity is full; finish or stop the current task "
                 "before starting another.",
        "missing_fields": [],
        "code": "WIP_ACTIVE_CAPACITY_DENIED",
        "actual": 1,
        "pending": 0,
        "cap": 1,
        "reservation_owner": "PLAT-7461",
        "reservation_owner_kind": "actual_item",
        "capacity_generation": 7,
        "slot_kind": "active",
        "assignment_group": "engineering",
        "assignee_email": "ichi@shizuha.com",
        "requested_item": "PLAT-7462",
    }

    def test_diagnostics_rendered_on_start_work_refusal(self):
        out = pulse_server._fmt_error({
            "error": "400 Client Error: Bad Request",
            "status_code": 400,
            "detail": dict(self.BODY),
        })
        self.assertIn("Active WIP capacity is full", out)
        self.assertIn("code=WIP_ACTIVE_CAPACITY_DENIED", out)
        self.assertIn("WIP capacity:", out)
        self.assertIn("actual=1", out)
        self.assertIn("pending=0", out)
        self.assertIn("cap=1", out)
        self.assertIn("slot_kind=active", out)
        self.assertIn("reservation_owner=PLAT-7461 (actual_item)", out)
        self.assertIn("assignment_group=engineering", out)

    def test_diagnostics_rendered_for_assignment_denial_shape(self):
        # The direct-assignment denial body (views.py wip_capacity_denied)
        # carries the same flattened snapshot without the transition `code`.
        body = dict(self.BODY)
        body.pop("code")
        body["error"] = "wip_capacity_denied"
        body["message"] = "ichi@shizuha.com is at the configured WIP cap; ..."
        out = pulse_server._fmt_error({
            "error": "409 Client Error",
            "status_code": 409,
            "detail": body,
        })
        self.assertIn("WIP capacity:", out)
        self.assertIn("reservation_owner=PLAT-7461 (actual_item)", out)

    def test_no_diagnostics_block_without_snapshot(self):
        out = pulse_server._fmt_error({
            "error": "400 Client Error",
            "status_code": 400,
            "detail": {"error": "screen_field_required", "code": "SCREEN_FIELDS"},
        })
        self.assertNotIn("WIP capacity:", out)

    def test_non_dict_detail_ignored(self):
        out = pulse_server._fmt_error({
            "error": "400 Client Error",
            "status_code": 400,
            "detail": "plain text body",
        })
        self.assertNotIn("WIP capacity:", out)


if __name__ == "__main__":
    unittest.main()

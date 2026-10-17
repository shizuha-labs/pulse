"""PLS-523 regression: the MCP error formatter must PRESERVE the server's
structured failing-layer markers (backend `code` + `marker`), not just the human
`detail`.

banto's PLS-523 evidence: a 500 body `{"detail": "...", "code": "...",
"marker": "PLS358_PROJECTS_UNEXPECTED_ERROR"}` rendered to the agent as only the
human detail — the `code`/`marker` that localize the failing layer were dropped,
so operators could not tell WHICH layer failed.
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class FmtErrorMarkerTests(unittest.TestCase):
    def test_marker_and_code_surfaced_from_dict_body(self):
        out = pulse_server._fmt_error({
            "error": "500 Server Error: Internal Server Error",
            "status_code": 500,
            "detail": {
                "detail": "Pulse project list failed unexpectedly.",
                "code": "projects_unexpected_error",
                "marker": "PLS358_PROJECTS_UNEXPECTED_ERROR",
            },
        })
        self.assertIn("Pulse project list failed unexpectedly.", out)
        self.assertIn("code=projects_unexpected_error", out)
        self.assertIn("marker=PLS358_PROJECTS_UNEXPECTED_ERROR", out)
        # 500 still carries the transient-degradation hint.
        self.assertIn("transient", out)

    def test_marker_surfaced_from_json_string_body(self):
        # Some capture paths stash the body as a JSON string, not a dict.
        out = pulse_server._fmt_error({
            "error": "500 Server Error",
            "status_code": 500,
            "detail": '{"detail": "Transition committed; display enrichment degraded.",'
                      ' "code": "transition_receipt_degraded",'
                      ' "marker": "PLS523_TRANSITION_RECEIPT_DEGRADED"}',
        })
        self.assertIn("marker=PLS523_TRANSITION_RECEIPT_DEGRADED", out)
        self.assertIn("code=transition_receipt_degraded", out)

    def test_no_marker_bits_when_body_has_none(self):
        out = pulse_server._fmt_error({
            "error": "400 Client Error",
            "status_code": 400,
            "detail": {"detail": "bad request"},
        })
        self.assertIn("bad request", out)
        self.assertNotIn("marker=", out)
        self.assertNotIn("code=", out)


if __name__ == "__main__":
    unittest.main()

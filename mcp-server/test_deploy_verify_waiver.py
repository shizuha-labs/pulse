"""MCP tool coverage for pulse_issue_deploy_verify_waiver (PLS-675 / PLAT-1351 v4).

The backend waiver mechanism (WorkflowService.issue_deploy_verify_waiver + the
DRF action `issue-deploy-verify-waiver`) is role-gated and tested in
tasks/tests/test_plat1351_deploy_verify_gate.py. This test pins the MCP-server
adapter: correct endpoint, reason payload, and error passthrough — so the
operator-facing invocation path stays wired.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class DeployVerifyWaiverMcpTests(unittest.TestCase):
    def test_issues_waiver_via_item_action_endpoint(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen.update({"method": method, "endpoint": endpoint, "data": data, "as_user": as_user})
            return {
                "id": 9001,
                "item_key": "TRD-34",
                "origin_event": "receipt.deploy_verify_waiver",
                "origin_event_id": "deploy-waiver:TRD-34:devops@shizuha.com:2026-08-09T00:00:00+00:00",
                "origin_payload": {"result": "issued", "issuer_team": "devops"},
                "caused_by_email_snapshot": "devops@shizuha.com",
            }

        with mock.patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.issue_deploy_verify_waiver(
                task_id="TRD-34", reason="no correlated production deployment for QA-only task",
            )

        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["endpoint"], "/items/TRD-34/issue-deploy-verify-waiver/")
        # Operator-bound action: acts as the chatting user (operator), never the agent.
        self.assertTrue(seen["as_user"])
        self.assertEqual(
            seen["data"],
            {"reason": "no correlated production deployment for QA-only task"},
        )
        self.assertIn("Deploy-verify waiver issued on [TRD-34]", out)
        self.assertIn("receipt.deploy_verify_waiver", out)

    def test_reason_is_optional_at_adapter_level(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen.update({"data": data})
            return {"item_key": "TRD-29", "origin_event": "receipt.deploy_verify_waiver"}

        with mock.patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            pulse_server.issue_deploy_verify_waiver(task_id="TRD-29")

        # The adapter passes an empty payload when reason is omitted; the
        # backend enforces the non-empty reason requirement (role-gated service).
        self.assertEqual(seen["data"], {})

    def test_backend_error_is_formatted_not_raised(self):
        with mock.patch.object(
            pulse_server,
            "api_request",
            return_value={
                "error": "403 Client Error",
                "detail": {"detail": "automation cannot issue a deploy-verify waiver"},
                "status_code": 403,
            },
        ):
            out = pulse_server.issue_deploy_verify_waiver(
                task_id="TRD-34", reason="x",
            )

        self.assertIn("automation cannot issue a deploy-verify waiver", out)


if __name__ == "__main__":
    unittest.main()

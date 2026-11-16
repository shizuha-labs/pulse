"""MCP tool coverage for pulse_issue_deploy_verify_waiver (PLS-675 / PLAT-1351 v4).

The backend waiver mechanism (WorkflowService.issue_deploy_verify_waiver + the
DRF action `issue-deploy-verify-waiver`) is role-gated and tested in
tasks/tests/test_plat1351_deploy_verify_gate.py. This test pins the MCP-server
adapter: correct endpoint, reason payload, and error passthrough — so the
operator-facing invocation path stays wired.

PLAT-12577 follow-up: with no user-delegation token the adapter calls Pulse
with the agent's own token and the server-side roster check is the authority.
A present delegation still acts as that user. Automation identities are
refused before the HTTP call.
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
        # No delegation token: the agent's own token carries the call.
        self.assertFalse(seen["as_user"])
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

    def test_no_delegation_uses_the_agent_token(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen["as_user"] = as_user
            return {"item_key": "PLAT-11842", "origin_event": "receipt.deploy_verify_waiver"}

        with mock.patch.object(pulse_server, "_get_effective_agent_token", return_value="agent-token"), \
                mock.patch.object(pulse_server, "_get_effective_user_token", return_value=""), \
                mock.patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            pulse_server.issue_deploy_verify_waiver(task_id="PLAT-11842", reason="operator mint")

        self.assertFalse(seen["as_user"])

    def test_automation_caller_is_refused_before_the_http_call(self):
        token = waiver_token({"email": "system@pulse"})
        with mock.patch.object(pulse_server, "_get_effective_agent_token", return_value=token), \
                mock.patch.object(pulse_server, "api_request") as api:
            out = pulse_server.issue_deploy_verify_waiver(task_id="PLAT-11842", reason="no")
        api.assert_not_called()
        self.assertIn("automation identities cannot mint", out)

    def test_delegation_present_acts_as_the_user(self):
        seen = {}

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            seen["as_user"] = as_user
            return {"item_key": "PLAT-11842", "origin_event": "receipt.deploy_verify_waiver"}

        with mock.patch.object(pulse_server, "_get_effective_agent_token", return_value="agent-token"), \
                mock.patch.object(pulse_server, "_get_effective_user_token", return_value="user-token"), \
                mock.patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            pulse_server.issue_deploy_verify_waiver(task_id="PLAT-11842", reason="delegated")

        self.assertTrue(seen["as_user"])


def waiver_token(claims):
    import base64
    import json

    def segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}."


if __name__ == "__main__":
    unittest.main()

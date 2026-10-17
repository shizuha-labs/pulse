"""PLS-1397: escalation-class creates must fail loud on transport timeout.

Also pins the MCP docstring to every evidence class the validator accepts,
so the published primitive can carry those tags (part 1).
"""
from unittest import mock

import requests

import pulse_server


def test_timeout_is_typed_retryable_and_stable():
    with mock.patch.object(pulse_server, "get_headers", return_value={}), mock.patch.object(
        pulse_server.requests, "request", side_effect=requests.Timeout("read timed out"),
    ):
        first = pulse_server.api_request("POST", "/items/", data={"title": "blocker"})
        second = pulse_server.api_request("POST", "/items/", data={"title": "blocker"})
    assert first["error_code"] == "pulse_transport_timeout"
    assert first["retryable"] is True
    assert first["dedup_key"]
    assert first["dedup_key"] == second["dedup_key"]
    assert "dedup=" in first["trace"]
    text = pulse_server._fmt_error(first)
    assert "pulse_transport_timeout" in text
    assert first["dedup_key"] in text
    assert "retryable=True" in text


def test_execute_transition_doc_lists_validator_evidence_classes():
    import inspect
    doc = inspect.getdoc(pulse_server.execute_transition) or ""
    for klass in (
        "human_held_source_evidence",
        "operator_decision",
        "operator_held_credential_access",
        "human_physical_action",
        "internal_capability_gap",
    ):
        assert klass in doc, klass


def test_human_ops_without_lane_tag_is_rejected_before_api_write():
    with mock.patch.object(pulse_server, "api_request") as api:
        out = pulse_server.create_blocker_task(
            parent_id="HIVE-2523",
            team_slug="human-ops",
            title="Run the drawer probe",
            description="## Recommendation\nOperator runs the two-line probe.",
        )
    assert "role-gated-operator" in out
    assert "No blocker task was created" in out
    api.assert_not_called()


def test_create_timeout_echoes_stable_dedup_not_uuid():
    parent = {"id": "p", "item_key": "HIVE-2523", "project": {"id": 1}}
    seen = {}

    def fake_api(method, endpoint, **kwargs):
        if method == "GET":
            return parent
        seen["data"] = kwargs.get("data")
        return {
            "error": "Read timed out",
            "error_code": "pulse_transport_timeout",
            "retryable": True,
            "dedup_key": "transport-hash",
        }

    with mock.patch.object(pulse_server, "api_request", side_effect=fake_api):
        first = pulse_server.create_blocker_task(
            parent_id="HIVE-2523",
            team_slug="engineering",
            title="Fix the relay",
            description="Engineering owns the code fix.",
        )
        second = pulse_server.create_blocker_task(
            parent_id="HIVE-2523",
            team_slug="engineering",
            title="Fix the relay",
            description="Engineering owns the code fix.",
        )
    assert "create_timeout" in first
    assert "retryable=true" in first
    assert "pulse_transport_timeout" in first
    key = seen["data"]["metadata"]["create_dedup_key"]
    assert key in first and key in second
    assert len(key) == 16

"""PLS-574 — pulse_propose_workflow_change organization-project binding.

Regression matrix for the architecture contract (d328824d §3.9, ratified by
sora + hiro cross-review). A workflow-change CR is now an org-scoped `mode=task`
item, so POST /items/ rejects it without a project. The tool must:

  1. Require an explicit `project_id` (no silent "first"/default pick, even when
     the caller has exactly one writable project) — a missing id fails at the
     MCP boundary with actionable candidates and creates NO item.
  2. Bind the CR to the supplied project (its tracking/authz HOME — not the
     scope of the target workflow's effect); a valid id creates the proposed
     item in that project with the authoritative typed metadata + pre-approval
     seal.
  3. Land only in the selected project across multiple orgs (no substitution).
  4. Reject a foreign/unwritable id fail-closed — no fallback, no item.
  5. R3 atomicity/compensation: if sealing (the auto-submit that carries the
     seal) fails, DELETE the just-created item so no usable UNSEALED proposal
     can remain.

The classifier internals (typed op-type tiering) are exercised in
test_plat1004_classifier_guard; here `_tcs` is stubbed where a typed spec is
needed so these tests pin the project-binding contract in isolation.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


# Two writable projects across two orgs — the caller's accessible set.
_PROJECTS = [
    {"id": 335, "key": "PLS", "name": "Pulse", "org_id": 1,
     "organization_name": "Shizuha", "access_level": "open", "can_create": True},
    {"id": 700, "key": "BRAND", "name": "Brand", "org_id": 7,
     "organization_name": "Brand Co", "access_level": "open", "can_create": True},
]


class _Recorder:
    """A dispatch stub for pulse_server.api_request that records every call and
    lets each test control the projects list, the create response, the
    submit(seal) response, and the delete response."""

    def __init__(self, submit_error=False, delete_error=False, projects=_PROJECTS,
                 projects_error=False):
        self.calls = []
        self.submit_error = submit_error
        self.delete_error = delete_error
        self.projects = projects
        self.projects_error = projects_error

    def __call__(self, method, path, data=None, params=None, **kw):
        self.calls.append((method, path, data or {}, params or {}))
        if method == "GET" and path == "/projects/":
            if self.projects_error:
                return {"error": "500 Server Error", "detail": {"error": "list boom"},
                        "status_code": 500}
            return {"results": list(self.projects), "next": None}
        if method == "DELETE" and path.startswith("/items/"):
            if self.delete_error:
                return {"error": "500 Server Error", "detail": {"error": "delete boom"},
                        "status_code": 500}
            return {"detail": "deleted"}
        if "execute-transition" in path:
            if self.submit_error:
                return {"error": "400 Client Error", "detail": {"error": "seal boom"},
                        "status_code": 400}
            return {"assignee_email": "operator@shizuha.com",
                    "assignment_group": "admin-ops"}
        if method == "POST" and path == "/items/":
            return {"id": "CR-UUID-1", "item_key": "PLS-9001", "title": data.get("title"),
                    "workflow_name": "workflow-change", "status": "proposed"}
        return {}

    # convenience assertions
    def item_creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/items/"]

    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]

    def submits(self):
        return [c for c in self.calls if "execute-transition" in c[1]]


def _force_typed(tier="risky"):
    """Patch _tcs so change_spec is treated as a valid typed envelope classified
    at `tier`, decoupled from the real workflow-fetch/classifier."""
    env = {"version": 1, "operations": [{"op": "set_knowledge_article"}], "rationale": "x"}
    return mock.patch.multiple(
        pulse_server._tcs,
        parse_envelope=mock.Mock(return_value=env),
        version_error=mock.Mock(return_value=None),
        validate_and_classify=mock.Mock(return_value={"reject": None, "tier": tier}),
        format_seal=mock.Mock(return_value="TYPED-SPEC-SEAL sha256=deadbeef tier=" + tier),
    )


class ProjectRequiredTests(unittest.TestCase):
    def test_missing_project_id_fails_at_boundary_with_candidates_and_no_item(self):
        rec = _Recorder()
        with mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="t", target_workflow="security-finding",
                change_spec="just prose", reason="r", impact="i",
            )
        self.assertIn("Error", out)
        self.assertIn("project_id` is required", out)
        # actionable dynamic candidates from the accessible set
        self.assertIn("project_id=335", out)
        self.assertIn("project_id=700", out)
        self.assertIn("pulse_list_projects", out)
        # NO item was created
        self.assertEqual(rec.item_creates(), [])

    def test_foreign_or_unwritable_project_fails_closed_no_fallback_no_item(self):
        rec = _Recorder()
        with mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="t", target_workflow="security-finding",
                change_spec="just prose", reason="r", impact="i",
                project_id=999,  # not in the accessible set
            )
        self.assertIn("Error", out)
        self.assertIn("999", out)
        self.assertIn("No item was created", out)
        self.assertEqual(rec.item_creates(), [])  # no fallback to another project

    def test_project_list_error_with_explicit_id_fails_closed_no_item(self):
        rec = _Recorder(projects_error=True)
        with mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="t", target_workflow="security-finding",
                change_spec="just prose", reason="r", impact="i",
                project_id=999,
            )
        self.assertIn("fail closed", out)
        self.assertIn("not created", out)
        self.assertEqual(rec.item_creates(), [])

    def test_visible_read_only_project_fails_closed_no_item(self):
        rec = _Recorder(projects=[{
            "id": 335, "key": "PLS", "name": "Pulse", "org_id": 1,
            "organization_name": "Shizuha", "access_level": "private",
            "can_create": False,
        }])
        with mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="t", target_workflow="security-finding",
                change_spec="just prose", reason="r", impact="i",
                project_id=335,
            )
        self.assertIn("not among your accessible/writable projects", out)
        self.assertEqual(rec.item_creates(), [])


class ProjectBindingTests(unittest.TestCase):
    def test_valid_project_creates_item_bound_to_that_project_with_seal(self):
        rec = _Recorder()
        with _force_typed(tier="risky"), \
                mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="Add note", target_workflow="autonomous-dev",
                change_spec='{"version":1,"operations":[]}', reason="r", impact="i",
                project_id=335,
            )
        self.assertNotIn("Error", out)
        self.assertIn("submitted for review", out)
        creates = rec.item_creates()
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2].get("project"), 335)  # bound to the tracking home
        # the seal rides the auto-submit transition comment
        submits = rec.submits()
        self.assertEqual(len(submits), 1)
        self.assertIn("TYPED-SPEC-SEAL", submits[0][2].get("comment", ""))

    def test_each_org_project_lands_only_in_the_selected_project(self):
        for pid in (335, 700):
            rec = _Recorder()
            with _force_typed(tier="risky"), \
                    mock.patch.object(pulse_server, "api_request", side_effect=rec):
                pulse_server.propose_workflow_change(
                    title="t", target_workflow="autonomous-dev",
                    change_spec='{"version":1,"operations":[]}', reason="r", impact="i",
                    project_id=pid,
                )
            creates = rec.item_creates()
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0][2].get("project"), pid)


class SealCompensationTests(unittest.TestCase):
    def test_seal_failure_deletes_the_item_no_unsealed_proposal_remains(self):
        rec = _Recorder(submit_error=True)
        with _force_typed(tier="risky"), \
                mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="t", target_workflow="autonomous-dev",
                change_spec='{"version":1,"operations":[]}', reason="r", impact="i",
                project_id=335,
            )
        self.assertIn("Error", out)
        self.assertIn("rolled back", out)
        # the just-created item was compensated (deleted)
        self.assertEqual(len(rec.item_creates()), 1)
        self.assertEqual(len(rec.deletes()), 1)
        self.assertIn("CR-UUID-1", rec.deletes()[0][1])

    def test_seal_and_rollback_both_fail_surfaces_manual_delete_warning(self):
        rec = _Recorder(submit_error=True, delete_error=True)
        with _force_typed(tier="risky"), \
                mock.patch.object(pulse_server, "api_request", side_effect=rec):
            out = pulse_server.propose_workflow_change(
                title="t", target_workflow="autonomous-dev",
                change_spec='{"version":1,"operations":[]}', reason="r", impact="i",
                project_id=335,
            )
        self.assertIn("Error", out)
        self.assertIn("UNSEALED", out)
        self.assertIn("Manually delete", out)
        self.assertEqual(len(rec.deletes()), 1)  # rollback was attempted


if __name__ == "__main__":
    unittest.main()

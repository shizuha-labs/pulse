"""PLAT-4178 tool-contract regressions for the group-only assignment path.

The endpoint fix makes `/assign/` accept a KNOWN team group-only (200) when no
concrete member is currently available, instead of 400. These tests pin the
matching MCP tool-layer contract:

1. `pulse_assign_task` must report a group-only 200 as SUCCESS (a team queue),
   not "assignment API returned success ... did not report a concrete assignee".
   Previously only `queued_at_capacity` was recognized, so the new
   `queued_no_available_member` queue (and the pre-existing `zero_member_team_alert`
   queue) were mis-reported as tool errors.
2. On a genuine reassignment error, update must surface the server's response
   body (the operator's explicit ask) rather than a bare "400 Client Error".
3. Normal task creation commits direct/team assignment in the initial POST and
   never follows with a second `/assign/` request (PLS-465).
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


def _group_only(flag):
    """A 200 group-only assign response carrying the given queue flag."""
    body = {
        "item_id": "P4178-1", "item_key": "P4178-1", "title": "t",
        "assignee_id": None, "assignee_email": None, "assignee": None,
        "assignment_group": "engineering",
    }
    body[flag] = True
    return body


def _assign_error():
    """An api_request() error dict as produced for a 400 on /assign, with the
    server's response body captured under `detail` (per api_request)."""
    return {
        "error": "400 Client Error: Bad Request for url: .../assign/",
        "detail": {"error": "No available assignee could be resolved for team 'engineering'"},
        "status_code": 400,
    }


class AssignTaskGroupOnlyTests(unittest.TestCase):
    def _assign(self, response):
        with mock.patch.object(pulse_server, "api_request", return_value=response):
            return pulse_server.assign_task("P4178-1", assignment_group="engineering")

    def test_queued_no_available_member_reported_as_success(self):
        out = self._assign(_group_only("queued_no_available_member"))
        self.assertNotIn("Error", out)
        self.assertIn("Queued", out)
        self.assertIn("engineering", out)
        self.assertIn("no member currently available", out)

    def test_queued_at_capacity_still_reported_as_success(self):
        out = self._assign(_group_only("queued_at_capacity"))
        self.assertNotIn("Error", out)
        self.assertIn("Queued", out)
        self.assertIn("WIP cap", out)

    def test_zero_member_team_reported_as_success(self):
        out = self._assign(_group_only("zero_member_team_alert"))
        self.assertNotIn("Error", out)
        self.assertIn("Queued", out)
        self.assertIn("zero-member", out)

    def test_concrete_assignee_still_reported_as_assigned(self):
        resp = {"item_key": "P4178-1", "title": "t",
                "assignee_email": "kai@shizuha.com", "assignment_group": "engineering"}
        out = self._assign(resp)
        self.assertIn("Assigned", out)
        self.assertIn("kai@shizuha.com", out)

    def test_no_assignee_and_no_team_signal_is_still_an_error(self):
        # A direct-assignee call whose 200 response carries neither an assignee nor
        # any team route is a genuine malformed-success failure (safety net).
        resp = {"item_key": "P4178-1", "title": "t",
                "assignee_email": None, "assignee": None, "assignment_group": None}
        with mock.patch.object(pulse_server, "api_request", return_value=resp):
            out = pulse_server.assign_task(
                "P4178-1", assignee_email="x@shizuha.com", direct_assignment_reason="r",
            )
        self.assertIn("Error", out)

    def test_malformed_team_route_200_fails_closed(self):
        # rei P1: a request-side assignment_group must NOT be treated as proof the
        # server persisted a team route. A 200 with no assignee, no queue flag, and no
        # response-side assignment_group must FAIL CLOSED — otherwise an API regression
        # could silently leave the task unrouted while the tool claims a queue success.
        resp = {"item_key": "PLAT-X", "title": "t"}  # no assignee / flag / response group
        with mock.patch.object(pulse_server, "api_request", return_value=resp):
            out = pulse_server.assign_task("PLAT-X", assignment_group="engineering")
        self.assertIn("Error", out)
        self.assertNotIn("Queued", out)


class AssignErrorDetailSurfacingTests(unittest.TestCase):
    def test_error_detail_text_extracts_server_body(self):
        self.assertEqual(
            pulse_server._error_detail_text(_assign_error()),
            "No available assignee could be resolved for team 'engineering'",
        )
        self.assertEqual(pulse_server._error_detail_text({"detail": ""}), "")
        self.assertEqual(pulse_server._error_detail_text({"detail": "{}"}), "")

    def test_error_detail_text_surfaces_assignment_validation_details(self):
        """PLS-859: never drop the field→constraint map on assignment_validation_failed."""
        body = {
            "error": "409 Client Error: Conflict for url: .../assign/",
            "detail": {
                "error": "assignment_validation_failed",
                "detail": "assignee_id: routing_authority:direct_writer_denied",
                "details": {
                    "assignee_id": ["routing_authority:direct_writer_denied"],
                },
            },
            "status_code": 409,
        }
        text = pulse_server._error_detail_text(body)
        self.assertIn("assignment_validation_failed", text)
        self.assertIn("assignee_id", text)
        self.assertIn("direct_writer_denied", text)

        # Legacy body with only the structured map (no human detail string).
        legacy = {
            "error": "409 Client Error",
            "detail": {
                "error": "assignment_validation_failed",
                "details": {"assignee_email": ["not authorized for this task organization"]},
            },
            "status_code": 409,
        }
        legacy_text = pulse_server._error_detail_text(legacy)
        self.assertIn("assignment_validation_failed", legacy_text)
        self.assertIn("assignee_email", legacy_text)
        self.assertIn("not authorized", legacy_text)

    def test_assign_task_announces_alias_rewrite(self):
        """PLS-859: trading-engineering → engineering must not look like silent fallback."""
        resp = {
            "item_key": "PLAT-4812",
            "title": "t",
            "assignee_email": "eng@shizuha.com",
            "assignment_group": "engineering",
            "assignment_group_canonicalized": True,
            "requested_assignment_group": "trading-engineering",
        }
        with mock.patch.object(pulse_server, "api_request", return_value=resp):
            out = pulse_server.assign_task(
                "PLAT-4812", assignment_group="trading-engineering",
            )
        self.assertIn("Assigned", out)
        self.assertIn("engineering", out)
        self.assertIn("trading-engineering", out)
        self.assertIn("canonicalized", out.lower())
        self.assertIn("not a silent fallback", out.lower())

    def test_assign_error_explanation_includes_body(self):
        expl = pulse_server._assign_error_explanation(_assign_error())
        self.assertIn("400 Client Error", expl)
        self.assertIn("No available assignee could be resolved for team 'engineering'", expl)

    def _url_aware_stub(self):
        def stub(method, path, data=None, **kw):
            if "/assign" in path:  # _item_path adds a trailing slash
                return _assign_error()
            base = {"id": "P4178-1", "item_id": "P4178-1", "item_key": "P4178-1",
                    "title": "t", "workflow_name": "autonomous-dev",
                    "workflow_status": "open", "status": "open"}
            if method == "PATCH":
                base["title"] = "new"
            return base
        return stub

    def test_direct_create_sends_final_assignment_in_initial_post_only(self):
        calls = []

        def stub(method, path, data=None, **kw):
            calls.append((method, path, data or {}))
            return {
                "id": "P465-1", "item_key": "P465-1", "title": "t",
                "workflow_name": "reviewed-task", "workflow_status": "open",
                "status": "open", "assignee_email": "hiro@shizuha.com",
                "assignment_group": "review",
            }

        with mock.patch.object(pulse_server, "api_request", side_effect=stub):
            out = pulse_server.create_task(
                title="t", project_id=1, workflow="reviewed-task",
                assignment_group="review", assignee_email="hiro@shizuha.com",
                direct_assignment_reason="exact-version continuity",
            )

        self.assertIn("Task created", out)
        item_posts = [c for c in calls if c[0] == "POST" and c[1] == "/items/"]
        self.assertEqual(len(item_posts), 1)
        payload = item_posts[0][2]
        self.assertEqual(payload["assignment_group"], "review")
        self.assertEqual(payload["assignee_email"], "hiro@shizuha.com")
        self.assertEqual(payload["direct_assignment_reason"], "exact-version continuity")
        self.assertFalse(any("/assign" in path for _, path, _ in calls))

    def test_team_only_create_does_not_repeat_assign_endpoint(self):
        calls = []

        def stub(method, path, data=None, **kw):
            calls.append((method, path, data or {}))
            return {
                "id": "P465-2", "item_key": "P465-2", "title": "t",
                "workflow_name": "autonomous-dev", "workflow_status": "open",
                "status": "open", "assignee_email": "kai@shizuha.com",
                "assignment_group": "engineering",
            }

        with mock.patch.object(pulse_server, "api_request", side_effect=stub):
            out = pulse_server.create_task(
                title="t", project_id=1, workflow="autonomous-dev",
                assignment_group="engineering",
            )

        self.assertIn("Task created", out)
        self.assertEqual(
            [path for method, path, _ in calls if method == "POST"],
            ["/items/"],
        )

    def test_update_surfaces_assignment_error_body(self):
        with mock.patch.object(pulse_server, "api_request", side_effect=self._url_aware_stub()):
            out = pulse_server.update_task(
                task_id="P4178-1", title="new", assignment_group="engineering",
            )
        self.assertIn("Assignment change failed", out)
        self.assertIn("No available assignee could be resolved for team 'engineering'", out)


class EvidenceBlockerCreateContractTests(unittest.TestCase):
    """PLS-463: the MCP helper materializes the event-hook input atomically."""

    @staticmethod
    def _stub(method, path, data=None, **kwargs):
        if method == 'GET':
            return {'id': 'parent-uuid', 'item_key': 'BKSG-9', 'project': {'id': 7}}
        if method == 'POST' and path == '/items/':
            return {'id': 'child-uuid', 'item_key': 'BKSG-10', 'title': 'Evidence'}
        return {'ok': True}

    def test_named_evidence_owner_is_in_initial_create_and_skips_team_reassign(self):
        with mock.patch.object(pulse_server, 'api_request', side_effect=self._stub) as api:
            out = pulse_server.create_blocker_task(
                parent_id='BKSG-9', team_slug='admin-ops', title='Supply statement',
                description='Need the bank-issued June statement.', priority='urgent',
                evidence_owner_email='owner@shizuha.com',
                required_evidence='June bank statement PDF',
                due_date='2026-07-14T09:00:00+05:30',
            )

        self.assertIn('BKSG-10', out)
        create_call = next(
            call for call in api.call_args_list
            if call.args[:2] == ('POST', '/items/')
        )
        payload = create_call.kwargs['data']
        self.assertEqual(payload['assignee_email'], 'owner@shizuha.com')
        self.assertEqual(payload['metadata']['evidence_request']['required_evidence'],
                         'June bank statement PDF')
        self.assertIn('evidence-request', payload['tags'])
        self.assertFalse(any('/assign' in str(call.args[1]) for call in api.call_args_list))

    def test_evidence_fields_are_all_or_nothing(self):
        with mock.patch.object(pulse_server, 'api_request', side_effect=self._stub) as api:
            out = pulse_server.create_blocker_task(
                parent_id='BKSG-9', team_slug='admin-ops', title='Supply statement',
                description='Need evidence.', evidence_owner_email='owner@shizuha.com',
            )
        self.assertIn('must all be provided', out)
        self.assertFalse(any(call.args[:2] == ('POST', '/items/') for call in api.call_args_list))

    def test_evidence_lane_never_targets_ceo(self):
        with mock.patch.object(pulse_server, 'api_request', side_effect=self._stub) as api:
            out = pulse_server.create_blocker_task(
                parent_id='BKSG-9', team_slug='admin-ops', title='Supply statement',
                description='Need evidence.', evidence_owner_email='operator@example.test',
                required_evidence='Statement', due_date='2026-07-14T09:00:00+05:30',
            )
        self.assertIn('cannot target the CEO', out)
        self.assertFalse(any(call.args[:2] == ('POST', '/items/') for call in api.call_args_list))

if __name__ == "__main__":
    unittest.main()

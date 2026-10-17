"""
Regression tests for PLAT-48: pulse_link_issues sent the wrong field name.

ItemLinkCreateSerializer accepts `target_item_id` (numeric pk) or
`target_item_key` (e.g. "PLAT-25"), and rejects a bare `target_item` with
HTTP 400 ("Either target_item_id or target_item_key is required"). These tests
pin the outgoing payload to the field names the serializer actually accepts.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class _ForgejoPullResp:
    """Minimal Origin/Forgejo PR fetch stub for link_pr's trusted head_sha fetch.

    Mirrors the response surface link_pr consumes (PLS-496 recipe): ok,
    status_code (404 discrimination) and the json() payload fields head.sha /
    merged / state / merged_at / merge_commit_sha.
    """

    ok = True
    status_code = 200

    def json(self):
        return {
            "state": "open",
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": "",
            "head": {"sha": "e0e0374897ae33221b9024036ebd4a746f4b7235"},
        }


class LinkIssuesPayloadTests(unittest.TestCase):
    def _call(self, target, resolved):
        """Invoke link_issues with api_request + _resolve_item_pk mocked.

        Returns the (path, data) the tool POSTed to the links endpoint.
        """
        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=resolved), \
             mock.patch.object(pulse_server, "api_request", return_value={}) as api:
            pulse_server.link_issues("PLAT-46", target, link_type="blocks")
        # Find the POST to the links endpoint.
        for call in api.call_args_list:
            args, kwargs = call
            if args and args[0] == "POST" and args[1].endswith("/links/"):
                return args[1], kwargs["data"]
        self.fail("link_issues did not POST to a /links/ endpoint")

    def test_key_target_sends_target_item_id_not_target_item(self):
        # A key resolves to a numeric pk -> target_item_id.
        path, data = self._call("PLAT-43", resolved=42)
        self.assertEqual(path, "/items/PLAT-46/links/")
        self.assertEqual(data, {"target_item_id": 42, "link_type": "blocks"})
        self.assertNotIn("target_item", data)  # the PLAT-48 bug field

    def test_numeric_target_passes_through_as_target_item_id(self):
        path, data = self._call(42, resolved=42)
        self.assertEqual(data["target_item_id"], 42)
        self.assertNotIn("target_item", data)

    def test_unresolvable_target_falls_back_to_target_item_key(self):
        # _resolve_item_pk returns the input unchanged on lookup failure; the
        # tool must then send it as target_item_key so the backend can link by
        # key (or surface a clear "not found"), never as the bare target_item.
        path, data = self._call("PLAT-999", resolved="PLAT-999")
        self.assertEqual(data, {"target_item_key": "PLAT-999", "link_type": "blocks"})
        self.assertNotIn("target_item", data)

    def test_link_type_is_forwarded(self):
        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=7), \
             mock.patch.object(pulse_server, "api_request", return_value={}) as api:
            pulse_server.link_issues("PLAT-1", "PLAT-2", link_type="duplicates")
        _, kwargs = api.call_args_list[-1]
        self.assertEqual(kwargs["data"]["link_type"], "duplicates")


class UnlinkIssuesCallerSequenceTests(unittest.TestCase):
    """PLS-498: exercise the production MCP discovery-to-DELETE sequence."""

    dependent_links = {
        "outward_links": [{
            "id": "edge-498",
            "link_type": "is_blocked_by",
            "target_item_key": "BLK-498",
        }],
        "inward_links": [],
    }

    def test_dependent_owner_unlinks_from_dependent_typed_direction(self):
        """Readable dependent listing reaches DELETE without a fallback GET."""
        def response(method, path, **kwargs):
            if method == "GET" and path == "/items/DEP-498/links/":
                return self.dependent_links
            if method == "DELETE" and path == "/items/DEP-498/links/edge-498/":
                return {}
            self.fail(f"unexpected production-order call: {method} {path}")

        with mock.patch.object(
            pulse_server, "api_request", side_effect=response,
        ) as api:
            output = pulse_server.unlink_issues(
                "DEP-498", "BLK-498", link_type="is_blocked_by",
            )

        self.assertIn("removed is_blocked_by -> [BLK-498]", output)
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in api.call_args_list],
            [
                ("GET", "/items/DEP-498/links/"),
                ("DELETE", "/items/DEP-498/links/edge-498/"),
            ],
        )

    def test_dependent_owner_unlinks_from_private_blocker_typed_direction(self):
        """A blocker-side 403 falls back to the readable dependent side."""
        def response(method, path, **kwargs):
            if method == "GET" and path == "/items/BLK-498/links/":
                return {"error": "HTTP 403: private blocker is not visible"}
            if method == "GET" and path == "/items/DEP-498/links/":
                return self.dependent_links
            if method == "DELETE" and path == "/items/DEP-498/links/edge-498/":
                return {}
            self.fail(f"unexpected production-order call: {method} {path}")

        with mock.patch.object(
            pulse_server, "api_request", side_effect=response,
        ) as api:
            output = pulse_server.unlink_issues(
                "BLK-498", "DEP-498", link_type="blocks",
            )

        self.assertIn("removed blocks -> [DEP-498]", output)
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in api.call_args_list],
            [
                ("GET", "/items/BLK-498/links/"),
                ("GET", "/items/DEP-498/links/"),
                ("DELETE", "/items/DEP-498/links/edge-498/"),
            ],
        )

    def test_numeric_identifiers_match_link_primary_keys(self):
        """PLS-673: numeric MCP inputs match the IDs returned by link listing."""
        links = {
            "outward_links": [{
                "id": "edge-673-numeric",
                "link_type": "relates_to",
                "source_item": 22672,
                "source_item_key": "PLS-673",
                "target_item": 19395,
                "target_item_key": "SCS-65",
            }],
            "inward_links": [],
        }

        def response(method, path, **kwargs):
            if method == "GET" and path == "/items/22672/links/":
                return links
            if method == "DELETE" and path == "/items/22672/links/edge-673-numeric/":
                return {}
            self.fail(f"unexpected production-order call: {method} {path}")

        with mock.patch.object(
            pulse_server, "api_request", side_effect=response,
        ) as api:
            output = pulse_server.unlink_issues(
                "22672", "19395", link_type="relates_to",
            )

        self.assertIn("removed relates_to -> [19395]", output)
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in api.call_args_list],
            [
                ("GET", "/items/22672/links/"),
                ("DELETE", "/items/22672/links/edge-673-numeric/"),
            ],
        )

    def test_uuid_identifiers_match_link_primary_keys(self):
        """PLS-673: UUID-shaped IDs use the same primary-key alias contract."""
        source_id = "11111111-1111-4111-8111-111111111111"
        target_id = "22222222-2222-4222-8222-222222222222"
        links = {
            "outward_links": [{
                "id": "edge-673-uuid",
                "link_type": "relates_to",
                "source_item": source_id,
                "source_item_key": "PLS-673",
                "target_item": target_id,
                "target_item_key": "SCS-65",
            }],
            "inward_links": [],
        }

        def response(method, path, **kwargs):
            if method == "GET" and path == f"/items/{source_id}/links/":
                return links
            if method == "DELETE" and path == (
                f"/items/{source_id}/links/edge-673-uuid/"
            ):
                return {}
            self.fail(f"unexpected production-order call: {method} {path}")

        with mock.patch.object(
            pulse_server, "api_request", side_effect=response,
        ) as api:
            output = pulse_server.unlink_issues(
                source_id, target_id, link_type="relates_to",
            )

        self.assertIn(f"removed relates_to -> [{target_id.upper()}]", output)
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in api.call_args_list],
            [
                ("GET", f"/items/{source_id}/links/"),
                ("DELETE", f"/items/{source_id}/links/edge-673-uuid/"),
            ],
        )



class ItemPathIdentifierTests(unittest.TestCase):
    def test_item_path_id_strips_numeric_display_key(self):
        self.assertEqual(pulse_server._item_path_id("#6559"), "6559")
        self.assertEqual(pulse_server._item_path("#6559", "activity"), "/items/6559/activity/")

    def test_item_path_id_quotes_non_numeric_identifiers(self):
        self.assertEqual(pulse_server._item_path_id("PLAT-123"), "PLAT-123")
        self.assertEqual(pulse_server._item_path_id("abc/def #1"), "abc%2Fdef%20%231")

    def test_get_task_display_key_uses_normalized_path(self):
        response = {
            "item_key": "#6559",
            "title": "numeric task",
            "status": "todo",
            "priority": "normal",
            "description": "desc",
        }
        with mock.patch.object(pulse_server, "api_request", return_value=response) as api:
            pulse_server.get_task("#6559")
        api.assert_called_once_with("GET", "/items/6559/")

    def test_get_activity_display_key_uses_normalized_path(self):
        with mock.patch.object(pulse_server, "api_request", return_value={"activities": []}) as api:
            pulse_server.get_activity("#6559")
        api.assert_called_once_with("GET", "/items/6559/activity/", params={"limit": 20})

    def test_update_assign_and_transition_paths_use_normalized_display_key(self):
        with mock.patch.object(pulse_server, "api_request", return_value={
            "item_key": "#6559",
            "title": "numeric task",
            "status": "todo",
            "assignee_email": "agent@example.com",
            "transitions": [],
            "current_status": "todo",
        }) as api:
            pulse_server.update_task("#6559", title="new title")
            pulse_server.assign_task("#6559", assignment_group="engineering")
            pulse_server.get_available_transitions("#6559")
            pulse_server.execute_transition("#6559", transition_id="tid")

        paths = [call.args[1] for call in api.call_args_list]
        self.assertIn("/items/6559/", paths)
        self.assertIn("/items/6559/assign/", paths)
        self.assertIn("/items/6559/available-transitions/", paths)
        self.assertIn("/items/6559/execute-transition/", paths)

    def test_link_and_pr_paths_use_normalized_display_key(self):
        def api_response(method, path, **kwargs):
            if path == "/items/6559/":
                return {"metadata": {}, "item_key": "#6559", "title": "numeric task", "status": "todo"}
            return {"item_key": "#6559", "title": "numeric task", "status": "todo"}

        with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=42), \
             mock.patch.object(pulse_server, "api_request", side_effect=api_response) as api, \
             mock.patch.object(pulse_server, "requests") as fake_requests, \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            pulse_server.get_issue_links("#6559")
            pulse_server.link_issues("#6559", "PLS-1")
            # 3953c7b8: a github.com pull request is not merge evidence — link_pr
            # rejects it before any persistence path. The link flow (and its
            # bare-item resolve) only proceeds on the canonical Origin shape.
            fake_requests.get.return_value = _ForgejoPullResp()
            pulse_server.link_pr("#6559", "https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/234")

        paths = [call.args[1] for call in api.call_args_list]
        self.assertIn("/items/6559/links/", paths)
        self.assertIn("/items/6559/", paths)

    def test_workflow_change_verification_uses_normalized_display_key(self):
        with mock.patch.object(pulse_server, "api_request", return_value={
            "workflow_name": "workflow-change",
            "status": "approved",
            "workflow_status": "Approved",
        }) as api:
            self.assertIsNone(pulse_server._verify_change_request("#10384"))
        api.assert_called_once_with("GET", "/items/10384/")

class ResolveItemPkTests(unittest.TestCase):
    def test_display_key_normalizes_before_lookup(self):
        with mock.patch.object(pulse_server, "api_request", return_value={"id": "uuid-6559"}) as api:
            self.assertEqual(pulse_server._resolve_item_pk("#6559"), "uuid-6559")
        api.assert_called_once_with("GET", "/items/6559/")

    def test_numeric_string_uses_backend_resolver_before_integer_fallback(self):
        with mock.patch.object(pulse_server, "api_request", return_value={"id": "uuid-from-key"}) as api:
            self.assertEqual(pulse_server._resolve_item_pk("6559"), "uuid-from-key")
        api.assert_called_once_with("GET", "/items/6559/")

    def test_numeric_string_falls_back_to_integer_when_lookup_fails(self):
        with mock.patch.object(pulse_server, "api_request", return_value={"error": "not found"}):
            self.assertEqual(pulse_server._resolve_item_pk("6559"), 6559)

    def test_uuid_and_project_key_are_url_escaped_for_lookup(self):
        with mock.patch.object(pulse_server, "api_request", return_value={"id": "uuid"}) as api:
            self.assertEqual(pulse_server._resolve_item_pk("PLAT-123"), "uuid")
        api.assert_called_once_with("GET", "/items/PLAT-123/")


class WorkflowMigrationToolResolverTests(unittest.TestCase):
    def test_admin_migrate_canonicalizes_all_task_identifier_shapes(self):
        def resolve(value):
            return {
                "#6559": "uuid-display",
                "6559": "uuid-numeric",
                "PLAT-123": "uuid-project-key",
                "0d3f6b2d-0000-4000-8000-000000000001": "0d3f6b2d-0000-4000-8000-000000000001",
            }[value]

        with mock.patch.object(pulse_server, "_resolve_item_pk", side_effect=resolve) as resolver, \
             mock.patch.object(pulse_server, "api_request", return_value={
                 "dry_run": True,
                 "target_workflow": "Reviewed",
                 "count": 4,
                 "results": [],
             }) as api:
            pulse_server.admin_migrate_task_workflow(
                target_workflow="reviewed-task",
                task_keys=[
                    "#6559",
                    "6559",
                    "PLAT-123",
                    "0d3f6b2d-0000-4000-8000-000000000001",
                ],
            )

        resolver.assert_has_calls([
            mock.call("#6559"),
            mock.call("6559"),
            mock.call("PLAT-123"),
            mock.call("0d3f6b2d-0000-4000-8000-000000000001"),
        ])
        api.assert_called_once()
        args, kwargs = api.call_args
        self.assertEqual(args[:2], ("POST", "/items/migrate-workflow/"))
        self.assertEqual(kwargs["data"]["task_keys"], [
            "uuid-display",
            "uuid-numeric",
            "uuid-project-key",
            "0d3f6b2d-0000-4000-8000-000000000001",
        ])


if __name__ == "__main__":
    unittest.main()

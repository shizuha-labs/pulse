"""
PLAT-1355 — MCP tool payload <-> backend serializer contract tests.

WHY THIS EXISTS
    Recurring MCP-tool/serializer drift silently broke writes across services
    (ID-38, PLS-224, PLAT-1171 class; locally AT-84 and PLAT-48 in Pulse): an
    exposed MCP tool built a POST payload whose keys diverged from the fields
    the backend DRF serializer actually accepts/requires, so the write failed
    (or silently dropped a field) only at runtime, in production.

    - AT-84: `pulse_log_work` sent `time_spent_minutes` / `work_date`, but
      `WorkLogCreateSerializer` accepts `duration_minutes` / `worked_date`.
    - PLAT-48: `pulse_link_issues` sent a bare `target_item`, but
      `ItemLinkCreateSerializer` accepts `target_item_id` / `target_item_key`.

WHAT THIS GUARANTEES
    For every registered tool below, the ACTUAL payload the tool POSTs (captured
    with `api_request` mocked, exactly as it would hit the backend) is checked
    against the target serializer's introspected field contract:

      1. No UNKNOWN keys      — every payload key is a writable serializer field
                                (catches PLAT-48: an extra/renamed field the
                                 serializer will reject or silently ignore).
      2. No MISSING required  — every serializer-required field is present in the
                                payload, honouring declared either/or groups
                                (catches AT-84: a required field sent under the
                                 wrong name).

    The serializer is the single source of truth; the test introspects it live,
    so it cannot go stale the way a hand-maintained field list would.

HOW TO ADD A NEW MCP TOOL (opt-in)
    Append one entry to CONTRACT_CASES:

        {
            "tool": "pulse_server function name (e.g. 'log_work')",
            "kwargs": {representative VALID arguments for the tool},
            "endpoint_suffix": "the POST path the tool hits (e.g. '/work-logs/')",
            "serializer": TheCreateSerializerClass,
            # optional — sets of fields where >=1 must be present (serializer
            # enforces the choice in validate()), e.g. duration_minutes/hours:
            "either_or_groups": [{"target_item_id", "target_item_key"}],
        }

    That is the entire opt-in. CI (manage.py test) then fails on any future
    drift for that tool before deployment. Keep `kwargs` to a minimal valid
    call; the test only inspects the outgoing payload shape, never the DB.
"""
import os
import sys

from django.test import SimpleTestCase

# The MCP server lives beside the Django project (repo_root/mcp-server); it is a
# standalone package, import-safe (its `mcp.run()` is guarded by __main__).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MCP_DIR = os.path.join(_REPO_ROOT, "mcp-server")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)
# pulse_server reads the backend base URL at import; value is irrelevant here
# because every api_request is mocked.
os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

from unittest import mock  # noqa: E402

import pulse_server  # noqa: E402

from tasks.serializers import (  # noqa: E402
    CommentCreateSerializer,
    ItemCreateSerializer,
    ItemLinkCreateSerializer,
    SavedFilterCreateSerializer,
    SavedFilterUpdateSerializer,
    WorkLogCreateSerializer,
)


def serializer_contract(serializer_cls):
    """Return (writable_fields, required_fields) introspected from a serializer.

    writable = every non-read-only field the serializer accepts on input.
    required = every writable field DRF marks required (no default, required=True).
    Either/or requirements (enforced only in the serializer's validate()) are
    handled per-case via `either_or_groups`, not here.
    """
    serializer = serializer_cls()
    writable, required = set(), set()
    for name, field in serializer.fields.items():
        if field.read_only:
            continue
        writable.add(name)
        if field.required:
            required.add(name)
    return writable, required


def capture_tool_payload(tool_name, kwargs, endpoint_suffix, method="POST"):
    """Invoke the MCP tool with api_request/_resolve_item_pk mocked and return
    the data dict it sent to the endpoint whose path ends with endpoint_suffix.
    """
    tool = getattr(pulse_server, tool_name)
    with mock.patch.object(pulse_server, "_resolve_item_pk", return_value=42), \
         mock.patch.object(pulse_server, "api_request",
                           return_value={"id": 1, "item_key": "PLAT-1"}) as api:
        tool(**kwargs)
    for call in api.call_args_list:
        args, kw = call
        if args and args[0] == method and str(args[1]).endswith(endpoint_suffix):
            return str(args[1]), dict(kw.get("data") or {})
    return None, None


# ── Contract registry: add a dict here to opt a new MCP write tool into CI. ──
CONTRACT_CASES = [
    {
        "tool": "log_work",
        "kwargs": dict(task_id="AT-84", time_spent_minutes=45,
                       description="contract probe", work_date="2026-06-04"),
        "endpoint_suffix": "/work-logs/",
        "serializer": WorkLogCreateSerializer,
        # duration_minutes OR duration_hours satisfies the "duration" requirement.
        "either_or_groups": [{"duration_minutes", "duration_hours"}],
    },
    {
        "tool": "link_issues",
        "kwargs": dict(task_id="PLAT-46", target_task_id="PLAT-43", link_type="blocks"),
        "endpoint_suffix": "/links/",
        "serializer": ItemLinkCreateSerializer,
        "either_or_groups": [{"target_item_id", "target_item_key"}],
    },
    {
        "tool": "add_comment",
        "kwargs": dict(task_id="AT-1", content="contract probe"),
        "endpoint_suffix": "/comments/",
        "serializer": CommentCreateSerializer,
    },
    {
        "tool": "create_task",
        "kwargs": dict(title="contract probe", description="d",
                       priority="normal", project_id=7),
        "endpoint_suffix": "/items/",
        "serializer": ItemCreateSerializer,
    },
    {
        "tool": "create_saved_filter",
        "kwargs": dict(name="contract probe", jql='status = "pending"',
                       project_id=7),
        "endpoint_suffix": "/saved-filters/",
        "serializer": SavedFilterCreateSerializer,
    },
    {
        "tool": "update_saved_filter",
        "kwargs": dict(filter_id="abc-123", name="renamed",
                       jql='priority = "high"'),
        "endpoint_suffix": "/saved-filters/abc-123/",
        "serializer": SavedFilterUpdateSerializer,
        "method": "PATCH",
    },
]


class MCPToolSerializerContractTests(SimpleTestCase):
    """Pin every registered MCP write tool's payload to its serializer contract.

    No database is touched: serializer field introspection and the mocked
    api_request capture are pure. Runs under `manage.py test` in CI.
    """

    def test_registered_tools_match_serializer_contract(self):
        for case in CONTRACT_CASES:
            with self.subTest(tool=case["tool"], serializer=case["serializer"].__name__):
                writable, required = serializer_contract(case["serializer"])
                endpoint, payload = capture_tool_payload(
                    case["tool"], case["kwargs"], case["endpoint_suffix"],
                    method=case.get("method", "POST"),
                )
                self.assertIsNotNone(
                    payload,
                    f"{case['tool']} did not POST to '*{case['endpoint_suffix']}' — "
                    f"tool/endpoint drift (check the tool's api_request call).",
                )
                keys = set(payload.keys())

                # (1) No unknown keys — the serializer would reject/ignore them.
                unknown = keys - writable
                self.assertFalse(
                    unknown,
                    f"{case['tool']} -> {case['serializer'].__name__}: payload has "
                    f"field(s) the serializer does not accept: {sorted(unknown)}. "
                    f"Serializer writable fields: {sorted(writable)}.",
                )

                # (2) No missing required — honour either/or groups.
                either_or_groups = case.get("either_or_groups", [])
                required_singletons = set(required)
                for group in either_or_groups:
                    required_singletons -= group  # satisfied by the group check
                    self.assertTrue(
                        keys & group,
                        f"{case['tool']} -> {case['serializer'].__name__}: payload "
                        f"satisfies none of the required either/or group {sorted(group)}.",
                    )
                    # A group only matters if the serializer actually accepts it.
                    self.assertTrue(
                        group <= writable,
                        f"either_or_group {sorted(group)} is not writable on "
                        f"{case['serializer'].__name__} — registry entry is stale.",
                    )
                missing = required_singletons - keys
                self.assertFalse(
                    missing,
                    f"{case['tool']} -> {case['serializer'].__name__}: payload is "
                    f"missing required serializer field(s): {sorted(missing)}.",
                )

    def test_historical_regressions_stay_fixed(self):
        """Pin the exact field names from the two Pulse drift incidents."""
        # AT-84: work-log payload must use serializer names, never the tool arg names.
        _, worklog = capture_tool_payload(
            "log_work",
            dict(task_id="AT-84", time_spent_minutes=45, work_date="2026-06-04"),
            "/work-logs/",
        )
        self.assertIn("duration_minutes", worklog)
        self.assertIn("worked_date", worklog)
        self.assertNotIn("time_spent_minutes", worklog)
        self.assertNotIn("work_date", worklog)

        # PLAT-48: link payload must never send the bare `target_item`.
        _, link = capture_tool_payload(
            "link_issues",
            dict(task_id="PLAT-46", target_task_id="PLAT-43", link_type="blocks"),
            "/links/",
        )
        self.assertNotIn("target_item", link)
        self.assertTrue({"target_item_id", "target_item_key"} & set(link))

    def test_list_tasks_preserves_native_array_and_object_label_filters(self):
        """PLS-712: FastMCP-decoded JSON reaches the backend as JSON text."""
        for labels, expected in (
            (['repo:shizuha-labs/drive'], '["repo:shizuha-labs/drive"]'),
            ({'repo': 'shizuha-labs/drive'}, '{"repo":"shizuha-labs/drive"}'),
        ):
            with self.subTest(labels=labels), mock.patch.object(
                pulse_server,
                'api_request',
                return_value={'results': [], 'count': 0, 'next': None},
            ) as api:
                pulse_server.list_tasks(status='deferred', labels=labels)

            self.assertEqual(api.call_args.kwargs['params']['labels'], expected)

"""PLS-840 / Standard v21 rule 14 — `pulse_link_pr("no-pr")` ingress contract.

The sentinel is a TASK-LEVEL no-code declaration, not a provider PR tuple. v21
requires ingress to:

  * set the canonical task-level `no-pr` flag,
  * append NO ``metadata.pull_requests`` row,
  * make NO provider request,
  * preserve existing rows (including a legacy sentinel audit row),
  * keep genuine PR obligations independent,
  * and be idempotent.

Before this, every call appended another row whose url was ``no-pr`` — a row
with no provider, no head and no possible advancement, which the re-entry gate
then evaluated as an unsatisfiable open obligation. Fixing only the reader would
have left this writer recreating the wedge on the next call.

Each permissive assertion below has a fail-closed twin: recognition WAIVES a
code-PR obligation, so anything that merely resembles the sentinel must fall
through to normal parsing.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class NoPrDeclarationIngressTests(unittest.TestCase):
    def _call(self, pr_url, item=None):
        """Invoke link_pr with the API mocked; return (result, calls, requests_used)."""
        item = item if item is not None else {"labels": [], "metadata": {}}
        calls = []

        def fake_api(method, path, data=None, **_kw):
            calls.append((method, path, data))
            return item if method == "GET" else {"ok": True}

        with mock.patch.object(pulse_server, "api_request", side_effect=fake_api), \
             mock.patch.object(pulse_server, "requests") as req:
            result = pulse_server.link_pr("PLAT-5519", pr_url)
        return result, calls, req

    # ---- the declaration path --------------------------------------------

    def test_sentinel_sets_the_flag_and_writes_no_pr_row(self):
        _result, calls, _req = self._call("no-pr")
        patches = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1, f"expected exactly one PATCH, got {calls!r}")
        payload = patches[0][2] or {}
        self.assertIn("no-pr", payload.get("labels") or [])
        self.assertNotIn(
            "metadata", payload,
            msg="the declaration must not write metadata.pull_requests",
        )

    def test_sentinel_makes_no_provider_request(self):
        """No provider exists to ask — a request would be a wedge, not a lookup."""
        _result, _calls, req = self._call("no-pr")
        req.get.assert_not_called()
        req.post.assert_not_called()

    def test_declaration_is_idempotent(self):
        item = {"labels": ["no-pr"], "metadata": {}}
        _result, calls, _req = self._call("no-pr", item=item)
        self.assertEqual(
            [c for c in calls if c[0] == "PATCH"], [],
            msg="a second call must be a no-op, not a duplicate write",
        )

    def test_existing_audit_rows_are_preserved(self):
        """A legacy sentinel row stays; v21 keeps it as immutable audit history."""
        legacy = {"url": "no-pr", "status": "open"}
        genuine = {
            "url": "https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/900",
            "repo": "shizuha-labs/shizuha-tasks", "number": 900,
            "status": "open", "head_sha": "abc",
        }
        item = {"labels": [], "metadata": {"pull_requests": [legacy, genuine]}}
        _result, calls, _req = self._call("no-pr", item=item)
        for _m, _p, data in calls:
            if data and "metadata" in data:
                self.fail(f"declaration must not rewrite metadata: {data!r}")

    def test_sentinel_case_and_whitespace_normalised(self):
        for variant in ("  no-pr  ", "NO-PR", "No-Pr"):
            with self.subTest(variant=variant):
                _result, calls, req = self._call(variant)
                req.get.assert_not_called()
                patches = [c for c in calls if c[0] == "PATCH"]
                self.assertEqual(len(patches), 1)
                self.assertIn("no-pr", (patches[0][2] or {}).get("labels") or [])

    # ---- the fail-closed twin --------------------------------------------

    def test_lookalikes_do_not_take_the_declaration_path(self):
        """Anything that merely resembles the sentinel is an ordinary URL.

        Without this, the branch that waives a code-PR obligation could be
        reached by a hostile or mistyped value.
        """
        for hostile in (
            "no-pr-really",
            "https://example.test/no-pr",
            "no_pr",
            "nopr",
            "no‑pr",          # U+2011 non-breaking hyphen
            "no-pr/../../etc",
            "no-pr?x=1",
        ):
            with self.subTest(hostile=hostile):
                _result, calls, _req = self._call(hostile)
                patches = [c for c in calls if c[0] == "PATCH"]
                labels_written = [
                    (c[2] or {}).get("labels") for c in patches if (c[2] or {}).get("labels")
                ]
                self.assertEqual(
                    labels_written, [],
                    msg=f"{hostile!r} must not be treated as the declaration",
                )



class ToolRegistrationTests(unittest.TestCase):
    """The decorator must stay attached to the tool it names (rei P1#1).

    Inserting `_declare_no_pr` immediately above `def link_pr` landed it BETWEEN
    `@mcp.tool(name="pulse_link_pr")` and its function, so the registered tool
    became the one-argument helper and the real `link_pr` was not registered at
    all — every `pulse_link_pr(item, url)` call would have failed.

    Every behavioural test above passed throughout, because they call
    `pulse_server.link_pr(...)` directly as a Python function and never traverse
    registration. A behavioural test structurally cannot see this defect; only an
    assertion about the decorator→function binding can, which is why this reads
    the AST rather than calling anything.
    """

    def _registered(self):
        import ast
        import pathlib
        src = pathlib.Path(pulse_server.__file__).read_text(encoding="utf-8")
        mapping = {}
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "attr", None) == "tool":
                    name = node.name
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            name = kw.value.value
                    mapping[name] = node.name
        return mapping

    def test_pulse_link_pr_is_bound_to_link_pr(self):
        self.assertEqual(self._registered().get("pulse_link_pr"), "link_pr")

    def test_the_no_pr_helper_is_not_a_registered_tool(self):
        """It is an internal helper; registering it would shadow a real tool."""
        self.assertNotIn("_declare_no_pr", self._registered().values())

    def test_every_registered_tool_binds_to_a_public_function(self):
        """Generalises the defect: a private helper can only become a tool by
        having stolen a decorator from the function below it."""
        stolen = {
            tool: fn for tool, fn in self._registered().items() if fn.startswith("_")
        }
        self.assertEqual(stolen, {}, msg=f"private functions registered as tools: {stolen}")

if __name__ == "__main__":
    unittest.main()

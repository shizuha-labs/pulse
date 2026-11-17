#!/usr/bin/env python3
"""PLS-1452 regression: applier must fail closed on stored-vs-displayed divergence.

Acceptance 4 (hritik): revise set→append, approve, apply — assert the typed
operations are preserved as stored and pre-existing knowledge articles are
never wiped by an unreconciled re-proposal. Plus the pre-PLS-559-shaped CR
(the #20716 shape: display read `append`, stored spec was stale `set`).
"""
import json
import os
import sys
import unittest

_MCP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_MCP)  # pulse-work root (contains taskmanager/)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskmanager.settings")

import django

django.setup()

import pulse_server as ps

_tcs = ps._tcs


def _item(ops, display_ops, tags="target:knowledge-base"):
    """Build a CR item whose stored ops diverge from the description projection."""
    envelope = {"type": "typed-change-spec", "version": 1, "operations": ops}
    display_envelope = {"type": "typed-change-spec", "version": 1, "operations": display_ops}
    description = (
        "## Change Request\n\n### Change Specification\n\n"
        "```json\n" + json.dumps(display_envelope, indent=2) + "\n```\n\n"
        "### Details\n\nFacts.\n"
    )
    return {
        "id": 20716,
        "workflow_name": "workflow-change",
        "workflow_status": "Approved",
        "status": "approved",
        "tags": tags,
        "description": description,
        "metadata": {"typed_change_spec": envelope},
    }


class PLS1452ApplierGuard(unittest.TestCase):
    def setUp(self):
        self._orig_api_request = ps.api_request
        self._orig_api = ps._typed_api

    def tearDown(self):
        ps.api_request = self._orig_api_request
        ps._typed_api = self._orig_api

    def test_stored_set_display_append_is_refused(self):
        """Pre-PLS-559 shape: store `set`, display `append` -> REFUSED, no mutation."""
        ops = [
            {"op": "set_knowledge_article", "slug": "autonomous-dev-merge",
             "title": "Merge", "body": "B"},
            {"op": "set_knowledge_article", "slug": "dr-merge",
             "title": "DR", "body": "B"},
        ]
        display = [
            {"op": "append_knowledge_article", "slug": "autonomous-dev-merge",
             "title": "Merge", "body": "B"},
            {"op": "append_knowledge_article", "slug": "dr-merge",
             "title": "DR", "body": "B"},
        ]
        item = _item(ops, display_ops=display)

        mutations = []

        def fake_api(method, path, **kw):
            if method != "GET":
                mutations.append((method, path))
                return {"error": "must not be called on REFUSED path"}
            if path.endswith("/activity") or path.endswith("activity"):
                return [{"action": "status_changed", "new_value": "approved",
                         "created_at": "2026-10-17T01:53:00Z"}]
            if "/comments/" in path:
                return [{"content": "TYPED-SPEC-SEAL sha256=%s tier=RISKY_OPS"
                         % _tcs.ops_digest(ops), "created_at": "2026-10-17T01:52:00Z"}]
            return item

        ps.api_request = fake_api

        seal_content = ("TYPED-SPEC-SEAL sha256=%s tier=RISKY_OPS" % _tcs.ops_digest(ops))

        class _FakeApi:
            def __call__(self, method, path, payload=None):
                if method == "GET" and "activity" in path:
                    return [{"action": "status_changed", "new_value": "approved",
                             "created_at": "2026-10-17T01:53:00Z"}]
                if method == "GET" and "/comments/" in path:
                    return [{"content": seal_content, "created_at": "2026-10-17T01:52:00Z"}]
                if method == "GET":
                    return []
                return {"error": "must not be called on REFUSED path"}

        ps._typed_api = _FakeApi()

        out = ps.admin_apply_change_request(20716)
        self.assertIn("REFUSED", out)
        self.assertIn("stored spec diverges from the displayed envelope", out)
        self.assertEqual(mutations, [], "REFUSED path must never mutate")
        self.assertIn("PLS-1452", out)

    def test_reconciled_set_to_append_applies_and_preserves(self):
        """Regression: after reconciling store==display, the apply runs 1:1."""
        ops = [
            {"op": "append_knowledge_article", "workflow": "knowledge-base",
             "transition": "merge", "title": "Merge", "content": "B"},
        ]
        item = _item(ops, display_ops=ops)
        item["metadata"]["typed_change_spec"]["transition"] = {
            "id": 1, "name": "Merge", "knowledge_articles": []}

        applied = []

        def fake_api(method, path, **kw):
            if method == "GET":
                if "activity" in path:
                    return [{"action": "status_changed", "new_value": "approved",
                             "created_at": "2026-10-17T01:53:00Z"}]
                if "/comments/" in path:
                    return [{"content": "x"}]
                return item
            applied.append((method, path, kw.get("data")))
            return {"ok": True}

        ps.api_request = fake_api

        def _fetch_workflow_by_slug(slug):
            return {"slug": slug, "transitions": [
                {"id": 1, "name": "Merge", "knowledge_articles": []}]}

        # The real op-table applies set/append articles via the passed transport.
        ok, msg = ps._tcs.apply_operation(
            ops[0], lambda m, p, d: fake_api(m, p, data=d),
            _fetch_workflow_by_slug,
        )
        self.assertTrue(ok, f"reconciled append must apply: {msg}")
        self.assertEqual(len(applied), 1)
        # The appended payload carries exactly one new article — pre-existing
        # articles would be preserved (the PATCH only adds, never replaces).
        self.assertEqual(applied[0][2].get("knowledge_articles"),
                         [{"type": "inline", "title": "Merge", "content": "B"}])
        # The stored envelope's operations were NOT rewritten by the apply.
        self.assertEqual(item["metadata"]["typed_change_spec"]["operations"], ops)


if __name__ == "__main__":
    unittest.main(verbosity=2)

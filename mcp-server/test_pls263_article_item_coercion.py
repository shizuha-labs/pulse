"""
Regression tests for PLS-263 (PLS-194 / PLS-224 recurrence class).

Symptom: pulse_get_available_transitions AND pulse_execute_transition returned a
hard ``'str' object has no attribute 'get'`` for a task whose workflow held a
malformed ``knowledge_articles`` row — a LIST whose *elements* were strings
(a JSON-encoded article string, or bare text). PLS-224 hardened the OUTER
container coercion in ``_safe_json_list`` but left the ITEMS un-coerced, so a
string element still reached a render loop's ``item.get(...)`` and crashed.

Contract: the READ boundary (``_safe_json_list``) must coerce items too —
string items are parsed once, non-dict items dropped — so a single malformed
article never crashes transition listing/execution. NEVER raise on read.
"""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server


class SafeJsonListItemCoercion(unittest.TestCase):
    """The PLS-263 gap: outer value is a list, but its ITEMS are strings."""

    def test_json_encoded_string_item_is_parsed(self):
        val = ['{"type": "inline", "title": "Read me", "content": "do X"}']
        self.assertEqual(
            pulse_server._safe_json_list(val),
            [{"type": "inline", "title": "Read me", "content": "do X"}],
        )

    def test_bare_text_item_is_dropped_not_returned_as_str(self):
        # A bare-text element cannot be a dict — it must be dropped so a render
        # loop never calls .get() on it.
        self.assertEqual(pulse_server._safe_json_list(["just some text"]), [])

    def test_mixed_items_only_dicts_survive(self):
        val = ["bare", {"type": "inline", "title": "Q"}, 42, None]
        self.assertEqual(pulse_server._safe_json_list(val), [{"type": "inline", "title": "Q"}])

    def test_raw_string_list_of_string_items(self):
        # The whole field is a raw JSON string that decodes to a list of
        # JSON-encoded article strings (double corruption).
        raw = json.dumps(['{"type": "inline", "title": "Z"}'])
        self.assertEqual(pulse_server._safe_json_list(raw), [{"type": "inline", "title": "Z"}])

    def test_native_dict_list_passthrough(self):
        val = [{"type": "inline", "title": "A"}, {"type": "link", "url": "u"}]
        self.assertEqual(pulse_server._safe_json_list(val), val)

    def test_str_item_type_keeps_strings(self):
        self.assertEqual(pulse_server._safe_json_list(["a", 1, "b"], item_type="str"), ["a", "b"])

    def test_never_raises_on_scalar_or_none(self):
        self.assertEqual(pulse_server._safe_json_list(None), [])
        self.assertEqual(pulse_server._safe_json_list(42), [])
        self.assertEqual(pulse_server._safe_json_list("not json"), [])


class RenderPathsNeverCrashOnStringItems(unittest.TestCase):
    """End-to-end: both MCP transition tools must render (not crash) when a
    transition's knowledge_articles holds string items."""

    MALFORMED = ['{"type": "inline", "title": "Read me", "content": "do X"}']

    def test_get_available_transitions_renders_article(self):
        resp = {
            "current_status": "in_review",
            "transitions": [{
                "name": "Merge", "to_status": "merged",
                "knowledge_articles": self.MALFORMED,
            }],
        }
        with mock.patch.object(pulse_server, "api_request", return_value=resp):
            out = pulse_server.get_available_transitions("PLAT-1610")
        self.assertIn("Read me", out)
        self.assertIn("Merge", out)

    def test_execute_transition_renders_executed_and_next_articles(self):
        resp = {
            "status": "in_review", "title": "t",
            "executed_transition": {"name": "X", "knowledge_articles": self.MALFORMED},
            "next_transitions": [{
                "name": "Y", "to_status": "z", "knowledge_articles": self.MALFORMED,
            }],
        }
        with mock.patch.object(pulse_server, "api_request", return_value=resp):
            out = pulse_server.execute_transition("PLAT-1610", to_status="in_review")
        self.assertIn("Transitioned", out)
        self.assertIn("Read me", out)
        self.assertIn("Author handoff complete — END THIS TURN", out)
        self.assertIn("Do not sleep or poll", out)

    def test_non_review_transition_does_not_render_author_handoff(self):
        with mock.patch.object(pulse_server, "api_request", return_value={
            "status": "in_progress",
            "title": "t",
            "next_transitions": [],
        }):
            out = pulse_server.execute_transition("PLAT-1610", to_status="in_progress")
        self.assertNotIn("Author handoff complete", out)

    def test_workflow_detail_counts_real_articles_not_string_items(self):
        # Two string-encoded articles + one bare-text (dropped) -> count is 2.
        malformed = [
            '{"type": "inline", "title": "One"}',
            '{"type": "inline", "title": "Two"}',
            "garbage",
        ]
        workflow = {
            "name": "wf", "slug": "wf", "statuses": [],
            "transitions": [{
                "name": "Merge", "from_status": "a", "to_status": "b",
                "knowledge_articles": malformed,
            }],
        }
        with mock.patch.object(pulse_server, "api_request", return_value=workflow):
            out = pulse_server.get_workflow_detail("wf-1")
        self.assertIn("2 knowledge article(s)", out)


if __name__ == "__main__":
    unittest.main()

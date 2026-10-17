"""
Regression tests for PLS-224 (per the aoi contract ruling).

Canonical representation: the transition list fields are native JSON arrays
everywhere. A raw JSON string is never a valid stored value.
- WRITE boundary (_coerce_json_list): accept native list or JSON-array string,
  parse ONCE, require a list; REJECT double-encoded strings and scalars.
- READ boundary (_safe_json_list): defensively coerce a possibly-raw-string
  stored value to a list for render/count; NEVER raise, NEVER a char-count
  (the "762 knowledge article(s)" symptom from #10384).
"""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server


class CoerceJsonListWriteBoundary(unittest.TestCase):
    def test_native_list_passthrough(self):
        self.assertEqual(pulse_server._coerce_json_list("f", [{"a": 1}]), ([{"a": 1}], None))

    def test_json_array_string(self):
        self.assertEqual(pulse_server._coerce_json_list("f", '[{"a": 1}]'), ([{"a": 1}], None))

    def test_double_encoded_string_rejected(self):
        # A double-encoded string parses to a str, not a list -> REJECT on write.
        val, err = pulse_server._coerce_json_list("knowledge_articles", '"[{\\"a\\": 1}]"')
        self.assertIsNone(val)
        self.assertIn("knowledge_articles", err)

    def test_bare_string_rejected(self):
        val, err = pulse_server._coerce_json_list("knowledge_articles", "just some text")
        self.assertIsNone(val)
        self.assertIn("knowledge_articles", err)

    def test_non_list_json_rejected(self):
        for bad in ('{"a": 1}', '42', 'true'):
            val, err = pulse_server._coerce_json_list("f", bad)
            self.assertIsNone(val, bad)
            self.assertTrue(err)

    def test_empty_list(self):
        self.assertEqual(pulse_server._coerce_json_list("f", []), ([], None))


class UpdateTransitionInvocationShape(unittest.TestCase):
    def _patch_data(self, **kwargs):
        with mock.patch.object(pulse_server, "_verify_change_request", return_value=None), \
             mock.patch.object(pulse_server, "api_request", return_value={"ok": True}) as api:
            result = pulse_server.admin_update_transition("T-1", 42, **kwargs)
        if not api.call_args_list:
            return None, result
        args, kw = api.call_args
        return kw.get("data"), result

    def test_native_list_stored_as_list(self):
        data, _ = self._patch_data(knowledge_articles=[{"type": "inline", "title": "x"}])
        self.assertEqual(data["knowledge_articles"], [{"type": "inline", "title": "x"}])

    def test_json_array_string_stored_as_list(self):
        data, _ = self._patch_data(knowledge_articles='[{"type": "inline", "title": "x"}]')
        self.assertEqual(data["knowledge_articles"], [{"type": "inline", "title": "x"}])
        self.assertIsInstance(data["knowledge_articles"], list)

    def test_double_encoded_rejected_no_patch(self):
        data, result = self._patch_data(knowledge_articles='"[{\\"title\\": \\"x\\"}]"')
        self.assertIsNone(data)
        self.assertIn("knowledge_articles", result)

    def test_bare_string_rejected_no_patch(self):
        data, result = self._patch_data(knowledge_articles="not json at all")
        self.assertIsNone(data)
        self.assertIn("knowledge_articles", result)

    def test_all_list_fields_coerced(self):
        data, _ = self._patch_data(
            post_functions=[{"type": "assign_team", "value": "review"}],
            knowledge_articles='[{"type": "inline"}]',
            screen_fields='["priority"]',
            allowed_roles='["admin"]',
        )
        self.assertEqual(data["post_functions"], [{"type": "assign_team", "value": "review"}])
        self.assertEqual(data["knowledge_articles"], [{"type": "inline"}])
        self.assertEqual(data["screen_fields"], ["priority"])
        self.assertEqual(data["allowed_roles"], ["admin"])


class SafeJsonListReadBoundary(unittest.TestCase):
    """aoi ruling item #1 — read/render must coerce defensively so a legacy
    raw-string field reports its real count, never a character count."""

    def test_native_list(self):
        self.assertEqual(pulse_server._safe_json_list([{"a": 1}]), [{"a": 1}])

    def test_raw_json_array_string(self):
        self.assertEqual(pulse_server._safe_json_list('[{"a": 1}, {"b": 2}]'), [{"a": 1}, {"b": 2}])

    def test_double_encoded_legacy_string(self):
        self.assertEqual(pulse_server._safe_json_list('"[{\\"a\\": 1}]"'), [{"a": 1}])

    def test_garbage_string_returns_empty(self):
        self.assertEqual(pulse_server._safe_json_list("not json"), [])

    def test_none_returns_empty(self):
        self.assertEqual(pulse_server._safe_json_list(None), [])

    def test_never_raises_on_scalar(self):
        self.assertEqual(pulse_server._safe_json_list(42), [])


class WorkflowDetailRenderCount(unittest.TestCase):
    """The live #10384 symptom: get_workflow_detail counted a raw-string
    knowledge_articles field character-by-character ('762 knowledge article(s)').
    A raw-string field must render its real (small) count."""

    def test_raw_string_field_renders_article_count_not_char_count(self):
        one_article = [{"type": "inline", "title": "Q", "content": "x" * 400}]
        raw = json.dumps(one_article)  # a STRING, as a corrupted JSONField holds
        workflow = {
            "name": "wf", "slug": "wf", "statuses": [],
            "transitions": [{
                "name": "Merge", "from_status": "a", "to_status": "b",
                "knowledge_articles": raw, "post_functions": raw,
            }],
        }
        with mock.patch.object(pulse_server, "api_request", return_value=workflow):
            out = pulse_server.get_workflow_detail("wf-1")
        self.assertIn("1 knowledge article(s)", out)
        self.assertIn("1 post-functions", out)
        self.assertNotIn("400 knowledge article(s)", out)  # the char-count bug


if __name__ == "__main__":
    unittest.main()

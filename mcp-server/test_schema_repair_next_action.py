"""SCHEMA REPAIR listings must name the next action so agents stop re-fetching."""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


class SchemaRepairNextActionTests(unittest.TestCase):
    def test_fmt_items_banner_on_schema_repair_title(self):
        out = pulse_server._fmt_items([
            {
                "item_key": "HIVE-1953",
                "id": 1953,
                "title": "[SCHEMA REPAIR: missing_evidence_schema] [BLOCKER] HIVE-1952",
                "status": "todo",
                "priority": "urgent",
            },
        ])
        self.assertIn("HIVE-1953", out)
        self.assertIn("Do not pulse_get_task this key again", out)
        self.assertIn("pulse_transition_task", out)

    def test_fmt_items_skips_banner_for_ordinary_tasks(self):
        out = pulse_server._fmt_items([
            {
                "item_key": "PLS-986",
                "id": 986,
                "title": "Security exact-version verdict",
                "status": "todo",
                "priority": "urgent",
            },
        ])
        self.assertIn("PLS-986", out)
        self.assertNotIn("Do not pulse_get_task this key again", out)

    def test_helper_accepts_metadata_marker(self):
        item = {
            "title": "[BLOCKER] HIVE-1952",
            "metadata": {"escalation_schema_repair": "v2"},
        }
        self.assertTrue(pulse_server._is_schema_repair_item(item))
        self.assertIn("pulse_transition_task", pulse_server._schema_repair_next_action_line(item))


if __name__ == "__main__":
    unittest.main()

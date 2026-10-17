"""
Regression tests for PLS-266: pulse_list_tasks / pulse_search_tasks / pulse_get_my_tasks
(all route through _fmt_items) must surface the numeric item primary key (PK) so
callers can use it directly as parent_item_id / the `item` write field without a
separate pulse_get_task round-trip.
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class FmtItemsPkTests(unittest.TestCase):
    def test_numeric_pk_surfaced_alongside_key(self):
        out = pulse_server._fmt_items([
            {"item_key": "PLAT-123", "id": 12345, "title": "Do the thing",
             "status": "open", "priority": "high"},
        ])
        self.assertIn("**PLAT-123**", out)
        self.assertIn("ID: 12345", out)
        # PK line sits with the item, before its Status line.
        self.assertLess(out.index("ID: 12345"), out.index("Status:"))

    def test_no_redundant_id_line_when_key_is_the_pk(self):
        # No item_key → the key line already shows #<pk>; don't duplicate it.
        out = pulse_server._fmt_items([
            {"id": 999, "title": "keyless", "status": "open", "priority": "low"},
        ])
        self.assertIn("**#999**", out)
        self.assertNotIn("ID: 999", out)

    def test_missing_pk_does_not_emit_id_line_or_crash(self):
        out = pulse_server._fmt_items([
            {"item_key": "PLAT-7", "title": "no pk", "status": "open", "priority": "normal"},
        ])
        self.assertIn("**PLAT-7**", out)
        self.assertNotIn("ID:", out)

    def test_each_item_gets_its_own_pk(self):
        out = pulse_server._fmt_items([
            {"item_key": "A-1", "id": 1, "title": "one", "status": "open", "priority": "normal"},
            {"item_key": "B-2", "id": 2, "title": "two", "status": "open", "priority": "normal"},
        ])
        self.assertIn("ID: 1", out)
        self.assertIn("ID: 2", out)


if __name__ == "__main__":
    unittest.main()

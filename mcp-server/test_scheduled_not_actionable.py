"""
ADR-0073 / PLS-862 regression tests: `scheduled` is WAITING, never terminal.

History: PLS-70 added 'scheduled' to TERMINAL_STATUSES so parked recurring
tasks would not return as actionable — but the right mechanism (and the one
that game today) was ALWAYS the backend WAITING_QUEUE_STATUS_SLUGS +
filter_active_queue_items parking scheduled rows for is_active=true, plus the
MCP's not-yet-due/future-recurring suppression. Folding `scheduled` into the
TERMINAL set was the wrong lever: it makes a recurring compliance item vanish
from BOTH the visible rows AND every count on the unfiltered assignee query
pulse-core prescribes — "no signal the item exists".

ADR-0073 (accepted 2026-08-03) resolves the contract: terminal is an explicit
allow-list (backend TERMINAL_STATUS_SLUGS, mirrored exactly by the MCP);
`scheduled` is a parked/waiting non-terminal status that renders as "waiting"
and, on the default-active path, folds into the hidden_scheduled bucket so the
accounting invariant (shown + hidden_non_terminal + hidden_terminal == assigned
scope) holds.

Polarity fixtures required by ADR §6:
- A: an in_progress + a scheduled item → counts reconcile (scheduled is hidden
  non-terminal, NOT terminal).
- B: a genuinely terminal item (done) is still excluded AND counted terminal.
Rebuilt 2026-08-XX under PLS-862 (was asserting scheduled ∈ TERMINAL_STATUSES).
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class ScheduledIsNotTerminalTests(unittest.TestCase):
    """ADR-0073: TERMINAL_STATUSES must NOT contain 'scheduled'."""

    def test_scheduled_not_in_terminal_statuses(self):
        self.assertNotIn(
            'scheduled',
            pulse_server.TERMINAL_STATUSES,
            "'scheduled' is waiting (ADR-0073), never terminal — it was removed "
            "from the terminal allow-list under PLS-862",
        )

    def test_filter_keeps_scheduled_task(self):
        tasks = [
            {"id": 1, "key": "AT-1", "title": "Active task", "status": "open"},
            {"id": 2, "key": "AT-2", "title": "Parked recurring", "status": "scheduled"},
        ]
        kept, hidden = pulse_server._filter_out_terminal(tasks)
        keys = [t["key"] for t in kept]
        self.assertIn("AT-1", keys)
        self.assertIn("AT-2", keys, "scheduled task is NOT terminal — must not be filtered (PLS-862)")
        self.assertEqual(hidden, 0)

    def test_filter_keeps_open_task(self):
        tasks = [{"id": 3, "key": "AT-3", "title": "Ready work", "status": "open"}]
        kept, hidden = pulse_server._filter_out_terminal(tasks)
        self.assertEqual(len(kept), 1)
        self.assertEqual(hidden, 0)

    def test_filter_removes_all_terminal_statuses(self):
        """Sanity-check the entire TERMINAL_STATUSES set via filter."""
        terminal_tasks = [
            {"id": i, "key": f"T-{i}", "title": s, "status": s}
            for i, s in enumerate(pulse_server.TERMINAL_STATUSES)
        ]
        kept, hidden = pulse_server._filter_out_terminal(terminal_tasks)
        self.assertEqual(kept, [], "all terminal tasks must be filtered")
        self.assertEqual(hidden, len(terminal_tasks))

    def test_filter_case_insensitive_scheduled(self):
        """Status comparisons must be case-insensitive (API may return 'Scheduled')."""
        tasks = [{"id": 5, "key": "AT-5", "title": "Mixed case", "status": "Scheduled"}]
        kept, hidden = pulse_server._filter_out_terminal(tasks)
        self.assertEqual(len(kept), 1, "uppercase 'Scheduled' is still NOT terminal (PLS-862)")
        self.assertEqual(hidden, 0)

    def test_blocked_task_still_visible(self):
        """'blocked' is waiting work, NOT terminal — agents must still see it."""
        tasks = [{"id": 6, "key": "AT-6", "title": "Blocked task", "status": "blocked"}]
        kept, hidden = pulse_server._filter_out_terminal(tasks)
        self.assertEqual(len(kept), 1, "blocked task must NOT be filtered out")
        self.assertEqual(hidden, 0)


class WaitingClassificationTests(unittest.TestCase):
    """ADR-0073 §6: scheduled renders as 'blocked/waiting' — never ready/movable, never terminal."""

    def test_fmt_items_counts_scheduled_as_blocked_waiting(self):
        out = pulse_server._fmt_items([
            {"item_key": "AT-1", "id": 1, "title": "Active", "status": "in_progress", "priority": "high"},
            {"item_key": "BKSG-9", "id": 2, "title": "GST cycle", "status": "scheduled", "priority": "high"},
        ])
        self.assertIn("2 active (1 ready/movable, 1 blocked/waiting), 0 terminal", out)
        self.assertNotIn("2 ready/movable", out)
        self.assertNotIn("2 terminal", out)

    def test_fmt_items_terminal_still_terminal(self):
        """Polarity B: a done row is still counted terminal."""
        out = pulse_server._fmt_items([
            {"item_key": "AT-1", "id": 1, "title": "Active", "status": "in_progress", "priority": "high"},
            {"item_key": "AT-2", "id": 2, "title": "Done", "status": "done", "priority": "high"},
        ])
        self.assertIn("1 active (1 ready/movable, 0 blocked/waiting), 1 terminal", out)


if __name__ == "__main__":
    unittest.main()

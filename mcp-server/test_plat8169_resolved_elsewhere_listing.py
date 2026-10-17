"""PLAT-8169 arm (c): the queue listing must not classify `resolved_elsewhere`
blocker children as ready/movable.

PLS-719 defines `resolved_elsewhere` as the block-releasing RESTING state for a
blocker child whose ask was answered on the parent — "no action required in
this workflow state". The listing classifier (BACKLOG_PULL_WAITING_STATUSES)
never included it, so every heartbeat preflight counted these rows as
ready/movable and the assignee re-ruled no-action per cycle (rui's 09-14
evidence: PLS-1227, CON-438, SCLI-719, CTX-946, PLAT-8012).

Fix under test: `resolved_elsewhere` joins the blocked/waiting set, same class
as awaiting_merge/awaiting_deploy (another lane owns the next step — or no one
does, because the ask is answered).
"""

import unittest

import pulse_server


class ResolvedElsewhereListingTests(unittest.TestCase):
    def test_accepted_ledger_is_not_ready(self):
        self.assertIn('accepted', pulse_server.BACKLOG_PULL_WAITING_STATUSES)

    def test_resolved_elsewhere_in_waiting_set(self):
        """The classifier set carries the PLS-719 resting state (drift guard)."""
        self.assertIn(
            "resolved_elsewhere",
            pulse_server.BACKLOG_PULL_WAITING_STATUSES,
            "resolved_elsewhere must be classified blocked/waiting, not ready/movable "
            "(PLAT-8169 arm c / PLS-719 block-releasing resting state)",
        )

    def test_fmt_items_counts_resolved_elsewhere_as_blocked_waiting(self):
        """A non-terminal resolved_elsewhere row is blocked/waiting, never ready/movable."""
        items = [
            {"item_key": "A", "id": 1, "title": "live work", "status": "in_progress", "priority": "high"},
            {"item_key": "B", "id": 2, "title": "answered blocker child", "status": "resolved_elsewhere", "priority": "high"},
        ]
        out = pulse_server._fmt_items(items)
        self.assertIn(
            "Found 2 task(s) — 2 active (1 ready/movable, 1 blocked/waiting), 0 terminal",
            out,
        )

    def test_fmt_items_ready_excludes_resolved_elsewhere_alone(self):
        """A queue of ONLY resolved_elsewhere rows reports zero ready/movable."""
        items = [
            {"item_key": "A", "id": 1, "title": "a", "status": "resolved_elsewhere", "priority": "high"},
            {"item_key": "B", "id": 2, "title": "b", "status": "resolved_elsewhere", "priority": "normal"},
        ]
        out = pulse_server._fmt_items(items)
        self.assertIn("(0 ready/movable, 2 blocked/waiting)", out)


if __name__ == "__main__":
    unittest.main()

"""PLS-464: pulse_get_my_tasks / _fmt_items split counts into active, ready/movable,
blocked/waiting, and terminal — retiring the ambiguous single "actionable" label.

The heartbeat execution gate is READY/MOVABLE work. Before this change the
summary lumped blocked and ready work together as "actionable", so an
all-blocked queue read as actionable (contradicting the ready/movable gate).

Classification contract (must mirror _fmt_items):
  - active      = non-terminal total = ready + blocked/waiting
  - ready/movable = non-terminal, not in BACKLOG_PULL_WAITING_STATUSES, not parked
  - blocked/waiting = non-terminal in BACKLOG_PULL_WAITING_STATUSES or PARKED_WAITING_SLUGS
  - terminal    = TERMINAL_STATUSES allow-list
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


def _t(key, status, priority="normal"):
    return {"item_key": key, "id": 1, "title": key, "status": status, "priority": priority}


class Pls464ReadyMovableSplitTests(unittest.TestCase):
    """PLS-464 AC: active/ready/blocked/terminal counts are reported separately."""

    def test_all_ready(self):
        out = pulse_server._fmt_items([
            _t("A-1", "open"),
            _t("A-2", "in_progress"),
            _t("A-3", "in_review"),
        ])
        self.assertIn("3 active (3 ready/movable, 0 blocked/waiting), 0 terminal", out)
        self.assertNotIn("actionable", out)

    def test_awaiting_deploy_is_blocked_waiting_not_ready(self):
        """Shion 2026-09-15: HIVE-1888 awaiting_deploy counted as ready/movable."""
        out = pulse_server._fmt_items([
            _t("HIVE-1888", "awaiting_deploy"),
            _t("PLS-1165", "in_progress"),
        ])
        self.assertIn("2 active (1 ready/movable, 1 blocked/waiting), 0 terminal", out)
        self.assertFalse(pulse_server._is_backlog_pull_ready(_t("HIVE-1888", "awaiting_deploy")))
        self.assertTrue(pulse_server._is_backlog_pull_ready(_t("PLS-1165", "in_progress")))

    def test_all_blocked(self):
        out = pulse_server._fmt_items([
            _t("B-1", "blocked"),
            _t("B-2", "awaiting_input"),
            _t("B-3", "on_hold"),
        ])
        self.assertIn("3 active (0 ready/movable, 3 blocked/waiting), 0 terminal", out)
        self.assertNotIn("actionable", out)

    def test_mixed_queue(self):
        out = pulse_server._fmt_items([
            _t("M-1", "in_progress"),
            _t("M-2", "blocked"),
            _t("M-3", "done"),
        ])
        self.assertIn("2 active (1 ready/movable, 1 blocked/waiting), 1 terminal", out)

    def test_scheduled_is_blocked_waiting_not_terminal(self):
        out = pulse_server._fmt_items([
            _t("S-1", "scheduled"),
            _t("S-2", "in_progress"),
        ])
        self.assertIn("2 active (1 ready/movable, 1 blocked/waiting), 0 terminal", out)

    def test_terminal_allow_list_still_terminal(self):
        for status in ("completed", "cancelled", "closed", "done", "deferred"):
            out = pulse_server._fmt_items([_t("T-1", status)])
            self.assertIn("0 active (0 ready/movable, 0 blocked/waiting), 1 terminal", out)

    def test_empty_with_hidden_notes_uses_ready_movable_language(self):
        out = pulse_server._fmt_items([], hidden_terminal=2)
        self.assertIn("No ready/movable tasks found", out)
        self.assertNotIn("actionable", out)

    def test_unknown_status_counts_ready(self):
        # A status not in any allow-list is non-terminal and not blocked/waiting
        # -> ready/movable (fail-open toward movable, matching _is_backlog_pull_ready).
        out = pulse_server._fmt_items([_t("U-1", "some_future_status")])
        self.assertIn("1 active (1 ready/movable, 0 blocked/waiting), 0 terminal", out)

    def test_workflow_awareness_is_visible_not_ready_or_terminal(self):
        item = dict(_t('A-1', 'resolved_elsewhere'), owner_action_policy='awareness')
        out = pulse_server._fmt_items([item])
        self.assertIn('1 active (0 ready/movable, 0 blocked/waiting, 1 awareness-only), 0 terminal', out)
        self.assertIn('**A-1**', out)
        self.assertFalse(pulse_server._is_backlog_pull_ready(item))

    def test_awareness_is_metadata_driven_not_a_status_exclusion(self):
        item = dict(_t('A-1', 'future_custom_status'), owner_action_policy='awareness')
        self.assertFalse(pulse_server._is_backlog_pull_ready(item))
        item['owner_action_policy'] = 'actionable'
        self.assertTrue(pulse_server._is_backlog_pull_ready(item))
        self.assertIn('1 ready/movable', pulse_server._fmt_items([item]))
        item.pop('owner_action_policy')
        self.assertTrue(pulse_server._is_backlog_pull_ready(item))

    def test_awareness_does_not_double_count_blocked_or_terminal(self):
        items = [
            dict(_t('A-1', 'blocked'), owner_action_policy='awareness'),
            dict(_t('A-2', 'done'), owner_action_policy='awareness'),
            _t('A-3', 'blocked'),
            _t('A-4', 'in_progress'),
        ]
        self.assertIn(
            '3 active (1 ready/movable, 1 blocked/waiting, 1 awareness-only), 1 terminal',
            pulse_server._fmt_items(items),
        )


class Pls464HeaderConsistencyTests(unittest.TestCase):
    """PLS-464 AC: the heartbeat-facing output uses ready/movable terminology.

    Updated for the pulse_get_my_work contract (d18b625e768e): the page
    HEADER no longer carries the ready/movable split — it carries the
    anti-ordering contract instead ("You choose what to advance"). The
    PLS-464 substance (an all-blocked queue must not read as actionable)
    now lives in the _fmt_items summary line rendered directly below the
    header, so this test pins BOTH layers: the header's anti-ordering
    language and the summary's ready/movable split in the full output.
    """

    def test_page_header_uses_ready_movable(self):
        header = pulse_server._my_tasks_page_header("x@shizuha.com", 3, 10, 5)
        # New header contract: a listing, not a command.
        self.assertIn("You choose what to advance", header)
        self.assertNotIn("ready item", header)
        self.assertNotIn("Work the highest-priority", header)
        # PLS-464 substance moved to the summary line: the full rendered
        # output still splits active into ready/movable vs blocked/waiting.
        results = [_t("M-1", "open"), _t("M-2", "blocked"), _t("M-3", "done")]
        out = pulse_server._fmt_items(results)
        self.assertIn("ready/movable", out)
        self.assertIn("blocked/waiting", out)

    def test_page_header_short_form(self):
        header = pulse_server._my_tasks_page_header("x@shizuha.com", 3, 3, 5)
        self.assertIn("active", header)
        self.assertIn("queue-ordered", header)


if __name__ == "__main__":
    unittest.main()

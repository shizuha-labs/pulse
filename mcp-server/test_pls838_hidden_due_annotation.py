"""PLS-838: the hidden not-yet-due annotation must state the REAL distance.

The defect: `_fmt_items` labelled the whole suppressed bucket
`(>{FAR_FUTURE_DUE_DAYS}d out)`. That describes only ONE of the two suppression
rules in `_filter_far_future_due` — the far-future one (PLS-98). The other rule
(PLS-126) hides a future-due *recurring* occurrence at ANY distance, so an
urgent item due in under four hours was reported as ">30d out".

Why the fixture below deliberately mixes distances (acceptance 3): a fixture
whose hidden rows are all genuinely far out would pass against the broken code,
because `(>30d out)` happens to be true for them. The sub-24h hidden row is the
only part that can fail the old implementation, so it is the load-bearing case
rather than an extra one.

Review follow-up (rui, 2026-08-05): the type change hidden_scheduled int→list
broke the empty-queue early-return (`[] == 0` is False). That false-drain is
covered below so an empty default-active queue still reads as empty.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


NOW = datetime(2026, 8, 2, 19, 14, 13, tzinfo=timezone.utc)  # capture instant from the ticket


def _row(key, *, due_delta, priority="normal", status="todo", recurring=True):
    return {
        "item_key": key,
        "title": f"{key} title",
        "status": status,
        "priority": priority,
        "is_recurring": recurring,
        "due_date": (NOW + due_delta).isoformat(),
    }


class HiddenDueAnnotationTests(unittest.TestCase):
    def test_states_real_distance_not_a_static_window(self):
        """The reported reproduction: soonest hidden row is 3h53m out, not >30d."""
        hidden = [
            _row("PLAT-1189", due_delta=timedelta(hours=3, minutes=53), priority="urgent"),
            _row("EVOL-36", due_delta=timedelta(days=3, hours=19)),
            _row("BKSG-12", due_delta=timedelta(days=23)),
        ]
        note = pulse_server._describe_hidden_scheduled(hidden, now=NOW)

        self.assertIn("3 not yet due", note)
        self.assertIn("PLAT-1189", note)
        self.assertIn("3h53m", note)
        # The specific falsehood this ticket is about must not reappear in any form.
        self.assertNotIn("30d", note)
        self.assertNotIn(">", note)

    def test_names_an_escalated_item_even_when_it_is_not_the_soonest(self):
        """Acceptance 2: a blocked seat must see an urgent item coming."""
        hidden = [
            _row("NORM-1", due_delta=timedelta(hours=2)),
            _row("EVOL-8", due_delta=timedelta(days=5), priority="urgent"),
        ]
        note = pulse_server._describe_hidden_scheduled(hidden, now=NOW)

        self.assertIn("NORM-1", note)      # soonest, still reported
        self.assertIn("EVOL-8", note)      # escalated, named despite being later
        self.assertIn("urgent", note)

    def test_distance_tracks_the_minimum_not_the_first_row(self):
        """Ordering must come from the due dates, not from list order."""
        hidden = [
            _row("LATER", due_delta=timedelta(days=10)),
            _row("SOONER", due_delta=timedelta(hours=6)),
        ]
        note = pulse_server._describe_hidden_scheduled(hidden, now=NOW)
        self.assertIn("soonest SOONER", note)
        self.assertNotIn("soonest LATER", note)

    def test_no_distance_is_claimed_when_rows_have_no_due_date(self):
        """Never state a distance that was not computed — the original sin here."""
        note = pulse_server._describe_hidden_scheduled(
            [{"item_key": "NODUE", "status": "todo", "priority": "normal"}], now=NOW
        )
        self.assertIn("1 not yet due", note)
        self.assertNotIn(" in ", note)
        self.assertNotIn("30d", note)

    def test_int_input_degrades_to_a_bare_count(self):
        """Callers holding only a count must not have a distance invented for them."""
        note = pulse_server._describe_hidden_scheduled(4)
        self.assertEqual(note, "4 not yet due")
        self.assertEqual(pulse_server._describe_hidden_scheduled(0), "")
        self.assertEqual(pulse_server._describe_hidden_scheduled([]), "")

    def test_both_render_paths_use_the_shared_helper(self):
        """Acceptance 4: populated and empty outputs must not drift apart."""
        live_now = datetime.now(timezone.utc)
        hidden = [{
            "item_key": "PLAT-1189", "title": "t", "status": "todo", "priority": "urgent",
            "is_recurring": True,
            "due_date": (live_now + timedelta(hours=3, minutes=53)).isoformat(),
        }]

        populated = pulse_server._fmt_items(
            [{"item_key": "VIS-1", "title": "visible", "status": "in_progress", "priority": "high"}],
            hidden_scheduled=hidden,
        )
        empty = pulse_server._fmt_items([], hidden_scheduled=hidden)

        for out in (populated, empty):
            self.assertIn("PLAT-1189", out)
            self.assertIn("not yet due; soonest", out)
            self.assertNotIn("30d", out)

    def test_filter_returns_rows_so_the_annotation_can_be_computed(self):
        """The plumbing the fix depends on: hidden rows, not a bare count."""
        live_now = datetime.now(timezone.utc)
        soon_recurring = {
            "item_key": "SOON", "status": "todo", "priority": "urgent", "is_recurring": True,
            "due_date": (live_now + timedelta(hours=4)).isoformat(),
        }
        kept, hidden = pulse_server._filter_far_future_due([soon_recurring])

        self.assertEqual(kept, [])
        self.assertEqual([h["item_key"] for h in hidden], ["SOON"])
        # And it flows through to a truthful annotation end-to-end.
        self.assertIn("3h59m", pulse_server._describe_hidden_scheduled(hidden, now=live_now + timedelta(minutes=1)))

    def test_empty_filter_input_returns_empty_row_list_not_int_zero(self):
        """Empty input must keep the list contract — extend([]) is safe; extend(0) is not."""
        kept, hidden = pulse_server._filter_far_future_due([])
        self.assertEqual(kept, [])
        self.assertEqual(hidden, [])
        self.assertIsInstance(hidden, list)


class EmptyDefaultActiveQueueTests(unittest.TestCase):
    """P1 review fix: empty default-active queue must still say 'No tasks assigned'."""

    def test_get_my_tasks_empty_default_active_reads_as_empty(self):
        """`_fetch_default_active_page` returns ([], 0, []) → clean empty verdict.

        Pre-fix, `hidden_future == 0` compared a list to an int (`[] == 0` is
        False), so the early-return never fired and the tool emitted a
        'showing 0 of N' header — the exact false-drain this ticket class exists
        to eliminate.
        """
        def fake_fetch(params, limit, *, as_user=False):
            return {"results": [], "count": 0, "next": None}, [], 0, []

        def fake_api_request(method, endpoint, params=None, data=None, as_user=False):
            # Backlog pull lane must stay empty so we fall through to the guard.
            return {"results": [], "count": 0, "next": None}

        with patch.object(pulse_server, "_get_my_user_id", return_value=(1, "shion@shizuha.com")), \
             patch.object(pulse_server, "_fetch_default_active_page", side_effect=fake_fetch), \
             patch.object(pulse_server, "api_request", side_effect=fake_api_request):
            out = pulse_server.get_my_tasks()

        self.assertEqual(out, "No tasks assigned to shion@shizuha.com.")
        self.assertNotIn("showing 0", out)
        self.assertNotIn("Found 0", out)

    def test_get_user_tasks_empty_default_active_reads_as_empty(self):
        """Same guard on pulse_get_user_tasks (the twin heartbeat-facing tool)."""
        def fake_fetch(params, limit, *, as_user=False):
            return {"results": [], "count": 0, "next": None}, [], 0, []

        with patch.object(pulse_server, "_get_effective_user_token", return_value="tok"), \
             patch.object(pulse_server, "_fetch_default_active_page", side_effect=fake_fetch), \
             patch("jwt.decode", return_value={"user_id": 9, "email": "user@example.com"}):
            out = pulse_server.get_user_tasks()

        self.assertEqual(out, "No tasks assigned to user@example.com.")
        self.assertNotIn("showing 0", out)


class HumanizeDeltaTests(unittest.TestCase):
    def test_renders_each_magnitude_band(self):
        h = pulse_server._humanize_delta
        self.assertEqual(h(timedelta(minutes=45)), "45m")
        self.assertEqual(h(timedelta(hours=3, minutes=53)), "3h53m")
        self.assertEqual(h(timedelta(days=4, hours=17)), "4d 17h")
        self.assertEqual(h(timedelta(days=23)), "23d")
        self.assertEqual(h(timedelta(seconds=-60)), "overdue")


if __name__ == "__main__":
    unittest.main()

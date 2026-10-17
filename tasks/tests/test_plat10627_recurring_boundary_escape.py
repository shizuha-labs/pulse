"""PLAT-10627 regression: recurring boundary computation must ALWAYS advance
past ``now`` — a fire must never re-consume a stale boundary.

Live defect (2026-10-01): 13 fleet cadences froze with boundaries in the past
while the engine self-reported healthy. Two arithmetic shapes spin without
escape:

1. Unparseable frequency/pattern → ``calculate_next_future_due_date`` → None
   → the fire path no-ops ('no_next_due') and the boundary never advances
   (andon-exhausted spin: CTX-478 assignment_retry_attempts=51).
2. Zero/negative interval → ``calculate_next_due_date`` returns the SAME
   boundary → the catch-up loop breaks on ``advanced <= next_due`` and the
   pre-fix code RETURNED the stale past boundary (re-consume/re-land).

Pins:
- non-positive intervals are rejected at parse time (dict + string forms);
- ``calculate_next_future_due_date`` never returns a boundary ≤ now;
- the cycle-machine ``_next_due`` escape hatch returns a strictly future
  boundary for andon-exhausted stale dues, unparseable custom patterns, and
  zero-interval patterns (skip/scheduled_start classes keep progressing).
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from tasks.services.recurring import (
    calculate_next_future_due_date,
    parse_interval_seconds,
)
from tasks.services.recurring_cycle_state import _next_due as state_next_due


def _aware(dt):
    return dt.replace(tzinfo=dt_timezone.utc)


NOW = _aware(datetime(2026, 10, 1, 6, 0, 0))


def _item(frequency, pattern=None, due=None):
    return SimpleNamespace(
        item_key='TEST-1',
        frequency=frequency,
        recurrence_pattern=pattern,
        due_date=due,
    )


class ParseRejectsNonPositiveIntervalsTests(SimpleTestCase):
    def test_zero_interval_dict_rejected(self):
        self.assertIsNone(
            parse_interval_seconds('custom', {'interval_hours': 0}))

    def test_negative_interval_dict_rejected(self):
        self.assertIsNone(
            parse_interval_seconds('custom', {'interval_seconds': -30}))

    def test_zero_interval_string_rejected(self):
        self.assertIsNone(parse_interval_seconds('custom', 'every 0 hours'))


class NextFutureDueNeverReturnsPastTests(SimpleTestCase):
    def test_zero_interval_returns_none_not_stale_boundary(self):
        # Pre-fix: returned `current_due` (≤ now) — the spin shape.
        stale_due = NOW - timedelta(days=3)
        self.assertIsNone(
            calculate_next_future_due_date(
                stale_due, 'custom', NOW, {'interval_hours': 0}))

    def test_garbage_custom_pattern_returns_none(self):
        self.assertIsNone(
            calculate_next_future_due_date(
                NOW - timedelta(days=1), 'custom', NOW, 'not-a-schedule'))

    def test_stale_daily_due_catches_up_to_future(self):
        due = NOW - timedelta(days=10)
        nxt = calculate_next_future_due_date(due, 'daily', NOW)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, NOW)

    def test_stale_monthly_due_catches_up_to_future(self):
        due = NOW - timedelta(days=45)
        nxt = calculate_next_future_due_date(due, 'monthly', NOW)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, NOW)


class CycleMachineNextDueEscapeHatchTests(SimpleTestCase):
    def test_andon_exhausted_stale_daily_due_advances_strictly_future(self):
        """The andon branch computes natural_due with preserve_future=False
        from a boundary 11 days stale — must land strictly in the future."""
        item = _item('daily', due=NOW - timedelta(days=11))
        nxt = state_next_due(item, NOW, preserve_future=False)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, NOW)

    def test_unparseable_custom_pattern_still_advances(self):
        """Pre-fix: None → 'no_next_due' → frozen forever."""
        item = _item('custom', pattern='garbage', due=NOW - timedelta(days=5))
        nxt = state_next_due(item, NOW, preserve_future=False)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, NOW)

    def test_zero_interval_pattern_still_advances(self):
        """Pre-fix: stale boundary returned → re-consumed forever."""
        item = _item('custom', pattern={'interval_hours': 0},
                     due=NOW - timedelta(days=5))
        nxt = state_next_due(item, NOW, preserve_future=False)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, NOW)

    def test_completed_item_skip_chain_keeps_progressing(self):
        """active_skip frozen class: a completed item's next fire must move
        the boundary strictly forward each time."""
        item = _item('daily', due=NOW - timedelta(days=4))
        first = state_next_due(item, NOW, preserve_future=False)
        second = state_next_due(
            _item('daily', due=first), first, preserve_future=False)
        self.assertGreater(first, NOW)
        self.assertGreater(second, first)

    def test_preserve_future_keeps_future_boundary(self):
        item = _item('daily', due=NOW + timedelta(hours=2))
        self.assertEqual(
            state_next_due(item, NOW, preserve_future=True),
            NOW + timedelta(hours=2),
        )

    def test_missing_due_falls_back_to_future(self):
        """due_date=None with a broken pattern must still yield a future
        boundary, not None."""
        item = _item('custom', pattern='garbage', due=None)
        nxt = state_next_due(item, NOW, preserve_future=False)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, NOW)

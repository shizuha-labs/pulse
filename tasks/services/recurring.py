"""
Recurrence utilities — shared between the scheduler loop and the legacy
completion endpoint.

The core primitives are:

- ``parse_interval_seconds(frequency, recurrence_pattern)`` — turns a
  task's frequency + pattern into a fixed interval in seconds, or returns
  None for calendar-based frequencies (monthly / yearly) where a fixed
  second count can't represent the step.
- ``calculate_next_due_date(current_due, frequency, recurrence_pattern)``
  — returns the next occurrence after ``current_due``.
- ``calculate_next_future_due_date(...)`` — same, but catches up past
  missed periods when the scheduler was down for a while.

Design intent (simplicity over flexibility):

Recurring tasks have one knob that matters — the interval. `due_date`
is the single source of truth for the next fire time. Everything else
is derived. Patterns are expressed using the same `"every N unit"` form
that the local-pulse daemon scheduler already parses, so platform tasks
and daemon-local tasks behave identically. JSON-encoded patterns with
an `interval_seconds` key are also accepted for programmatic callers.
"""

import ast
import json
import re
from datetime import datetime, timedelta
from typing import Optional


# Matches patterns like "every 4 hours", "every 30 minutes", "every 7d".
# The unit group accepts both short (s/m/h/d/w) and long forms.
_CUSTOM_INTERVAL_RE = re.compile(
    r'^\s*every\s+(\d+)\s*'
    r'(s|sec|secs|second|seconds|'
    r'm|min|mins|minute|minutes|'
    r'h|hr|hrs|hour|hours|'
    r'd|day|days|'
    r'w|wk|wks|week|weeks)\s*$',
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    's': 1, 'sec': 1, 'secs': 1, 'second': 1, 'seconds': 1,
    'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60,
    'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600, 'hours': 3600,
    'd': 86400, 'day': 86400, 'days': 86400,
    'w': 604800, 'wk': 604800, 'wks': 604800, 'week': 604800, 'weeks': 604800,
}

# Keys agents have written in JSON recurrence_pattern dicts. Listed in
# descending unit so {"interval_hours": 4, "interval_seconds": 300} would
# prefer hours (unambiguous intent) — but in practice only one is ever set.
_INTERVAL_KEY_SECONDS = [
    ('interval_weeks', 604800),
    ('interval_days', 86400),
    ('interval_hours', 3600),
    ('interval_minutes', 60),
    ('interval_seconds', 1),
]


VALID_RECURRENCE_FREQUENCIES = (
    'one_time', 'daily', 'weekly', 'monthly', 'yearly', 'custom',
)
RECURRING_RECURRENCE_FREQUENCIES = tuple(
    f for f in VALID_RECURRENCE_FREQUENCIES if f != 'one_time'
)
_CUSTOM_RECURRENCE_HINT = (
    'use frequency=custom with recurrence_pattern="every N hours"; '
    'cron strings are not supported'
)


def validate_recurring_configuration(is_recurring, frequency, recurrence_pattern=None):
    """Validate Pulse Item recurrence fields.

    Raises ValueError with an operator/actionable message for combinations the
    scheduler cannot fire. Kept here so model, DRF serializers, MCP, and
    data-audit command all enforce the same contract.
    """
    if not is_recurring:
        return

    if not frequency:
        raise ValueError(
            'Recurring items require a frequency. Use one of '
            f'{list(RECURRING_RECURRENCE_FREQUENCIES)}; {_CUSTOM_RECURRENCE_HINT}.'
        )

    if frequency not in VALID_RECURRENCE_FREQUENCIES:
        raise ValueError(
            f"Unsupported recurring frequency {frequency!r}. Use one of "
            f"{list(RECURRING_RECURRENCE_FREQUENCIES)}; {_CUSTOM_RECURRENCE_HINT}."
        )

    if frequency == 'one_time':
        raise ValueError(
            'Recurring items cannot use frequency=one_time. Use one of '
            f'{list(RECURRING_RECURRENCE_FREQUENCIES)}; {_CUSTOM_RECURRENCE_HINT}.'
        )

    if frequency == 'custom' and parse_interval_seconds(frequency, recurrence_pattern) is None:
        raise ValueError(
            'Unsupported custom recurrence_pattern. For custom recurrence, '
            f'{_CUSTOM_RECURRENCE_HINT}.'
        )


def _interval_from_dict(d):
    """Extract a seconds value from a pattern dict supporting multiple unit keys."""
    # PLAT-10627: reject non-positive intervals at parse — a zero/negative
    # interval makes calculate_next_due_date return the SAME boundary, which
    # the fire path re-consumes forever (spin without escape).
    for key, mult in _INTERVAL_KEY_SECONDS:
        val = d.get(key)
        if val:
            try:
                seconds = int(val) * mult
            except (ValueError, TypeError):
                continue
            if seconds <= 0:
                continue
            return seconds
    return None


def parse_interval_seconds(
    frequency: str,
    recurrence_pattern=None,
) -> Optional[int]:
    """
    Return the interval between fires, in seconds, for a recurring task.

    Returns None when:
    - frequency is None / 'one_time'
    - frequency is calendar-based (monthly, yearly) — those aren't
      representable as a fixed second count and are handled via
      ``calculate_next_due_date``'s date-arithmetic branches
    - frequency is 'custom' and the pattern can't be interpreted

    Supported shapes for ``recurrence_pattern`` (when frequency='custom'):

    - Plain string, human-readable: ``"every 4 hours"``,
      ``"every 30 minutes"``, ``"every 7d"``. Same shape the local-pulse
      daemon's ``parseScheduleMs`` accepts, so platform-pulse and
      daemon-local recurring tasks use the same format.
    - JSON string encoding a dict: ``'{"interval_seconds": 14400}'``
    - Dict directly: ``{"interval_seconds": 14400}``
    """
    if not frequency:
        return None

    if frequency == 'daily':
        return 86400
    if frequency == 'weekly':
        return 7 * 86400
    if frequency == 'hourly':  # not in current choices, but harmless
        return 3600

    if frequency != 'custom':
        # Calendar frequencies (monthly, yearly) handled by caller.
        return None

    if not recurrence_pattern:
        return None

    # Dict form (already parsed) — accept any of the interval_* unit keys.
    # Agents / importers sometimes write {"interval_hours": 4} etc. instead
    # of expanding to seconds; without this branch the scheduler silently
    # leaves due_date=None and the task never fires.
    if isinstance(recurrence_pattern, dict):
        return _interval_from_dict(recurrence_pattern)

    if not isinstance(recurrence_pattern, str):
        return None

    s = recurrence_pattern.strip()
    if not s:
        return None

    # Encoded dict. Prefer strict JSON, but fall back to a Python literal:
    # the Item.recurrence_pattern TextField str()-coerces a dict assigned
    # directly (a common agent/MCP mistake), producing the repr form
    # "{'interval_hours': 2}" — single quotes, invalid JSON. ast.literal_eval
    # safely parses that without an eval(). Without this fallback the interval
    # comes back None and the task silently drops out of the recurring cycle.
    if s.startswith('{') or s.startswith('['):
        d = None
        try:
            d = json.loads(s)
        except (ValueError, TypeError):
            try:
                d = ast.literal_eval(s)
            except (ValueError, TypeError, SyntaxError):
                d = None
        if isinstance(d, dict):
            parsed = _interval_from_dict(d)
            if parsed:
                return parsed

    # Human-readable "every N unit"
    m = _CUSTOM_INTERVAL_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = _UNIT_SECONDS.get(unit)
        # PLAT-10627: n must be positive (see _interval_from_dict).
        if mult and n > 0:
            return n * mult

    return None


def calculate_next_due_date(
    current_due: datetime,
    frequency: str,
    recurrence_pattern=None,
) -> Optional[datetime]:
    """
    Return the next occurrence of a recurring item.

    Args:
        current_due: The current due_date of the item (tz-aware datetime).
        frequency: One of 'daily', 'weekly', 'monthly', 'yearly', 'custom'.
        recurrence_pattern: Optional pattern. Used only for 'custom'
            frequency. Accepted as a dict, JSON string, or the
            human-readable form "every N unit".

    Returns:
        The next due_date, or None if the frequency/pattern is unknown.
    """
    if not current_due or not frequency:
        return None

    # Fixed-interval frequencies (daily, weekly, hourly, custom)
    interval = parse_interval_seconds(frequency, recurrence_pattern)
    if interval is not None:
        return current_due + timedelta(seconds=interval)

    # Calendar-based frequencies
    if frequency == 'monthly':
        # Walk to the first of next month, then snap back to the same day
        # (or the month's last day if the source day doesn't exist).
        # PLAT-7404: the previous "+ timedelta(days=32)" walk overshot the
        # 1st of next month (a 31-day month lands on the 2nd), so the
        # "last day of next month" below actually computed the CURRENT
        # month's last day and the clamp never engaged — raising
        # ValueError for any 29th-31st followed by a shorter month
        # (e.g. Aug 31 -> "Sep 31"), date-dependently red-ing CI.
        if current_due.month == 12:
            first_of_next = current_due.replace(
                year=current_due.year + 1, month=1, day=1
            )
        else:
            first_of_next = current_due.replace(
                month=current_due.month + 1, day=1
            )
        if first_of_next.month == 12:
            first_of_following = first_of_next.replace(
                year=first_of_next.year + 1, month=1, day=1
            )
        else:
            first_of_following = first_of_next.replace(
                month=first_of_next.month + 1, day=1
            )
        last_day_of_next_month = (
            first_of_following - timedelta(days=1)
        ).day
        return first_of_next.replace(
            day=min(current_due.day, last_day_of_next_month)
        )

    if frequency == 'yearly':
        try:
            return current_due.replace(year=current_due.year + 1)
        except ValueError:
            # Feb 29 → Feb 28 of non-leap year
            return current_due.replace(year=current_due.year + 1, day=28)

    return None


def calculate_next_future_due_date(
    current_due: datetime,
    frequency: str,
    now: datetime,
    recurrence_pattern=None,
) -> Optional[datetime]:
    """
    Compute the next *future* occurrence, catching up past missed periods.

    If the scheduler was down for multiple periods (e.g., a daily
    recurring task with the last known due_date 14 days ago), we advance
    past all missed periods rather than firing 14 times in a row. This
    matches the "check in, don't spam" semantics the user asked for.
    """
    next_due = calculate_next_due_date(
        current_due, frequency, recurrence_pattern,
    )
    if next_due is None:
        return None

    # Bounded loop to avoid runaway on malformed frequency configs
    max_advances = 10_000
    while next_due <= now and max_advances > 0:
        advanced = calculate_next_due_date(
            next_due, frequency, recurrence_pattern,
        )
        if advanced is None or advanced <= next_due:
            # Guard against zero/negative-interval frequency misconfig
            break
        next_due = advanced
        max_advances -= 1

    # PLAT-10627: never return a stale boundary. If the catch-up loop broke
    # on a non-advancing interval or exhausted its bound without getting past
    # ``now``, report the boundary as uncomputable (None) instead of handing
    # the fire path a boundary in the past — re-consuming a past boundary is
    # the spin-without-escape that froze 13 fleet cadences (andon-exhausted,
    # skip, and scheduled_start classes). Callers own the fallback/escalation.
    if next_due <= now:
        return None
    return next_due

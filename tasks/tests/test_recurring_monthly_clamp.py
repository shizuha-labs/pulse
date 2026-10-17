"""PLAT-7404 regression: monthly recurrence must clamp to the NEXT month's
last day, not the current month's.

The previous ``+ timedelta(days=32)`` walk overshot the 1st of next month,
so the clamp computed the current month's length and any due date on the
29th-31st followed by a shorter month raised
``ValueError("day is out of range for month")`` — date-dependently red-ing
CI whenever ``(now - 1d)`` hit such a day (e.g. Aug 31 -> "Sep 31").
"""
from datetime import datetime

from django.test import SimpleTestCase

from tasks.services.recurring import calculate_next_due_date


def _dt(day, month, year=2026):
    return datetime(year, month, day, 9, 30, 0)


class MonthlyClampTests(SimpleTestCase):
    def test_31st_followed_by_30_day_month_clamps(self):
        # Aug 31 -> Sep 30 (was: ValueError "Sep 31")
        self.assertEqual(
            calculate_next_due_date(_dt(31, 8), 'monthly'),
            _dt(30, 9),
        )

    def test_31st_followed_by_31_day_month_keeps_day(self):
        # Jul 31 -> Aug 31
        self.assertEqual(
            calculate_next_due_date(_dt(31, 7), 'monthly'),
            _dt(31, 8),
        )

    def test_31st_into_february_clamps(self):
        # Jan 31 -> Feb 28 (2026 is non-leap)
        self.assertEqual(
            calculate_next_due_date(_dt(31, 1), 'monthly'),
            _dt(28, 2),
        )

    def test_31st_into_leap_february_clamps(self):
        # Jan 31 -> Feb 29 (2024 leap)
        self.assertEqual(
            calculate_next_due_date(_dt(31, 1, 2024), 'monthly'),
            _dt(29, 2, 2024),
        )

    def test_30th_keeps_day_into_31_day_month(self):
        # Mar 30 -> Apr 30
        self.assertEqual(
            calculate_next_due_date(_dt(30, 3), 'monthly'),
            _dt(30, 4),
        )

    def test_mid_month_unchanged(self):
        self.assertEqual(
            calculate_next_due_date(_dt(15, 8), 'monthly'),
            _dt(15, 9),
        )

    def test_first_of_month_unchanged(self):
        self.assertEqual(
            calculate_next_due_date(_dt(1, 9), 'monthly'),
            _dt(1, 10),
        )

    def test_december_wraps_year(self):
        self.assertEqual(
            calculate_next_due_date(_dt(31, 12, 2026), 'monthly'),
            _dt(31, 1, 2027),
        )

    def test_time_of_day_preserved(self):
        result = calculate_next_due_date(
            datetime(2026, 8, 31, 14, 45, 30), 'monthly'
        )
        self.assertEqual(
            (result.hour, result.minute, result.second), (14, 45, 30)
        )

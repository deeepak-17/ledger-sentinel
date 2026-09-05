"""T+2 business-day settlement rules, weekends and bank holidays."""

from __future__ import annotations

from datetime import date

import pytest

from src.dates import (
    add_business_days,
    days_off_expected,
    expected_settlement_date,
    is_business_day,
    within_settlement_window,
)

# 2026-08-15 (Independence Day) is a configured holiday but falls on a Saturday,
# so the useful holiday fixture is 2026-10-02, a Friday.
HOLIDAY_FRIDAY = date(2026, 10, 2)


class TestBusinessDays:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 9, 3), True),  # Thursday
            (date(2026, 9, 5), False),  # Saturday
            (date(2026, 9, 6), False),  # Sunday
            (HOLIDAY_FRIDAY, False),  # bank holiday
        ],
    )
    def test_weekends_and_holidays_are_not_business_days(self, day, expected):
        assert is_business_day(day) is expected

    def test_zero_days_is_a_no_op_even_on_a_holiday(self):
        assert add_business_days(HOLIDAY_FRIDAY, 0) == HOLIDAY_FRIDAY

    def test_rejects_negative_days(self):
        with pytest.raises(ValueError, match="must not be negative"):
            add_business_days(date(2026, 9, 3), -1)


class TestSettlementDate:
    def test_midweek_capture_settles_two_calendar_days_later(self):
        # Monday capture -> Wednesday credit.
        assert expected_settlement_date(date(2026, 9, 7)) == date(2026, 9, 9)

    def test_thursday_capture_skips_the_weekend(self):
        # Thursday -> Friday is one, Monday is two.
        assert expected_settlement_date(date(2026, 9, 3)) == date(2026, 9, 7)

    def test_capture_before_a_holiday_weekend_drifts_four_calendar_days(self):
        # Wednesday 2026-09-30 -> Thursday is one; Friday is a holiday and the
        # weekend follows, so the second business day is Monday 2026-10-05.
        assert expected_settlement_date(date(2026, 9, 30)) == date(2026, 10, 5)


class TestSettlementWindow:
    def test_exact_expected_date_is_inside_the_window(self):
        captured = date(2026, 9, 7)
        assert within_settlement_window(captured, date(2026, 9, 9)) is True

    def test_late_credit_within_tolerance_is_inside(self):
        captured = date(2026, 9, 7)
        assert within_settlement_window(captured, date(2026, 9, 12)) is True

    def test_far_late_credit_is_outside(self):
        captured = date(2026, 9, 7)
        assert within_settlement_window(captured, date(2026, 9, 20)) is False

    def test_money_can_never_arrive_before_it_was_captured(self):
        # This is the asymmetry that stops the matcher pairing a credit with a
        # later order that happens to have the same amount.
        captured = date(2026, 9, 7)
        assert within_settlement_window(captured, date(2026, 9, 6)) is False

    def test_drift_is_reported_signed(self):
        captured = date(2026, 9, 7)  # expects 2026-09-09
        assert days_off_expected(captured, date(2026, 9, 11)) == 2
        assert days_off_expected(captured, date(2026, 9, 9)) == 0

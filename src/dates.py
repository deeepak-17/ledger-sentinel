"""Business-day arithmetic for the settlement calendar.

Also deterministic by design. A settlement lands T+2 *business* days after
capture, where a business day excludes weekends and the configured bank
holidays -- so a Thursday capture surfaces on Monday, and a capture before a
holiday weekend can drift four calendar days. Most "the money is missing"
exceptions are really this.
"""

from __future__ import annotations

from datetime import date, timedelta

from config import BANK_HOLIDAYS, DATE_WINDOW_DAYS, SETTLEMENT_LAG_BUSINESS_DAYS

SATURDAY = 5


def is_business_day(day: date) -> bool:
    if day.weekday() >= SATURDAY:
        return False
    return day.isoformat() not in BANK_HOLIDAYS


def add_business_days(start: date, days: int) -> date:
    """Advance `days` business days from `start`. `days=0` returns `start`
    unchanged even if it is a holiday -- the caller decides what that means."""
    if days < 0:
        raise ValueError("days must not be negative")
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if is_business_day(current):
            remaining -= 1
    return current


def expected_settlement_date(captured_at: date) -> date:
    """When a payment captured on `captured_at` should reach the bank."""
    return add_business_days(captured_at, SETTLEMENT_LAG_BUSINESS_DAYS)


def within_settlement_window(captured_at: date, value_date: date) -> bool:
    """Whether a bank credit's value date is close enough to the expected
    settlement date to be the same event. Asymmetric on purpose: money arriving
    *before* it was captured is never plausible, however small the gap."""
    expected = expected_settlement_date(captured_at)
    if value_date < captured_at:
        return False
    return abs((value_date - expected).days) <= DATE_WINDOW_DAYS


def days_off_expected(captured_at: date, value_date: date) -> int:
    """Signed drift from the expected settlement date, for exception reporting."""
    return (value_date - expected_settlement_date(captured_at)).days

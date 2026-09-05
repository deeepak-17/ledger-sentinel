"""Fee and GST arithmetic in integer paise.

Deliberately not AI. This is the part of reconciliation that has one correct
answer, and a language model that computes money is a liability, not a feature.

Rounding is half-up on the paise, applied independently to the fee and then to
the GST on that fee -- which is what produces the one-paise disagreements that
case 8 exists to model.
"""

from __future__ import annotations

from config import BPS_DENOMINATOR, FEE_BPS_BY_METHOD, GST_BPS, PAISE_PER_RUPEE


class UnknownMethodError(ValueError):
    """Raised when a payment method has no configured fee rate."""


def _round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounding .5 away from zero. No floats touch money."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) * 2 + denominator) // (denominator * 2))


def fee_bps(method: str) -> int:
    try:
        return FEE_BPS_BY_METHOD[method]
    except KeyError as exc:
        raise UnknownMethodError(
            f"no fee rate configured for method {method!r}; "
            f"known methods: {sorted(FEE_BPS_BY_METHOD)}"
        ) from exc


def platform_fee_paise(amount_paise: int, method: str) -> int:
    """The gateway's cut, before tax."""
    if amount_paise < 0:
        raise ValueError("amount_paise must not be negative")
    return _round_half_up(amount_paise * fee_bps(method), BPS_DENOMINATOR)


def gst_on_fee_paise(fee_paise: int) -> int:
    """GST is levied on the fee, never on the transaction amount."""
    if fee_paise < 0:
        raise ValueError("fee_paise must not be negative")
    return _round_half_up(fee_paise * GST_BPS, BPS_DENOMINATOR)


def expected_deduction(amount_paise: int, method: str) -> tuple[int, int]:
    """Return `(fee_paise, tax_paise)` for a transaction. The classifier's
    `expected_fee` tool is a thin wrapper over exactly this function, so the
    model and the deterministic layer can never disagree about the arithmetic."""
    fee = platform_fee_paise(amount_paise, method)
    return fee, gst_on_fee_paise(fee)


def expected_net_paise(amount_paise: int, method: str) -> int:
    """What should actually land in the bank for a single settled payment."""
    fee, tax = expected_deduction(amount_paise, method)
    return amount_paise - fee - tax


def format_inr(paise: int) -> str:
    """Render paise for human-facing reports. Presentation only -- never fed back
    into arithmetic."""
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), PAISE_PER_RUPEE)
    return f"{sign}Rs {rupees:,}.{remainder:02d}"

"""Money arithmetic must be exact in integer paise. Floats never appear."""

from __future__ import annotations

import pytest

from src.money import (
    UnknownMethodError,
    expected_deduction,
    expected_net_paise,
    format_inr,
    gst_on_fee_paise,
    platform_fee_paise,
)


class TestPlatformFee:
    @pytest.mark.parametrize(
        ("amount_paise", "method", "expected_fee"),
        [
            (100_000, "card", 2_000),  # Rs 1000 at 2% -> Rs 20
            (100_000, "netbanking", 2_000),
            (100_000, "wallet", 2_000),
            (100_000, "upi", 0),  # zero MDR
            (1, "card", 0),  # 0.02 paise rounds down to nothing
            (25, "card", 1),  # 0.5 paise rounds half-up to 1
            (0, "card", 0),
        ],
    )
    def test_fee_is_two_percent_except_on_upi(self, amount_paise, method, expected_fee):
        assert platform_fee_paise(amount_paise, method) == expected_fee

    def test_rejects_unknown_method(self):
        with pytest.raises(UnknownMethodError, match="no fee rate configured"):
            platform_fee_paise(100_000, "crypto")

    def test_rejects_negative_amount(self):
        with pytest.raises(ValueError, match="must not be negative"):
            platform_fee_paise(-1, "card")


class TestGst:
    @pytest.mark.parametrize(
        ("fee_paise", "expected_tax"),
        [
            (2_000, 360),  # 18% of Rs 20 -> Rs 3.60
            (0, 0),
            (1, 0),  # 0.18 paise rounds to nothing
            (3, 1),  # 0.54 paise rounds half-up to 1
        ],
    )
    def test_gst_is_eighteen_percent_of_the_fee(self, fee_paise, expected_tax):
        assert gst_on_fee_paise(fee_paise) == expected_tax

    def test_gst_is_never_charged_on_the_transaction(self):
        # A Rs 1000 card sale must deduct Rs 23.60, not Rs 180 + Rs 20.
        fee, tax = expected_deduction(100_000, "card")
        assert (fee, tax) == (2_000, 360)


class TestNetSettlement:
    def test_net_is_amount_less_fee_and_tax(self):
        # Arrange
        amount_paise = 100_000

        # Act
        net = expected_net_paise(amount_paise, "card")

        # Assert
        assert net == 100_000 - 2_000 - 360

    def test_upi_settles_gross(self):
        assert expected_net_paise(100_000, "upi") == 100_000

    @pytest.mark.parametrize("amount_paise", [1, 999, 12_345, 999_999, 100_000_000])
    def test_net_plus_deductions_always_reconstructs_gross(self, amount_paise):
        fee, tax = expected_deduction(amount_paise, "card")
        assert expected_net_paise(amount_paise, "card") + fee + tax == amount_paise


class TestFormatting:
    @pytest.mark.parametrize(
        ("paise", "rendered"),
        [
            (100_000, "Rs 1,000.00"),
            (2_360, "Rs 23.60"),
            (5, "Rs 0.05"),
            (-2_360, "-Rs 23.60"),
            (0, "Rs 0.00"),
        ],
    )
    def test_renders_rupees_and_paise(self, paise, rendered):
        assert format_inr(paise) == rendered

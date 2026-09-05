"""Typed models for the three input sources and the ground-truth file.

Field names follow Razorpay's public settlement recon report where one exists
(`settlement_id`, `utr`, `payment_id`, `order_id`, `fee`, `tax`, `method`) so a
reader who knows the product recognises the shape. Amount columns carry an
explicit `_paise` suffix because the report's rupee-denominated columns are the
single easiest place to introduce a float bug.

Validation is strict on purpose: a malformed synthetic row must fail at
ingestion, not silently become a mismatch that flatters our exception count.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Paise = Annotated[int, Field(ge=0)]

PaymentMethod = Literal["card", "upi", "netbanking", "wallet"]
OrderStatus = Literal["paid", "refunded", "partially_refunded"]
EntityType = Literal["payment", "refund", "adjustment"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Order(StrictModel):
    """A row from the merchant's own order book -- what the merchant believes it sold."""

    order_id: str
    payment_id: str
    amount_paise: Paise
    currency: Literal["INR"] = "INR"
    method: PaymentMethod
    status: OrderStatus
    captured_at: date

    @field_validator("order_id", "payment_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("identifier must not be empty")
        return value


class SettlementRow(StrictModel):
    """A row from the Razorpay settlement recon report.

    One row per entity (payment, refund or adjustment), grouped into a payout by
    `settlement_id`. `credit_paise` and `debit_paise` are mutually exclusive:
    payments credit the merchant, refunds and chargebacks debit it.
    """

    settlement_id: str
    utr: str
    entity_id: str
    entity_type: EntityType
    order_id: str | None = None
    payment_id: str | None = None
    method: PaymentMethod | None = None
    amount_paise: Paise
    fee_paise: Paise = 0
    tax_paise: Paise = 0
    credit_paise: Paise = 0
    debit_paise: Paise = 0
    on_hold: bool = False
    settled_at: date

    @field_validator("debit_paise")
    @classmethod
    def _one_direction_only(cls, debit: int, info) -> int:
        if debit and info.data.get("credit_paise"):
            raise ValueError("a settlement row cannot both credit and debit")
        return debit

    @property
    def net_paise(self) -> int:
        """What this row contributes to its payout: positive credits, negative debits."""
        return self.credit_paise - self.debit_paise


class BankTxn(StrictModel):
    """A line from the merchant's bank statement -- the only source that is ground
    truth about money actually moving.

    `utr` is optional because it genuinely is. Single NEFT credits carry the
    reference in the narration; consolidated payouts arrive over a different rail
    with nothing to join on. That absence is the hard part of the problem, not a
    data-quality bug to clean up at ingestion.
    """

    txn_id: str
    value_date: date
    description: str
    utr: str | None = None
    credit_paise: Paise = 0
    debit_paise: Paise = 0

    @property
    def net_paise(self) -> int:
        return self.credit_paise - self.debit_paise


class TruthRecord(StrictModel):
    """One planted case and the answer the pipeline is scored against.

    `expected_resolution` is deliberately coarse -- "matched" or "escalate" --
    because that is the decision the system actually takes. `case_id` scores the
    diagnosis separately, so a right answer for a wrong reason is still visible.
    """

    truth_id: str
    case_id: int
    expected_resolution: Literal["matched", "escalate"]
    order_ids: tuple[str, ...] = ()
    settlement_ids: tuple[str, ...] = ()
    bank_txn_ids: tuple[str, ...] = ()
    note: str = ""


class Dataset(StrictModel):
    """The full contents of one seed directory, already validated."""

    seed: str
    orders: tuple[Order, ...]
    settlements: tuple[SettlementRow, ...]
    bank: tuple[BankTxn, ...]
    truth: tuple[TruthRecord, ...]

"""Per-case scenario builders.

Each builder plants exactly one case and returns the orders, settlement rows and
bank lines it produced together with the truth record scoring it. The builders
know the answer; nothing outside `data/` may import from this module.

A deliberate constraint: no builder may leave a tell that the matcher could key
on other than the economics of the case itself. Ids are random, dates overlap
between cases, and the output is sorted by date before it is written.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from config import ROUNDING_TOLERANCE_PAISE
from data.ids import IdMinter
from src.cases import Case
from src.dates import expected_settlement_date
from src.money import expected_deduction
from src.schema import BankTxn, Order, SettlementRow, TruthRecord

FEE_BEARING_METHODS = ("card", "netbanking", "wallet")

# Case 10 needs net(a) + net(b) == net(a + b) to hold exactly, which is only true
# when the fee and GST divide without rounding. Multiples of Rs 50 guarantee it.
COLLISION_AMOUNT_STEP_PAISE = 5_000


@dataclass
class Unit:
    """One reconciliation unit: the rows for a single planted case, plus its answer."""

    orders: list[Order]
    settlements: list[SettlementRow]
    bank: list[BankTxn]
    truth: TruthRecord


@dataclass
class Context:
    rng: random.Random
    ids: IdMinter
    cycle_start: date
    _truth_counter: list[int]
    _bank_counter: list[int]

    def next_truth_id(self) -> str:
        self._truth_counter[0] += 1
        return f"T{self._truth_counter[0]:04d}"

    def next_provisional_bank_id(self) -> str:
        """Bank lines are renumbered once the whole statement is sorted by date,
        so builders hand out a provisional handle that truth records point at and
        the generator rewrites. Keyed by handle rather than UTR because a bulk
        credit has no UTR at all, and case 11 will deliberately reuse one."""
        self._bank_counter[0] += 1
        return f"TMP{self._bank_counter[0]:04d}"

    def capture_date(self) -> date:
        """A capture date somewhere in the first two weeks of the cycle."""
        return self.cycle_start + timedelta(days=self.rng.randrange(0, 14))

    def amount(self, low_rupees: int = 150, high_rupees: int = 9_500) -> int:
        """A plausible order value in paise, priced to the rupee."""
        return self.rng.randrange(low_rupees, high_rupees) * 100

    def collision_amount(self, low_steps: int = 4, high_steps: int = 40) -> int:
        return self.rng.randrange(low_steps, high_steps) * COLLISION_AMOUNT_STEP_PAISE


# ---------------------------------------------------------------------------
# helpers shared by the builders
# ---------------------------------------------------------------------------


def _make_order(
    ctx: Context,
    *,
    method: str,
    amount_paise: int,
    captured_at: date,
    status: str = "paid",
) -> Order:
    return Order(
        order_id=ctx.ids.order(),
        payment_id=ctx.ids.payment(),
        amount_paise=amount_paise,
        method=method,
        status=status,
        captured_at=captured_at,
    )


def _payment_row(order: Order, *, settlement_id: str, utr: str, settled_at: date) -> SettlementRow:
    fee, tax = expected_deduction(order.amount_paise, order.method)
    return SettlementRow(
        settlement_id=settlement_id,
        utr=utr,
        entity_id=order.payment_id,
        entity_type="payment",
        order_id=order.order_id,
        payment_id=order.payment_id,
        method=order.method,
        amount_paise=order.amount_paise,
        fee_paise=fee,
        tax_paise=tax,
        credit_paise=order.amount_paise - fee - tax,
        settled_at=settled_at,
    )


def _refund_row(
    ctx: Context,
    order: Order,
    *,
    settlement_id: str,
    utr: str,
    settled_at: date,
    amount_paise: int | None = None,
) -> SettlementRow:
    """A refund debits the gross amount back. The fee already charged on the
    original payment is NOT returned -- which is precisely why a netted refund
    does not reconcile against the order's face value."""
    refunded = order.amount_paise if amount_paise is None else amount_paise
    return SettlementRow(
        settlement_id=settlement_id,
        utr=utr,
        entity_id=ctx.ids.refund(),
        entity_type="refund",
        order_id=order.order_id,
        payment_id=order.payment_id,
        method=order.method,
        amount_paise=refunded,
        debit_paise=refunded,
        settled_at=settled_at,
    )


def _bank_credit(ctx: Context, *, utr: str, value_date: date, paise: int) -> BankTxn:
    """A single NEFT credit, which carries its UTR in the narration."""
    return BankTxn(
        txn_id=ctx.next_provisional_bank_id(),
        value_date=value_date,
        description=f"NEFT CR RAZORPAY SOFTWARE PVT LTD {utr}",
        utr=utr,
        credit_paise=paise,
    )


def _bulk_credit(ctx: Context, *, value_date: date, paise: int) -> BankTxn:
    """A consolidated payout as it actually appears on an Indian bank statement.

    Single NEFT credits carry the UTR in the narration. Bulk payouts arrive over a
    different rail with a generic narration and no reference the merchant can join
    on -- which is why batched settlements are the part of reconciliation that
    genuinely needs reasoning rather than a lookup. This is the difficulty knob of
    the dataset and it is the honest one: we are not deleting evidence, we are
    modelling a statement format that never carried it.
    """
    return BankTxn(
        txn_id=ctx.next_provisional_bank_id(),
        value_date=value_date,
        description=f"RAZORPAY PAYOUT CONSOLIDATED {value_date.isoformat()}",
        utr=None,
        credit_paise=paise,
    )


def _truth(
    ctx: Context,
    case: Case,
    resolution: str,
    orders: list[Order],
    settlements: list[SettlementRow],
    bank: list[BankTxn],
    note: str,
) -> TruthRecord:
    return TruthRecord(
        truth_id=ctx.next_truth_id(),
        case_id=int(case),
        expected_resolution=resolution,
        order_ids=tuple(o.order_id for o in orders),
        settlement_ids=tuple(s.entity_id for s in settlements),
        bank_txn_ids=tuple(b.txn_id for b in bank),
        note=note,
    )


# ---------------------------------------------------------------------------
# case builders
# ---------------------------------------------------------------------------


def build_clean_match(ctx: Context) -> Unit:
    """Case 1. UPI carries no MDR, so the credit equals the order exactly."""
    captured = ctx.capture_date()
    order = _make_order(ctx, method="upi", amount_paise=ctx.amount(), captured_at=captured)
    settled = expected_settlement_date(captured)
    utr = ctx.ids.utr()
    row = _payment_row(order, settlement_id=ctx.ids.settlement(), utr=utr, settled_at=settled)
    credit = _bank_credit(ctx, utr=utr, value_date=settled, paise=row.credit_paise)
    return Unit(
        [order],
        [row],
        [credit],
        _truth(
            ctx,
            Case.CLEAN_MATCH,
            "matched",
            [order],
            [row],
            [credit],
            "gross amount credited in full; no fee on UPI",
        ),
    )


def build_fee_and_gst(ctx: Context) -> Unit:
    """Case 2. The credit is short by fee + 18% GST on that fee."""
    captured = ctx.capture_date()
    method = ctx.rng.choice(FEE_BEARING_METHODS)
    order = _make_order(ctx, method=method, amount_paise=ctx.amount(), captured_at=captured)
    settled = expected_settlement_date(captured)
    utr = ctx.ids.utr()
    row = _payment_row(order, settlement_id=ctx.ids.settlement(), utr=utr, settled_at=settled)
    credit = _bank_credit(ctx, utr=utr, value_date=settled, paise=row.credit_paise)
    return Unit(
        [order],
        [row],
        [credit],
        _truth(
            ctx,
            Case.FEE_AND_GST,
            "matched",
            [order],
            [row],
            [credit],
            f"short by fee {row.fee_paise}p + GST {row.tax_paise}p",
        ),
    )


def _late_week_capture(ctx: Context) -> date:
    """Pick a Thursday or Friday so the T+2 settlement crosses a weekend."""
    for offset in range(0, 21):
        day = ctx.cycle_start + timedelta(days=(ctx.rng.randrange(0, 14) + offset))
        if day.weekday() in (3, 4):
            return day
    return ctx.cycle_start + timedelta(days=3)


def build_settlement_timing(ctx: Context) -> Unit:
    """Case 3. Amount agrees; only the calendar looks wrong. Captured immediately
    before a weekend or bank holiday so a naive T+2 in calendar days misses it."""
    captured = _late_week_capture(ctx)
    method = ctx.rng.choice((*FEE_BEARING_METHODS, "upi"))
    order = _make_order(ctx, method=method, amount_paise=ctx.amount(), captured_at=captured)
    settled = expected_settlement_date(captured)
    utr = ctx.ids.utr()
    row = _payment_row(order, settlement_id=ctx.ids.settlement(), utr=utr, settled_at=settled)
    credit = _bank_credit(ctx, utr=utr, value_date=settled, paise=row.credit_paise)
    drift = (settled - captured).days
    return Unit(
        [order],
        [row],
        [credit],
        _truth(
            ctx,
            Case.SETTLEMENT_TIMING,
            "matched",
            [order],
            [row],
            [credit],
            f"credited {drift} calendar days after capture across a non-business day",
        ),
    )


def build_rounding_drift(ctx: Context) -> Unit:
    """Case 8. Two systems round the same fee independently. One paise apart is
    a match within tolerance, not an exception -- reporting it would be noise."""
    captured = ctx.capture_date()
    method = ctx.rng.choice(FEE_BEARING_METHODS)
    order = _make_order(ctx, method=method, amount_paise=ctx.amount(), captured_at=captured)
    settled = expected_settlement_date(captured)
    utr = ctx.ids.utr()
    row = _payment_row(order, settlement_id=ctx.ids.settlement(), utr=utr, settled_at=settled)
    drift = ctx.rng.choice([-ROUNDING_TOLERANCE_PAISE, -1, 1, ROUNDING_TOLERANCE_PAISE])
    credit = _bank_credit(ctx, utr=utr, value_date=settled, paise=row.credit_paise + drift)
    return Unit(
        [order],
        [row],
        [credit],
        _truth(
            ctx,
            Case.ROUNDING_DRIFT,
            "matched",
            [order],
            [row],
            [credit],
            f"bank credit differs from computed net by {drift}p",
        ),
    )


def build_batched_payout(ctx: Context, batch_size: int = 3) -> Unit:
    """Case 4. Several settlements leave as one consolidated credit with no
    reference to join on. No single order explains the figure; exactly one subset
    of the rows in the window does."""
    capture_base = ctx.capture_date()
    settlement_id = ctx.ids.settlement()
    utr = ctx.ids.utr()

    orders: list[Order] = []
    for _ in range(batch_size):
        captured = capture_base + timedelta(days=ctx.rng.randrange(0, 2))
        method = ctx.rng.choice((*FEE_BEARING_METHODS, "upi"))
        order = _make_order(ctx, method=method, amount_paise=ctx.amount(), captured_at=captured)
        orders.append(order)

    settled = expected_settlement_date(max(o.captured_at for o in orders))
    rows = [
        _payment_row(o, settlement_id=settlement_id, utr=utr, settled_at=settled) for o in orders
    ]
    credit = _bulk_credit(ctx, value_date=settled, paise=sum(r.net_paise for r in rows))
    return Unit(
        orders,
        rows,
        [credit],
        _truth(
            ctx,
            Case.BATCHED_PAYOUT,
            "matched",
            orders,
            rows,
            [credit],
            f"{batch_size} settlements paid out as one consolidated credit with no UTR",
        ),
    )


def build_netted_refund(ctx: Context) -> Unit:
    """Case 5. A refund is netted inside the payout rather than debited
    separately. The merchant does not get the original fee back, so the credit
    reconciles against neither the gross nor the net of the surviving order, and
    no subset of the payment rows alone can reach it."""
    settlement_id = ctx.ids.settlement()
    utr = ctx.ids.utr()
    captured = ctx.capture_date()

    kept = _make_order(
        ctx,
        method=ctx.rng.choice(FEE_BEARING_METHODS),
        amount_paise=ctx.amount(low_rupees=2_000, high_rupees=9_500),
        captured_at=captured,
    )
    reversed_order = _make_order(
        ctx,
        method=ctx.rng.choice(FEE_BEARING_METHODS),
        amount_paise=ctx.amount(low_rupees=150, high_rupees=1_500),
        captured_at=captured,
        status="refunded",
    )

    settled = expected_settlement_date(captured)
    kept_row = _payment_row(kept, settlement_id=settlement_id, utr=utr, settled_at=settled)
    reversed_row = _payment_row(
        reversed_order, settlement_id=settlement_id, utr=utr, settled_at=settled
    )
    refund_row = _refund_row(
        ctx, reversed_order, settlement_id=settlement_id, utr=utr, settled_at=settled
    )

    rows = [kept_row, reversed_row, refund_row]
    credit = _bulk_credit(ctx, value_date=settled, paise=sum(r.net_paise for r in rows))
    fee, tax = expected_deduction(reversed_order.amount_paise, reversed_order.method)
    return Unit(
        [kept, reversed_order],
        rows,
        [credit],
        _truth(
            ctx,
            Case.NETTED_REFUND,
            "matched",
            [kept, reversed_order],
            rows,
            [credit],
            f"refund of {reversed_order.order_id} netted into the payout; "
            f"fee {fee}p + GST {tax}p not returned",
        ),
    )


def build_amount_collision(ctx: Context) -> Unit:
    """Case 10, adversarial. Three settlements sit in one window where
    net(a) + net(b) == net(c) exactly, and the bank shows a single consolidated
    credit for that figure. Reading it as `c` and reading it as `a + b` are both
    arithmetically perfect; only one is true.

    The planted truth is `a + b`. `c` was paid on a credit that falls outside this
    statement export -- an ordinary partial download, not a trick. Crucially
    nothing in the data decides between the readings: `c` is not on hold, not
    flagged, not late, and sits in the same window. A field that resolved it would
    turn this into a puzzle with an answer, and the whole point of the case is
    that it has none. Confidence here is the failure; escalation is the only
    correct output.
    """
    captured = ctx.capture_date()
    settled = expected_settlement_date(captured)

    amount_a = ctx.collision_amount()
    amount_b = ctx.collision_amount()
    order_a = _make_order(ctx, method="card", amount_paise=amount_a, captured_at=captured)
    order_b = _make_order(ctx, method="card", amount_paise=amount_b, captured_at=captured)
    order_c = _make_order(
        ctx, method="card", amount_paise=amount_a + amount_b, captured_at=captured
    )

    batch_id = ctx.ids.settlement()
    batch_utr = ctx.ids.utr()
    row_a = _payment_row(order_a, settlement_id=batch_id, utr=batch_utr, settled_at=settled)
    row_b = _payment_row(order_b, settlement_id=batch_id, utr=batch_utr, settled_at=settled)
    row_c = _payment_row(
        order_c, settlement_id=ctx.ids.settlement(), utr=ctx.ids.utr(), settled_at=settled
    )

    if row_a.net_paise + row_b.net_paise != row_c.net_paise:
        raise AssertionError(
            "collision construction broken: fee rounding made the two readings "
            f"differ ({row_a.net_paise} + {row_b.net_paise} != {row_c.net_paise}). "
            "Amounts must be multiples of COLLISION_AMOUNT_STEP_PAISE."
        )

    credit = _bulk_credit(ctx, value_date=settled, paise=row_a.net_paise + row_b.net_paise)
    return Unit(
        [order_a, order_b, order_c],
        [row_a, row_b, row_c],
        [credit],
        _truth(
            ctx,
            Case.AMOUNT_COLLISION,
            "escalate",
            [order_a, order_b, order_c],
            [row_a, row_b, row_c],
            [credit],
            "credit equals both (a+b) and c exactly and nothing in the data "
            "distinguishes them; truth is a+b, c settled outside this export",
        ),
    )

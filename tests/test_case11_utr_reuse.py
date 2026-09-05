"""Case 11 -- a bank reference that closes a second match must not be trusted.

`RULE_UTR_ALREADY_CLAIMED` has been written since M2 and, until this file, had
never executed against any data. A rule that has never fired is a claim, not a
feature: nothing proved it was reachable, that it produced a usable reason, or
that it declined to match rather than quietly matching twice.

The reported seeds do not plant case 11 -- `ACTIVE_CASES` ships 1/2/3/4/5/8/10 --
and planting it there would change every classifier prompt and invalidate the
committed response cache. So the case is exercised here against a purpose-built
dataset instead. That proves the rule works; it does not claim case 11 appears
in the published numbers, and `src/metrics.py` still scores only what the seeds
contain.

Why the rule exists at all: a UTR is the only thing that opens a match in this
system (see `docs/architecture.md`). If the same reference resolves two separate
credits, it has stopped identifying anything -- and the fact that it legitimately
closed an earlier credit does not make the second one true. Matching both is the
expensive failure: it books money twice against one payout.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.matcher import (
    MATCH_RULES,
    RULE_UTR_ALREADY_CLAIMED,
    RULE_UTR_EXACT,
    reconcile,
)
from src.schema import BankTxn, Dataset, Order, SettlementRow

SETTLED = date(2026, 8, 10)
VALUE_DATE = date(2026, 8, 10)
REUSED_UTR = "UTR900000000001"
NET_PAISE = 500_000


def _dataset() -> Dataset:
    """One payout, and two bank credits that both carry its reference.

    The second credit is otherwise perfect -- same amount, same date, inside the
    window -- so the *only* thing that can stop it matching is the reuse rule.
    Anything else about it agreeing is the point: this is not a malformed row.
    """
    order = Order(
        order_id="order_reuse_1",
        payment_id="pay_reuse_1",
        amount_paise=NET_PAISE,
        method="upi",
        status="paid",
        captured_at=date(2026, 8, 6),
    )
    settlement = SettlementRow(
        settlement_id="setl_reuse_1",
        utr=REUSED_UTR,
        entity_id="pay_reuse_1",
        entity_type="payment",
        order_id="order_reuse_1",
        payment_id="pay_reuse_1",
        method="upi",
        amount_paise=NET_PAISE,
        credit_paise=NET_PAISE,
        settled_at=SETTLED,
    )
    first = BankTxn(
        txn_id="BNK90001",
        value_date=VALUE_DATE,
        description=f"NEFT CR {REUSED_UTR}",
        utr=REUSED_UTR,
        credit_paise=NET_PAISE,
    )
    second = BankTxn(
        txn_id="BNK90002",
        value_date=VALUE_DATE,
        description=f"NEFT CR {REUSED_UTR}",
        utr=REUSED_UTR,
        credit_paise=NET_PAISE,
    )
    return Dataset(
        seed="case11",
        orders=(order,),
        settlements=(settlement,),
        bank=(first, second),
        truth=(),
    )


@pytest.fixture(scope="module")
def result():
    return reconcile(_dataset())


class TestAReusedReferenceCannotCloseASecondMatch:
    def test_the_first_credit_matches_normally(self, result):
        """The rule must not punish the legitimate claim. Exactly one match."""
        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.bank_txn_id == "BNK90001"
        assert match.rule == RULE_UTR_EXACT

    def test_the_second_credit_is_refused_and_names_the_earlier_claim(self, result):
        exceptions = {e.bank_txn_id: e for e in result.exceptions}
        assert set(exceptions) == {"BNK90002"}

        refused = exceptions["BNK90002"]
        assert refused.rule == RULE_UTR_ALREADY_CLAIMED
        # A controller has to be able to act on this without reading the code,
        # so the reason names both the reference and the credit that took it.
        assert REUSED_UTR in refused.reason
        assert "BNK90001" in refused.reason

    def test_the_refusal_still_carries_its_candidate_rows(self, result):
        """Refusing is not the same as having nothing to say. The rows the
        reference resolves to travel with the exception, so the classifier and a
        human both start from evidence rather than from scratch."""
        refused = next(e for e in result.exceptions if e.bank_txn_id == "BNK90002")
        assert refused.candidate_entity_ids == ("pay_reuse_1",)

    def test_the_settlement_row_is_never_claimed_twice(self, result):
        """The failure this rule exists to prevent: one payout booked against two
        credits. Money counted twice is worse than money left unexplained."""
        claimed = [eid for m in result.matches for eid in m.settlement_entity_ids]
        assert claimed == ["pay_reuse_1"]

    def test_the_second_credit_is_not_matched_by_any_rule(self, result):
        matched_ids = {m.bank_txn_id for m in result.matches}
        assert "BNK90002" not in matched_ids
        assert {m.rule for m in result.matches} <= MATCH_RULES

"""The 11-case reconciliation taxonomy.

This is the contract shared by the generator (which plants cases), the matcher
and classifier (which diagnose them), and the metrics layer (which scores per
case). Nothing else in the codebase may invent a case id.

`resolvable_by` records which layer is *supposed* to close each case. It is
documentation and a test fixture -- never an input to the classifier, which
would leak ground truth into the thing being measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class ResolutionLayer(StrEnum):
    DETERMINISTIC = "deterministic"
    TOOL = "tool"
    AI = "ai"
    ESCALATE = "escalate"


class Case(IntEnum):
    CLEAN_MATCH = 1
    FEE_AND_GST = 2
    SETTLEMENT_TIMING = 3
    BATCHED_PAYOUT = 4
    NETTED_REFUND = 5
    PARTIAL_REFUND = 6
    CHARGEBACK = 7
    ROUNDING_DRIFT = 8
    DUPLICATE_PAYMENT = 9
    AMOUNT_COLLISION = 10
    UTR_REUSE = 11


@dataclass(frozen=True)
class CaseSpec:
    case: Case
    name: str
    description: str
    resolvable_by: ResolutionLayer
    adversarial: bool = False


_SPECS: tuple[CaseSpec, ...] = (
    CaseSpec(
        Case.CLEAN_MATCH,
        "Clean match",
        "One order, one settlement row, one bank credit. Amounts agree exactly "
        "after the zero-MDR path. The control group.",
        ResolutionLayer.DETERMINISTIC,
    ),
    CaseSpec(
        Case.FEE_AND_GST,
        "Fee and GST deducted",
        "The bank credit is short by the platform fee plus 18% GST on that fee. "
        "Arithmetically closed -- no judgement required, so no AI.",
        ResolutionLayer.DETERMINISTIC,
    ),
    CaseSpec(
        Case.SETTLEMENT_TIMING,
        "Settlement timing gap",
        "Capture and credit sit T+2 business days apart, stretched further by a "
        "weekend or a bank holiday. Amount agrees; only the date looks wrong.",
        ResolutionLayer.DETERMINISTIC,
    ),
    CaseSpec(
        Case.BATCHED_PAYOUT,
        "Batched payout",
        "Several settlement rows are paid out as a single bank credit that carries "
        "no reference to join on. No single order explains the amount; a subset of "
        "them explains it exactly.",
        ResolutionLayer.TOOL,
    ),
    CaseSpec(
        Case.NETTED_REFUND,
        "Refund netted into the batch",
        "A refund is deducted from the same payout rather than debited "
        "separately, so the credit is smaller than the sum of its payments and no "
        "subset of payments alone can reach it.",
        ResolutionLayer.AI,
    ),
    CaseSpec(
        Case.PARTIAL_REFUND,
        "Partial refund mid-cycle",
        "Part of an order is refunded after capture but before settlement, so "
        "neither the gross nor the net order amount matches the credit.",
        ResolutionLayer.AI,
    ),
    CaseSpec(
        Case.CHARGEBACK,
        "Chargeback deduction",
        "A dispute reverses an earlier cycle's payment inside this cycle's "
        "payout. The offsetting payment is not in this batch at all.",
        ResolutionLayer.AI,
    ),
    CaseSpec(
        Case.ROUNDING_DRIFT,
        "Paise rounding drift",
        "Merchant and gateway round the fee independently and disagree by one or "
        "two paise. Within tolerance, so it is a match, not an exception.",
        ResolutionLayer.DETERMINISTIC,
    ),
    CaseSpec(
        Case.DUPLICATE_PAYMENT,
        "Duplicate payment on one order",
        "One order carries two payment ids -- a retry that both succeeded. Only "
        "one was settled; the other needs a refund, not a match.",
        ResolutionLayer.AI,
    ),
    CaseSpec(
        Case.AMOUNT_COLLISION,
        "Amount collision (adversarial)",
        "Two unrelated settlements sum to exactly the same figure as a third "
        "unrelated settlement. Both readings of the bank credit are arithmetically "
        "perfect, nothing in the data decides between them, and only one is true. "
        "Auto-resolving this is the expensive mistake, so the correct answer is to "
        "escalate.",
        ResolutionLayer.ESCALATE,
        adversarial=True,
    ),
    CaseSpec(
        Case.UTR_REUSE,
        "UTR reuse across cycles (adversarial)",
        "A bank reference number reappears on a later credit with a plausible "
        "amount. The identifier says match; the calendar says it cannot be. "
        "Identifier evidence must not outrank contradictory evidence.",
        ResolutionLayer.ESCALATE,
        adversarial=True,
    ),
)

CASE_SPECS: dict[Case, CaseSpec] = {spec.case: spec for spec in _SPECS}

ADVERSARIAL_CASES: frozenset[Case] = frozenset(spec.case for spec in _SPECS if spec.adversarial)


def spec_for(case_id: int) -> CaseSpec:
    """Look up a case spec, failing loudly on an id outside the taxonomy."""
    try:
        return CASE_SPECS[Case(case_id)]
    except ValueError as exc:  # Case(...) raises on an unknown id
        raise ValueError(f"case_id {case_id!r} is not in the 11-case taxonomy") from exc

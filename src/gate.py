"""The resolution gate: what the system is willing to book without a human.

The classifier produces a diagnosis and a confidence. The gate decides whether
that is good enough, and **the gate does not trust the confidence on its own.**
A model's stated certainty is a self-report, and self-reports are exactly what
fail on the case designed to fool it: an amount collision looks *easy*, because
the arithmetic closes perfectly. High confidence there is not a bug in the
prompt, it is the honest output of a system that cannot see what it is missing.

So confidence is the last check, not the first. Before it:

1. **The arithmetic is re-verified.** The rows the model claims must actually sum
   to the credit. This is checked deterministically, here, against the same money
   functions the matcher uses. A model that names the right case and the wrong
   rows does not get booked.
2. **Evidence-level ambiguity vetoes any confidence.** If the tools proved that
   two or more structurally valid readings exist that share no rows -- each a
   complete settlement batch -- nothing may auto-resolve. This is derived from the
   tool trace, not from the model's opinion, so it holds even when the model
   fails to notice the ambiguity itself. It is the backstop that makes "case 10 is
   never auto-resolved" a property of the system rather than a hope about the model.
3. **Named adversarial cases never auto-resolve**, regardless of confidence.

Only then does the threshold apply. Tuned on seed_A, reported on seed_B.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from config import (
    ALWAYS_ESCALATE_CASES,
    AUTO_RESOLVE_THRESHOLD,
    ROUNDING_TOLERANCE_PAISE,
    VETO_ON_AMBIGUOUS_EVIDENCE,
)
from src.classifier import RESOLUTION_ESCALATE, RESOLUTION_MATCHED, Diagnosis

LAYER = "ai"

RULE_MODEL_ESCALATED = "model_escalated"
RULE_NO_ROWS_CLAIMED = "no_rows_claimed"
RULE_ARITHMETIC_FAILS = "claimed_rows_do_not_sum"
RULE_AMBIGUOUS_EVIDENCE = "ambiguous_evidence_veto"
RULE_ALWAYS_ESCALATE = "always_escalate_case"
RULE_BELOW_THRESHOLD = "below_confidence_threshold"
RULE_AUTO_RESOLVED = "auto_resolved"


RULE_DOCS: dict[str, str] = {
    RULE_MODEL_ESCALATED: (
        "The classifier investigated and declined to attribute the credit. Its "
        "reasoning is attached."
    ),
    RULE_NO_ROWS_CLAIMED: (
        "The classifier said 'matched' but named no settlement rows, so there is nothing to book."
    ),
    RULE_ARITHMETIC_FAILS: (
        "The rows the classifier named do not sum to the credit. Re-checked here "
        "against the same money functions the deterministic matcher uses -- a "
        "diagnosis that does not survive re-checking is never booked, whatever "
        "confidence it carries."
    ),
    RULE_AMBIGUOUS_EVIDENCE: (
        "The tools found two complete settlement batches that each explain this "
        "credit exactly and share no rows. Nothing in the data chooses between "
        "them, so no stated confidence is high enough. This is derived from the "
        "evidence rather than from the model's opinion of itself, which is why it "
        "holds even when the classifier misdiagnoses the case."
    ),
    RULE_ALWAYS_ESCALATE: (
        "The case is on the never-auto-resolve list. On these constructions the "
        "classifier being confident is itself the failure being guarded against."
    ),
    RULE_BELOW_THRESHOLD: (
        "The classifier was not confident enough to book without review. A low "
        "number here is a legitimate result, not a failure."
    ),
    RULE_AUTO_RESOLVED: (
        "Diagnosed, re-verified arithmetically, unambiguous, and above the "
        "confidence threshold. Booked without review."
    ),
}


@dataclass(frozen=True)
class Decision:
    bank_txn_id: str
    auto_resolved: bool
    rule: str
    reason: str
    case_id: int | None
    confidence: float
    settlement_entity_ids: tuple[str, ...]
    layer: str = LAYER

    @property
    def resolution(self) -> str:
        return RESOLUTION_MATCHED if self.auto_resolved else RESOLUTION_ESCALATE


def disjoint_complete_readings(diagnosis: Diagnosis) -> list[list[str]]:
    """Structurally valid readings of the credit that share no rows.

    Pulled out of the tool trace rather than recomputed, so the veto is grounded
    in evidence the model actually saw. Two complete settlement batches that both
    sum to the credit and overlap in nothing is the signature of an amount
    collision -- and there is no field left to break the tie.
    """
    for call in diagnosis.tool_calls:
        if call.name != "subset_sum" or not isinstance(call.result, dict):
            continue
        solutions = call.result.get("solutions") or []
        structures = call.result.get("solution_structure") or []
        complete = [
            tuple(solution)
            for solution, structure in zip(solutions, structures, strict=False)
            if isinstance(structure, dict) and structure.get("is_complete_batch")
        ]
        for left, right in combinations(complete, 2):
            if not set(left) & set(right):
                return [list(left), list(right)]
    return []


def decide(
    diagnosis: Diagnosis,
    *,
    credit_paise: int,
    net_paise_of: Callable[[Sequence[str]], int | None],
    threshold: float = AUTO_RESOLVE_THRESHOLD,
) -> Decision:
    """Apply the gate to one diagnosis.

    `net_paise_of` returns what the claimed rows actually net, or None if any id
    is unknown. It is injected rather than imported so the gate stays a pure
    function and the tests can drive it without a database.
    """

    def escalate(rule: str, reason: str) -> Decision:
        return Decision(
            bank_txn_id=diagnosis.bank_txn_id,
            auto_resolved=False,
            rule=rule,
            reason=reason,
            case_id=diagnosis.case_id,
            confidence=diagnosis.confidence,
            settlement_entity_ids=diagnosis.settlement_entity_ids,
        )

    if diagnosis.resolution != RESOLUTION_MATCHED:
        return escalate(
            RULE_MODEL_ESCALATED,
            diagnosis.reasoning or "the classifier declined to resolve this credit",
        )

    claimed = diagnosis.settlement_entity_ids
    if not claimed:
        return escalate(
            RULE_NO_ROWS_CLAIMED,
            "claimed a match but named no settlement rows, so there is nothing to book",
        )

    actual = net_paise_of(claimed)
    if actual is None:
        return escalate(
            RULE_ARITHMETIC_FAILS,
            f"claimed settlement rows {sorted(claimed)} include ids that are not "
            "available to be matched",
        )
    drift = credit_paise - actual
    if abs(drift) > ROUNDING_TOLERANCE_PAISE:
        return escalate(
            RULE_ARITHMETIC_FAILS,
            f"claimed rows net {actual}p against a credit of {credit_paise}p, off by "
            f"{drift:+d}p -- the diagnosis does not survive re-checking the arithmetic",
        )

    if VETO_ON_AMBIGUOUS_EVIDENCE:
        readings = disjoint_complete_readings(diagnosis)
        if readings:
            return escalate(
                RULE_AMBIGUOUS_EVIDENCE,
                "the tools found two complete settlement batches that each explain "
                f"this credit exactly and share no rows -- {readings[0]} and "
                f"{readings[1]}. Nothing in the data chooses between them, so no "
                "confidence is high enough to book one",
            )

    if diagnosis.case_id in {int(c) for c in ALWAYS_ESCALATE_CASES}:
        return escalate(
            RULE_ALWAYS_ESCALATE,
            f"case {diagnosis.case_id} is on the never-auto-resolve list; the "
            f"classifier's {diagnosis.confidence:.2f} confidence is itself the risk "
            "being guarded against",
        )

    if diagnosis.confidence < threshold:
        return escalate(
            RULE_BELOW_THRESHOLD,
            f"confidence {diagnosis.confidence:.2f} is below the {threshold:.2f} "
            f"auto-resolve threshold. {diagnosis.reasoning}",
        )

    return Decision(
        bank_txn_id=diagnosis.bank_txn_id,
        auto_resolved=True,
        rule=RULE_AUTO_RESOLVED,
        reason=(
            f"case {diagnosis.case_id} at {diagnosis.confidence:.2f} confidence; "
            f"claimed rows net {actual}p against a {credit_paise}p credit "
            f"({drift:+d}p). {diagnosis.reasoning}"
        ),
        case_id=diagnosis.case_id,
        confidence=diagnosis.confidence,
        settlement_entity_ids=claimed,
    )


def net_paise_lookup(rows: dict[str, Any]) -> Callable[[Sequence[str]], int | None]:
    """Build a `net_paise_of` from an `entity_id -> SettlementRow` mapping."""

    def lookup(entity_ids: Sequence[str]) -> int | None:
        try:
            return sum(rows[eid].net_paise for eid in entity_ids)
        except KeyError:
            return None

    return lookup

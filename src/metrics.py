"""Scoring, and the report a judge actually reads.

**False-match rate is printed first, before the match rate.** That ordering is
the whole argument of the project. A reconciliation tool can reach any match
rate you like by lowering its standard of evidence, and the cost of a wrong
match is not symmetric with the cost of an unresolved one: an exception costs a
controller ten minutes, a false match closes a discrepancy that was real. So the
headline number is the one that can only get worse when we get greedier.

This is the only module permitted to load `truth.json`. Everything upstream of
it works from the three files a merchant would actually hand over.

Every number in `docs/metrics.md` and the README is emitted from here. Nothing
about this dataset is typed by hand into a document -- `make metrics` regenerates
it, and a document that disagrees with the code is a bug we want CI to catch.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from config import (
    COST_INR_PER_MTOK_INPUT,
    COST_INR_PER_MTOK_OUTPUT,
)
from src.cases import ResolutionLayer, spec_for
from src.ingest import load_truth
from src.matcher import Match
from src.money import format_inr
from src.pipeline import RunResult, run_reconciliation, seed_dir
from src.schema import TruthRecord

# Calibration buckets. A model claiming 0.9 across a bucket that turns out 0.6
# correct is miscalibrated even if its accuracy looks fine in aggregate, and the
# gate's threshold is only meaningful if the number it thresholds means something.
CALIBRATION_BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 0.95), (0.95, 1.01))


@dataclass(frozen=True)
class FalseMatch:
    """A match the system was wrong to make. There should be none of these."""

    bank_txn_id: str
    rule: str
    claimed_order_ids: tuple[str, ...]
    truth_order_ids: tuple[str, ...]
    truth_case_id: int | None
    why: str


@dataclass(frozen=True)
class Unresolved:
    bank_txn_id: str
    rule: str
    reason: str
    credit_paise: int
    candidate_count: int
    planted_case_id: int | None
    planted_expectation: str | None


@dataclass(frozen=True)
class CaseScore:
    case_id: int
    name: str
    resolvable_by: str
    units: int
    orders: int
    orders_matched: int
    units_resolved_correctly: int


@dataclass(frozen=True)
class CalibrationBucket:
    low: float
    high: float
    n: int
    mean_confidence: float
    observed_accuracy: float

    @property
    def gap(self) -> float:
        return self.mean_confidence - self.observed_accuracy


@dataclass(frozen=True)
class AIReport:
    """How the classifier and the gate performed, scored separately from the rules.

    Diagnosis accuracy and auto-resolve precision are deliberately different
    numbers. A model can name the right case and claim the wrong rows, or name
    the wrong case and still be refused by the gate for the right reason. Rolling
    them into one figure would hide which half of the system is working.
    """

    exceptions_seen: int
    diagnoses_correct: int
    auto_resolved: int
    auto_resolved_correct: int
    escalated: int
    escalations_by_rule: tuple[tuple[str, int], ...]
    adversarial_seen: int
    adversarial_escalated: int
    calibration: tuple[CalibrationBucket, ...]
    input_tokens: int
    output_tokens: int
    llm_turns: int
    model: str
    backend: str

    @property
    def diagnosis_accuracy(self) -> float:
        return self.diagnoses_correct / self.exceptions_seen if self.exceptions_seen else 0.0

    @property
    def auto_resolve_precision(self) -> float:
        return self.auto_resolved_correct / self.auto_resolved if self.auto_resolved else 1.0

    @property
    def cost_inr(self) -> float:
        return (
            self.input_tokens * COST_INR_PER_MTOK_INPUT
            + self.output_tokens * COST_INR_PER_MTOK_OUTPUT
        ) / 1_000_000


@dataclass(frozen=True)
class Report:
    seed: str
    run_id: str
    orders: int
    settlement_rows: int
    bank_lines: int
    match_count: int
    matched_orders: int
    false_matches: tuple[FalseMatch, ...]
    unresolved: tuple[Unresolved, ...]
    per_case: tuple[CaseScore, ...]
    unclaimed_entity_ids: tuple[str, ...]
    escalation_required: int
    escalation_achieved: int
    ai: AIReport | None = None

    @property
    def false_match_rate(self) -> float:
        if self.match_count == 0:
            return 0.0
        return len(self.false_matches) / self.match_count

    @property
    def match_rate(self) -> float:
        return self.matched_orders / self.orders if self.orders else 0.0

    @property
    def unresolved_orders(self) -> int:
        return self.orders - self.matched_orders


def _truth_by_bank(truth: tuple[TruthRecord, ...]) -> dict[str, TruthRecord]:
    index: dict[str, TruthRecord] = {}
    for record in truth:
        for txn_id in record.bank_txn_ids:
            index[txn_id] = record
    return index


def _judge(match: Match, truth: TruthRecord | None) -> str | None:
    """Return why this match is wrong, or None if it is right.

    A match is only correct if it claims *exactly* the right rows. Getting the
    bank line into the right bucket while attributing it to the wrong orders is
    still a wrong answer -- it is the answer that puts a controller on the wrong
    trail, and scoring it as a win is how a system flatters itself.
    """
    if truth is None:
        return "matched a bank line that no truth record covers"
    if truth.expected_resolution != "matched":
        return f"case {truth.case_id} must be escalated, not matched"
    if set(match.order_ids) != set(truth.order_ids):
        return (
            f"claimed orders {sorted(match.order_ids)} but the credit belongs to "
            f"{sorted(truth.order_ids)}"
        )
    if set(match.settlement_entity_ids) != set(truth.settlement_ids):
        return (
            f"claimed settlement rows {sorted(match.settlement_entity_ids)} but the "
            f"credit belongs to {sorted(truth.settlement_ids)}"
        )
    return None


def score(result: RunResult, truth: tuple[TruthRecord, ...]) -> Report:
    """Score what the system as a whole concluded.

    `result.outcome` is the deterministic layer alone when the AI layer did not
    run, and both layers combined when it did. Deliberately the same function
    either way: a false match booked by the classifier is counted against the
    identical headline number as one booked by a rule.
    """
    reconciliation = result.outcome
    truth_by_bank = _truth_by_bank(truth)
    matched_orders = reconciliation.matched_order_ids
    matched_bank = reconciliation.matched_bank_txn_ids

    false_matches = tuple(
        FalseMatch(
            bank_txn_id=match.bank_txn_id,
            rule=match.rule,
            claimed_order_ids=match.order_ids,
            truth_order_ids=(
                truth_by_bank[match.bank_txn_id].order_ids
                if match.bank_txn_id in truth_by_bank
                else ()
            ),
            truth_case_id=(
                truth_by_bank[match.bank_txn_id].case_id
                if match.bank_txn_id in truth_by_bank
                else None
            ),
            why=why,
        )
        for match in reconciliation.matches
        if (why := _judge(match, truth_by_bank.get(match.bank_txn_id))) is not None
    )

    unresolved = tuple(
        Unresolved(
            bank_txn_id=exception.bank_txn_id,
            rule=exception.rule,
            reason=exception.reason,
            credit_paise=exception.credit_paise,
            candidate_count=len(exception.candidate_entity_ids),
            planted_case_id=(
                truth_by_bank[exception.bank_txn_id].case_id
                if exception.bank_txn_id in truth_by_bank
                else None
            ),
            planted_expectation=(
                truth_by_bank[exception.bank_txn_id].expected_resolution
                if exception.bank_txn_id in truth_by_bank
                else None
            ),
        )
        for exception in reconciliation.exceptions
    )

    # --- per case -----------------------------------------------------------
    buckets: dict[int, list[TruthRecord]] = defaultdict(list)
    for record in truth:
        buckets[record.case_id].append(record)

    per_case: list[CaseScore] = []
    escalation_required = 0
    escalation_achieved = 0
    for case_id, records in sorted(buckets.items()):
        spec = spec_for(case_id)
        orders = sum(len(r.order_ids) for r in records)
        orders_hit = sum(len(set(r.order_ids) & matched_orders) for r in records)

        resolved = 0
        for record in records:
            claimed = set(record.bank_txn_ids) & matched_bank
            if record.expected_resolution == "matched":
                # right only if every bank line of the unit was matched, and
                # matched correctly -- a false match is not a resolution.
                correct = claimed == set(record.bank_txn_ids) and not any(
                    fm.bank_txn_id in record.bank_txn_ids for fm in false_matches
                )
            else:
                escalation_required += 1
                correct = not claimed
                escalation_achieved += int(correct)
            resolved += int(correct)

        per_case.append(
            CaseScore(
                case_id=case_id,
                name=spec.name,
                resolvable_by=spec.resolvable_by.value,
                units=len(records),
                orders=orders,
                orders_matched=orders_hit,
                units_resolved_correctly=resolved,
            )
        )

    ai_report = _score_ai(result, truth_by_bank) if result.ai_ran else None

    return Report(
        seed=result.seed,
        run_id=result.run_id,
        orders=len(result.dataset.orders),
        settlement_rows=len(result.dataset.settlements),
        bank_lines=len(result.dataset.bank),
        match_count=len(reconciliation.matches),
        matched_orders=len(matched_orders),
        false_matches=false_matches,
        unresolved=unresolved,
        per_case=tuple(per_case),
        unclaimed_entity_ids=reconciliation.unclaimed_entity_ids,
        escalation_required=escalation_required,
        escalation_achieved=escalation_achieved,
        ai=ai_report,
    )


def _score_ai(result: RunResult, truth_by_bank: dict[str, TruthRecord]) -> AIReport:
    """Score the AI layer on its own terms.

    A diagnosis is correct when the model names the case the generator planted.
    An auto-resolution is correct when the rows it booked are exactly the rows
    the credit actually paid -- the same standard the deterministic layer is
    held to, and deliberately stricter than "landed in the matched bucket".
    """
    diagnoses = {d.bank_txn_id: d for d in result.diagnoses}
    correct_diagnoses = 0
    auto_resolved = 0
    auto_correct = 0
    escalated = 0
    escalation_rules: Counter[str] = Counter()
    adversarial_seen = 0
    adversarial_escalated = 0
    scored: list[tuple[float, bool]] = []

    for decision in result.decisions:
        record = truth_by_bank.get(decision.bank_txn_id)
        diagnosis = diagnoses[decision.bank_txn_id]

        if record is not None and diagnosis.case_id == record.case_id:
            correct_diagnoses += 1

        must_escalate = record is not None and record.expected_resolution != "matched"
        if must_escalate:
            adversarial_seen += 1
            adversarial_escalated += int(not decision.auto_resolved)

        if decision.auto_resolved:
            auto_resolved += 1
            booked_right = (
                record is not None
                and record.expected_resolution == "matched"
                and set(decision.settlement_entity_ids) == set(record.settlement_ids)
            )
            auto_correct += int(booked_right)
            scored.append((diagnosis.confidence, booked_right))
        else:
            escalated += 1
            escalation_rules[decision.rule] += 1
            # An escalation is "right" when the item genuinely could not be booked.
            scored.append((diagnosis.confidence, must_escalate))

    buckets: list[CalibrationBucket] = []
    for low, high in CALIBRATION_BUCKETS:
        inside = [(c, ok) for c, ok in scored if low <= c < high]
        if not inside:
            continue
        buckets.append(
            CalibrationBucket(
                low=low,
                high=high,
                n=len(inside),
                mean_confidence=sum(c for c, _ in inside) / len(inside),
                observed_accuracy=sum(1 for _, ok in inside if ok) / len(inside),
            )
        )

    first = result.diagnoses[0] if result.diagnoses else None
    return AIReport(
        exceptions_seen=len(result.decisions),
        diagnoses_correct=correct_diagnoses,
        auto_resolved=auto_resolved,
        auto_resolved_correct=auto_correct,
        escalated=escalated,
        escalations_by_rule=tuple(sorted(escalation_rules.items())),
        adversarial_seen=adversarial_seen,
        adversarial_escalated=adversarial_escalated,
        calibration=tuple(buckets),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        llm_turns=sum(d.turns for d in result.diagnoses),
        model=first.model if first else "n/a",
        backend=first.backend if first else "n/a",
    )


def evaluate(
    seed: str,
    *,
    db_path: Path | str = "ledger.db",
    use_ai: bool = False,
    threshold: float | None = None,
) -> Report:
    kwargs: dict[str, object] = {"db_path": db_path, "use_ai": use_ai}
    if threshold is not None:
        kwargs["threshold"] = threshold
    result = run_reconciliation(seed, **kwargs)
    return score(result, load_truth(seed_dir(seed) / "truth.json"))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_text(report: Report) -> str:
    layer = "deterministic + AI" if report.ai else "deterministic layer"
    lines = [
        f"Ledger Sentinel -- {layer}, seed_{report.seed} ({report.run_id})",
        "=" * 72,
        "",
        f"  FALSE-MATCH RATE      {_pct(report.false_match_rate)}   "
        f"({len(report.false_matches)} of {report.match_count} matches wrong)",
        f"  match rate            {_pct(report.match_rate)}   "
        f"({report.matched_orders} of {report.orders} orders)",
        f"  must-escalate cases   {report.escalation_achieved}/{report.escalation_required} "
        "correctly escalated",
        "",
        f"  {report.orders} orders | {report.settlement_rows} settlement rows | "
        f"{report.bank_lines} bank lines | {len(report.unresolved)} exceptions",
        "",
        "Per case",
        "-" * 72,
        f"  {'case':>4}  {'name':<30} {'layer':<14} {'orders':>10}  units ok",
    ]
    for case in report.per_case:
        lines.append(
            f"  {case.case_id:>4}  {case.name:<30} {case.resolvable_by:<14} "
            f"{case.orders_matched:>4}/{case.orders:<5}  "
            f"{case.units_resolved_correctly}/{case.units}"
        )

    if report.ai:
        ai = report.ai
        lines += [
            "",
            f"AI layer ({ai.model} via {ai.backend})",
            "-" * 72,
            f"  auto-resolve precision  {_pct(ai.auto_resolve_precision)}   "
            f"({ai.auto_resolved_correct} of {ai.auto_resolved} booked correctly)",
            f"  diagnosis accuracy      {_pct(ai.diagnosis_accuracy)}   "
            f"({ai.diagnoses_correct} of {ai.exceptions_seen} cases named correctly)",
            f"  must-escalate refused   {ai.adversarial_escalated}/{ai.adversarial_seen}",
            f"  escalated               {ai.escalated} of {ai.exceptions_seen}",
        ]
        for rule, count in ai.escalations_by_rule:
            lines.append(f"      {count:>2}x {rule}")
        lines += [
            f"  {ai.llm_turns} model turns | {ai.input_tokens} in / {ai.output_tokens} out "
            f"tokens | approx Rs {ai.cost_inr:.2f}",
        ]
        if ai.calibration:
            lines += ["", "  Calibration -- stated confidence against what happened", ""]
            lines.append(f"      {'bucket':<12} {'n':>3}  {'stated':>7} {'actual':>7}  gap")
            for bucket in ai.calibration:
                lines.append(
                    f"      {bucket.low:.2f}-{bucket.high:.2f}  {bucket.n:>3}  "
                    f"{bucket.mean_confidence:>7.2f} {bucket.observed_accuracy:>7.2f}  "
                    f"{bucket.gap:+.2f}"
                )

    lines += ["", "Unresolved -- every one with a reason", "-" * 72]
    if not report.unresolved:
        lines.append("  (none)")
    for item in report.unresolved:
        planted = f"case {item.planted_case_id}" if item.planted_case_id else "unlabelled"
        lines += [
            f"  {item.bank_txn_id}  {format_inr(item.credit_paise):>16}  {item.rule}"
            f"   [ground truth: {planted}, {item.planted_expectation}]",
            f"      {item.reason}",
        ]

    if report.false_matches:
        lines += ["", "FALSE MATCHES", "-" * 72]
        for bad in report.false_matches:
            lines += [f"  {bad.bank_txn_id}  rule={bad.rule}", f"      {bad.why}"]

    lines += [
        "",
        f"{len(report.unclaimed_entity_ids)} settlement rows carry no matched bank credit "
        "in this export;",
        "they are offered as candidates to the unresolved credits above.",
        "",
    ]
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """`docs/metrics.md`, regenerated by `make metrics`. Never hand-edited."""
    lines = [
        "# Metrics",
        "",
        "Generated by `make metrics`. Do not edit by hand.",
        "",
        f"Layer: **deterministic only** (M2). Seed: **seed_{report.seed}**, held out from "
        "all tuning.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| **False-match rate** | **{_pct(report.false_match_rate)}** "
        f"({len(report.false_matches)}/{report.match_count}) |",
        f"| Match rate | {_pct(report.match_rate)} "
        f"({report.matched_orders}/{report.orders} orders) |",
        f"| Must-escalate cases correctly escalated | "
        f"{report.escalation_achieved}/{report.escalation_required} |",
        f"| Exceptions raised | {len(report.unresolved)} |",
        f"| Unresolved orders | {report.unresolved_orders} |",
        "",
    ]
    if report.ai:
        ai = report.ai
        lines += [
            "## AI layer",
            "",
            f"Model `{ai.model}`, served by the **{ai.backend}** backend, temperature 0.",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| **Auto-resolve precision** | **{_pct(ai.auto_resolve_precision)}** "
            f"({ai.auto_resolved_correct}/{ai.auto_resolved}) |",
            f"| Diagnosis accuracy | {_pct(ai.diagnosis_accuracy)} "
            f"({ai.diagnoses_correct}/{ai.exceptions_seen}) |",
            f"| Must-escalate cases refused | {ai.adversarial_escalated}/{ai.adversarial_seen} |",
            f"| Escalated | {ai.escalated}/{ai.exceptions_seen} |",
            f"| Model turns | {ai.llm_turns} |",
            f"| Tokens | {ai.input_tokens} in / {ai.output_tokens} out |",
            f"| Cost | approx Rs {ai.cost_inr:.2f} for {report.orders} records |",
            "",
            "### Why each escalation happened",
            "",
            "| Gate rule | Count |",
            "|---|---:|",
        ]
        for rule, count in ai.escalations_by_rule:
            lines.append(f"| `{rule}` | {count} |")
        if ai.calibration:
            lines += [
                "",
                "### Calibration",
                "",
                "| Confidence bucket | n | Stated | Observed | Gap |",
                "|---|---:|---:|---:|---:|",
            ]
            for bucket in ai.calibration:
                lines.append(
                    f"| {bucket.low:.2f}-{bucket.high:.2f} | {bucket.n} | "
                    f"{bucket.mean_confidence:.2f} | {bucket.observed_accuracy:.2f} | "
                    f"{bucket.gap:+.2f} |"
                )
        lines.append("")
    lines += [
        "## Per case",
        "",
        "| Case | Name | Resolved by | Orders matched | Units correct |",
        "|---:|---|---|---:|---:|",
    ]
    for case in report.per_case:
        lines.append(
            f"| {case.case_id} | {case.name} | {case.resolvable_by} | "
            f"{case.orders_matched}/{case.orders} | "
            f"{case.units_resolved_correctly}/{case.units} |"
        )

    ai_orders = sum(
        c.orders for c in report.per_case if c.resolvable_by != ResolutionLayer.DETERMINISTIC.value
    )
    lines += [
        "",
        "## What is left",
        "",
        f"{ai_orders} of {report.orders} orders sit behind bank credits that carry no",
        "reference to join on. The deterministic layer will not guess a combination for",
        "them, so they are exceptions by design rather than by failure -- M3 is what",
        "resolves them, and the adversarial case is what it is allowed to refuse.",
        "",
        "| Bank line | Amount | Rule | Candidates | Ground truth |",
        "|---|---:|---|---:|---|",
    ]
    for item in report.unresolved:
        planted = f"case {item.planted_case_id} ({item.planted_expectation})"
        lines.append(
            f"| {item.bank_txn_id} | {format_inr(item.credit_paise)} | `{item.rule}` | "
            f"{item.candidate_count} | {planted} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the pipeline and score it.")
    parser.add_argument("--seed", default="B", help="which seed to report on (default: B)")
    parser.add_argument("--db", type=Path, default=Path("ledger.db"))
    parser.add_argument(
        "--ai",
        action="store_true",
        help="also run the classifier and the gate (replays the committed cache offline)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="override the auto-resolve threshold; for tuning on seed_A only",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("docs/metrics.md"),
        help="where to write the generated metrics document",
    )
    parser.add_argument("--no-markdown", action="store_true")
    args = parser.parse_args()

    report = evaluate(args.seed, db_path=args.db, use_ai=args.ai, threshold=args.threshold)
    print(render_text(report))

    if not args.no_markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {args.markdown}")


if __name__ == "__main__":
    main()

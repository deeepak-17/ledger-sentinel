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
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.cases import ResolutionLayer, spec_for
from src.ingest import load_truth
from src.matcher import Match
from src.money import format_inr
from src.pipeline import RunResult, run_reconciliation, seed_dir
from src.schema import TruthRecord


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
    )


def evaluate(seed: str, *, db_path: Path | str = "ledger.db") -> Report:
    result = run_reconciliation(seed, db_path=db_path)
    return score(result, load_truth(seed_dir(seed) / "truth.json"))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_text(report: Report) -> str:
    lines = [
        f"Ledger Sentinel -- deterministic layer, seed_{report.seed} ({report.run_id})",
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
        "--markdown",
        type=Path,
        default=Path("docs/metrics.md"),
        help="where to write the generated metrics document",
    )
    parser.add_argument("--no-markdown", action="store_true")
    args = parser.parse_args()

    report = evaluate(args.seed, db_path=args.db)
    print(render_text(report))

    if not args.no_markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {args.markdown}")


if __name__ == "__main__":
    main()

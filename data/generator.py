"""Synthetic dataset generator with published ground truth.

Run: `python -m data.generator --seed A`

Two independent seeds exist for one reason: thresholds are tuned on seed_A and
every reported number comes from seed_B, which is never inspected during tuning.
The generator is committed so a reader can audit the difficulty of the data
rather than take the match rate on trust.

Determinism is a hard requirement -- regenerating a seed must produce
byte-identical files, which CI asserts with `git diff --exit-code`.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections.abc import Callable
from datetime import date
from pathlib import Path

from config import ACTIVE_CASES
from data.ids import IdMinter
from data.scenarios import (
    Context,
    Unit,
    build_amount_collision,
    build_batched_payout,
    build_clean_match,
    build_fee_and_gst,
    build_netted_refund,
    build_rounding_drift,
    build_settlement_timing,
)
from src.cases import Case, ResolutionLayer, spec_for
from src.schema import BankTxn, Dataset, TruthRecord

DATA_DIR = Path(__file__).parent

# Each seed gets its own RNG stream and its own settlement cycle, so the two
# datasets share a shape but no values.
SEED_CONFIG: dict[str, tuple[int, date]] = {
    "A": (1001, date(2026, 8, 3)),
    "B": (2002, date(2026, 9, 7)),
}

Builder = Callable[[Context], Unit]

# How the 80 records are spread across the taxonomy. The last column is orders
# per unit, which lets the plan convert a record budget into a unit count
# without guessing.
PLAN: tuple[tuple[Case, Builder, int, int], ...] = (
    (Case.CLEAN_MATCH, build_clean_match, 18, 1),
    (Case.FEE_AND_GST, build_fee_and_gst, 22, 1),
    (Case.SETTLEMENT_TIMING, build_settlement_timing, 10, 1),
    (Case.ROUNDING_DRIFT, build_rounding_drift, 6, 1),
    (Case.BATCHED_PAYOUT, build_batched_payout, 4, 3),
    (Case.NETTED_REFUND, build_netted_refund, 3, 2),
    (Case.AMOUNT_COLLISION, build_amount_collision, 2, 3),
)


def _is_active(case: Case) -> bool:
    return int(case) in ACTIVE_CASES


def planned_record_count() -> int:
    """Orders emitted by the active plan."""
    return sum(units * per_unit for case, _, units, per_unit in PLAN if _is_active(case))


def build_dataset(seed_name: str) -> Dataset:
    seed_int, cycle_start = SEED_CONFIG[seed_name]
    ctx = Context(
        rng=random.Random(seed_int),
        ids=IdMinter(random.Random(seed_int + 1)),
        cycle_start=cycle_start,
        _truth_counter=[0],
        _bank_counter=[0],
    )

    units: list[Unit] = []
    for case, builder, unit_count, _ in PLAN:
        if not _is_active(case):
            continue
        units.extend(builder(ctx) for _ in range(unit_count))

    orders = [o for u in units for o in u.orders]
    settlements = [s for u in units for s in u.settlements]
    bank = [b for u in units for b in u.bank]
    truth = [u.truth for u in units]

    if len(orders) != planned_record_count():
        raise AssertionError(
            f"builders emitted {len(orders)} orders but PLAN promises "
            f"{planned_record_count()}; a builder is not honouring its unit size"
        )

    orders.sort(key=lambda o: (o.captured_at, o.order_id))
    settlements.sort(key=lambda s: (s.settled_at, s.settlement_id, s.entity_id))
    bank, truth = _renumber_bank(bank, truth)

    return Dataset(
        seed=seed_name,
        orders=tuple(orders),
        settlements=tuple(settlements),
        bank=tuple(bank),
        truth=tuple(truth),
    )


def _renumber_bank(
    bank: list[BankTxn], truth: list[TruthRecord]
) -> tuple[list[BankTxn], list[TruthRecord]]:
    """Sort the statement into date order and give it sequential line ids, then
    repoint the truth records at the new ids. A real statement is numbered by
    when money moved, not by which case we happened to build first."""
    bank.sort(key=lambda b: (b.value_date, b.txn_id))
    remap = {row.txn_id: f"BNK{index:05d}" for index, row in enumerate(bank, start=1)}

    renumbered = [row.model_copy(update={"txn_id": remap[row.txn_id]}) for row in bank]
    repointed = [
        record.model_copy(update={"bank_txn_ids": tuple(remap[tid] for tid in record.bank_txn_ids)})
        for record in truth
    ]
    return renumbered, repointed


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

ORDER_COLUMNS = [
    "order_id",
    "payment_id",
    "amount_paise",
    "currency",
    "method",
    "status",
    "captured_at",
]
SETTLEMENT_COLUMNS = [
    "settlement_id",
    "utr",
    "entity_id",
    "entity_type",
    "order_id",
    "payment_id",
    "method",
    "amount_paise",
    "fee_paise",
    "tax_paise",
    "credit_paise",
    "debit_paise",
    "on_hold",
    "settled_at",
]
BANK_COLUMNS = ["txn_id", "value_date", "description", "utr", "credit_paise", "debit_paise"]


def _render(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _render(row[key]) for key in columns})


def write_dataset(dataset: Dataset, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "orders.csv", ORDER_COLUMNS, [o.model_dump() for o in dataset.orders])
    _write_csv(
        out_dir / "settlements.csv",
        SETTLEMENT_COLUMNS,
        [s.model_dump() for s in dataset.settlements],
    )
    _write_csv(out_dir / "bank.csv", BANK_COLUMNS, [b.model_dump() for b in dataset.bank])

    truth_payload = [record.model_dump() for record in dataset.truth]
    for record in truth_payload:
        for key in ("order_ids", "settlement_ids", "bank_txn_ids"):
            record[key] = list(record[key])
    (out_dir / "truth.json").write_text(
        json.dumps(truth_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def distribution(dataset: Dataset) -> dict[int, dict[str, int]]:
    """Per-case unit and record counts, used by data/README.md and the metrics report."""
    summary: dict[int, dict[str, int]] = {}
    for record in dataset.truth:
        entry = summary.setdefault(record.case_id, {"units": 0, "orders": 0})
        entry["units"] += 1
        entry["orders"] += len(record.order_ids)
    return dict(sorted(summary.items()))


def render_distribution_markdown() -> str:
    """Generate data/README.md from the datasets themselves.

    Hand-typing a distribution table is how a README ends up describing a dataset
    that no longer exists. This is regenerated by `make data`, and CI fails if the
    committed copy drifts.
    """
    datasets = {name: build_dataset(name) for name in sorted(SEED_CONFIG)}
    lines = [
        "# Dataset",
        "",
        "Generated by `data/generator.py`. Do not edit by hand -- run `make data`.",
        "",
        "Thresholds are tuned on **seed_A**. Every number reported anywhere in this",
        "repository comes from **seed_B**, which is not inspected during tuning.",
        "",
        "## Failure-mode distribution",
        "",
        "A *unit* is one reconciliation problem: the orders, settlement rows and bank",
        "lines that have to be resolved together. Most units are one order; batched",
        "payouts and the adversarial collision span several.",
        "",
        "| Case | Name | Resolved by | Units | Orders | Expected outcome |",
        "|---:|---|---|---:|---:|---|",
    ]

    reference = datasets["B"]
    for case_id, counts in distribution(reference).items():
        spec = spec_for(case_id)
        outcome = "escalate" if spec.resolvable_by is ResolutionLayer.ESCALATE else "matched"
        lines.append(
            f"| {case_id} | {spec.name} | {spec.resolvable_by.value} | "
            f"{counts['units']} | {counts['orders']} | {outcome} |"
        )

    lines += [
        "",
        "## Why some bank lines carry no UTR",
        "",
        "Single NEFT credits carry their UTR in the narration, so they join to the",
        "settlement report on an identifier. Consolidated payouts arrive over a",
        "different rail with a generic narration and no reference at all. That is the",
        "difficulty of this dataset and it is an honest one -- we are not deleting",
        "evidence, we are modelling a statement format that never carried it.",
        "",
        "It is also the boundary between the two layers of the system: **the",
        "deterministic matcher only closes a match when it has an identifier.** Amount",
        "agreement alone never closes anything, because amounts collide. Everything",
        "without a UTR goes to the classifier, which must reason about it and may",
        "refuse.",
        "",
    ]

    for name, dataset in datasets.items():
        no_utr = sum(1 for line in dataset.bank if line.utr is None)
        lines += [
            f"## seed_{name}",
            "",
            f"- cycle starts `{SEED_CONFIG[name][1].isoformat()}`",
            f"- {len(dataset.orders)} orders, {len(dataset.settlements)} settlement rows, "
            f"{len(dataset.bank)} bank lines",
            f"- {no_utr} of {len(dataset.bank)} bank lines carry no UTR",
            f"- {len(dataset.truth)} truth records covering every row exactly once",
            "",
        ]

    dormant = sorted(set(int(c) for c in Case) - set(ACTIVE_CASES))
    lines += [
        "## Cases defined but not yet generated",
        "",
        "The taxonomy in `src/cases.py` defines all eleven cases. "
        f"Cases {', '.join(str(c) for c in dormant)} are dormant: turning them on is a "
        "change to `ACTIVE_CASES` in `config.py` plus a builder, not a redesign.",
        "",
        "## Why amounts are in paise",
        "",
        "Razorpay's settlement report denominates `amount`, `fee` and `tax` in rupees.",
        "We deviate deliberately and suffix every money column `_paise`, holding integers.",
        "Float rupees are the single most common source of silent reconciliation error,",
        "and a column name that states its unit is cheaper than a comment nobody reads.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", choices=sorted(SEED_CONFIG))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--readme",
        action="store_true",
        help="regenerate data/README.md from the datasets instead of writing CSVs",
    )
    args = parser.parse_args()

    if args.readme:
        (DATA_DIR / "README.md").write_text(render_distribution_markdown(), encoding="utf-8")
        print("wrote data/README.md")
        return

    if not args.seed:
        parser.error("--seed is required unless --readme is given")

    dataset = build_dataset(args.seed)
    out_dir = args.out or DATA_DIR / f"seed_{args.seed}"
    write_dataset(dataset, out_dir)

    print(
        f"seed_{args.seed}: {len(dataset.orders)} orders, "
        f"{len(dataset.settlements)} settlement rows, "
        f"{len(dataset.bank)} bank lines, {len(dataset.truth)} truth records"
    )
    for case_id, counts in distribution(dataset).items():
        print(f"  case {case_id:>2}: {counts['units']:>2} units, {counts['orders']:>2} orders")


if __name__ == "__main__":
    main()

"""CSV and JSON loading into validated models.

The contract of this module is that nothing downstream ever sees a string where
it expected an integer, or a silently-empty field where it expected an
identifier. A malformed row raises here, naming the file and the line, rather
than travelling on to become an exception -- an ingestion bug that quietly
inflates the exception count would flatter every number this project reports.

Header drift is treated as a malformed file too: if the settlement export gains
or loses a column we want to know at load time, not by discovering that `utr`
has become `None` for every row.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from src.schema import BankTxn, Dataset, Order, SettlementRow, TruthRecord

T = TypeVar("T", Order, SettlementRow, BankTxn)

ORDER_COLUMNS = (
    "order_id",
    "payment_id",
    "amount_paise",
    "currency",
    "method",
    "status",
    "captured_at",
)
SETTLEMENT_COLUMNS = (
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
)
BANK_COLUMNS = ("txn_id", "value_date", "description", "utr", "credit_paise", "debit_paise")

# Columns whose empty string means "genuinely absent" rather than "bad row".
# A bulk payout really does arrive with no UTR; a refund row really has no method.
NULLABLE: dict[str, frozenset[str]] = {
    "orders.csv": frozenset(),
    "settlements.csv": frozenset({"order_id", "payment_id", "method"}),
    "bank.csv": frozenset({"utr"}),
}


class IngestError(ValueError):
    """A source file could not be read into the schema. Always names file and row."""


def _read_rows(path: Path, columns: tuple[str, ...]) -> Iterator[tuple[int, dict[str, str]]]:
    if not path.exists():
        raise IngestError(f"{path}: file not found")

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        if header != columns:
            raise IngestError(
                f"{path.name}: header is {list(header)} but this loader expects "
                f"{list(columns)}. Refusing to guess a column mapping."
            )
        # start=2 because row 1 is the header, so the number matches what an
        # editor shows when someone goes to look at the offending line.
        yield from enumerate(reader, start=2)


def _blank_to_none(row: dict[str, str], nullable: frozenset[str]) -> dict[str, object]:
    """Empty strings are only allowed where absence is meaningful. Everywhere
    else the empty string is passed through so the model rejects it loudly."""
    return {key: (None if key in nullable and value == "" else value) for key, value in row.items()}


def _load_table(path: Path, columns: tuple[str, ...], model: type[T]) -> tuple[T, ...]:
    nullable = NULLABLE[path.name]
    parsed: list[T] = []
    for line_number, row in _read_rows(path, columns):
        try:
            parsed.append(model.model_validate(_blank_to_none(row, nullable)))
        except ValidationError as exc:
            raise IngestError(f"{path.name} line {line_number}: {exc}") from exc
    if not parsed:
        raise IngestError(f"{path.name}: no data rows")
    return tuple(parsed)


def load_orders(path: Path) -> tuple[Order, ...]:
    return _load_table(path, ORDER_COLUMNS, Order)


def load_settlements(path: Path) -> tuple[SettlementRow, ...]:
    return _load_table(path, SETTLEMENT_COLUMNS, SettlementRow)


def load_bank(path: Path) -> tuple[BankTxn, ...]:
    return _load_table(path, BANK_COLUMNS, BankTxn)


def load_truth(path: Path) -> tuple[TruthRecord, ...]:
    """Ground truth is loaded by the metrics layer only.

    Nothing in the matching or classification path may import this -- a scorer
    that can see the answers is not a scorer.
    """
    if not path.exists():
        raise IngestError(f"{path}: file not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IngestError(f"{path.name}: not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise IngestError(f"{path.name}: expected a JSON array of truth records")

    records: list[TruthRecord] = []
    for index, item in enumerate(payload):
        try:
            records.append(TruthRecord.model_validate(item))
        except ValidationError as exc:
            raise IngestError(f"{path.name} record {index}: {exc}") from exc
    return tuple(records)


def load_dataset(seed_dir: Path, *, seed: str | None = None, with_truth: bool = True) -> Dataset:
    """Read one seed directory into a validated `Dataset`.

    `with_truth` exists so the pipeline can load exactly the three files a real
    merchant would hand over, and the scorer can load the fourth separately.
    """
    seed_dir = Path(seed_dir)
    name = seed if seed is not None else seed_dir.name.removeprefix("seed_")
    truth = load_truth(seed_dir / "truth.json") if with_truth else ()
    return Dataset(
        seed=name,
        orders=load_orders(seed_dir / "orders.csv"),
        settlements=load_settlements(seed_dir / "settlements.csv"),
        bank=load_bank(seed_dir / "bank.csv"),
        truth=truth,
    )

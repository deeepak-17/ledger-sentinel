"""SQLite persistence for the reconciliation run.

One file, committed nowhere, rebuilt by `make metrics`, and inspectable live:

    sqlite3 ledger.db "select rule, count(*) from exceptions group by rule"

That last property is the reason SQLite is here rather than a dict. A judge who
does not trust the printed match rate can open the database in front of us and
count the rows themselves, and the audit table lets them ask *why* about any
individual decision without reading Python.

Schema notes
------------
`matches` and `exceptions` are keyed on the bank line, because the bank
statement is the only source that is ground truth about money actually moving.
A match may claim several settlement rows and several orders, so those are held
as JSON arrays rather than a join table: they are read as a unit, never queried
element-wise, and a join table would buy nothing but ceremony here.

The `runs` table already carries the LLM columns M3 will fill. They cost
nothing empty and save a migration later.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.schema import BankTxn, Dataset, Order, SettlementRow

DEFAULT_DB_PATH = Path("ledger.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    seed             TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    orders           INTEGER NOT NULL DEFAULT 0,
    settlement_rows  INTEGER NOT NULL DEFAULT 0,
    bank_lines       INTEGER NOT NULL DEFAULT 0,
    match_count      INTEGER NOT NULL DEFAULT 0,
    exception_count  INTEGER NOT NULL DEFAULT 0,
    matched_orders   INTEGER NOT NULL DEFAULT 0,
    llm_calls        INTEGER NOT NULL DEFAULT 0,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_inr         REAL NOT NULL DEFAULT 0.0,
    model            TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    payment_id   TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    currency     TEXT NOT NULL,
    method       TEXT NOT NULL,
    status       TEXT NOT NULL,
    captured_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    entity_id     TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL,
    utr           TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    order_id      TEXT,
    payment_id    TEXT,
    method        TEXT,
    amount_paise  INTEGER NOT NULL,
    fee_paise     INTEGER NOT NULL,
    tax_paise     INTEGER NOT NULL,
    credit_paise  INTEGER NOT NULL,
    debit_paise   INTEGER NOT NULL,
    on_hold       INTEGER NOT NULL,
    settled_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_utr ON settlements(utr);
CREATE INDEX IF NOT EXISTS idx_settlements_settled_at ON settlements(settled_at);

CREATE TABLE IF NOT EXISTS bank (
    txn_id       TEXT PRIMARY KEY,
    value_date   TEXT NOT NULL,
    description  TEXT NOT NULL,
    utr          TEXT,
    credit_paise INTEGER NOT NULL,
    debit_paise  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_utr ON bank(utr);

CREATE TABLE IF NOT EXISTS matches (
    match_id              TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs(run_id),
    bank_txn_id           TEXT NOT NULL REFERENCES bank(txn_id),
    layer                 TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    settlement_entity_ids TEXT NOT NULL,
    order_ids             TEXT NOT NULL,
    credit_paise          INTEGER NOT NULL,
    expected_paise        INTEGER NOT NULL,
    delta_paise           INTEGER NOT NULL,
    confidence            REAL,
    reason                TEXT NOT NULL,
    decided_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id         TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL REFERENCES runs(run_id),
    bank_txn_id          TEXT NOT NULL REFERENCES bank(txn_id),
    layer                TEXT NOT NULL,
    rule                 TEXT NOT NULL,
    reason               TEXT NOT NULL,
    credit_paise         INTEGER NOT NULL,
    candidate_entity_ids TEXT NOT NULL,
    case_id              INTEGER,
    resolution           TEXT NOT NULL,
    confidence           REAL,
    decided_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    seq          INTEGER NOT NULL,
    layer        TEXT NOT NULL,
    rule         TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    inputs       TEXT NOT NULL,
    decided_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit(run_id, subject_id);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str = DEFAULT_DB_PATH, *, fresh: bool = False) -> sqlite3.Connection:
    """Open (and if needed create) the ledger database.

    `fresh=True` deletes the file first. `make metrics` uses it so a reported
    number can never come from a previous run's leftovers.
    """
    path = Path(path)
    if fresh and path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def as_json(values: Iterable[str]) -> str:
    """Stable JSON for the id arrays: sorted, so two equal claims serialise equally."""
    return json.dumps(sorted(values), separators=(",", ":"))


def from_json(payload: str) -> list[str]:
    return list(json.loads(payload))


# ---------------------------------------------------------------------------
# loading the three sources
# ---------------------------------------------------------------------------


def _order_row(order: Order) -> tuple[Any, ...]:
    return (
        order.order_id,
        order.payment_id,
        order.amount_paise,
        order.currency,
        order.method,
        order.status,
        order.captured_at.isoformat(),
    )


def _settlement_row(row: SettlementRow) -> tuple[Any, ...]:
    return (
        row.entity_id,
        row.settlement_id,
        row.utr,
        row.entity_type,
        row.order_id,
        row.payment_id,
        row.method,
        row.amount_paise,
        row.fee_paise,
        row.tax_paise,
        row.credit_paise,
        row.debit_paise,
        int(row.on_hold),
        row.settled_at.isoformat(),
    )


def _bank_row(row: BankTxn) -> tuple[Any, ...]:
    return (
        row.txn_id,
        row.value_date.isoformat(),
        row.description,
        row.utr,
        row.credit_paise,
        row.debit_paise,
    )


def load_dataset_into(conn: sqlite3.Connection, dataset: Dataset) -> None:
    """Write the three input sources. Ground truth is deliberately NOT stored:
    the database is what the pipeline sees, and the pipeline must not see the
    answers."""
    conn.executemany(
        "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?)",
        [_order_row(o) for o in dataset.orders],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_settlement_row(s) for s in dataset.settlements],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO bank VALUES (?,?,?,?,?,?)",
        [_bank_row(b) for b in dataset.bank],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, run_id: str, seed: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, seed, started_at) VALUES (?,?,?)",
        (run_id, seed, started_at),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, finished_at: str, **counts: Any) -> None:
    if counts:
        assignments = ", ".join(f"{column} = ?" for column in counts)
        conn.execute(
            f"UPDATE runs SET finished_at = ?, {assignments} WHERE run_id = ?",
            (finished_at, *counts.values(), run_id),
        )
    else:
        conn.execute("UPDATE runs SET finished_at = ? WHERE run_id = ?", (finished_at, run_id))
    conn.commit()


def insert_matches(conn: sqlite3.Connection, rows: Sequence[tuple[Any, ...]]) -> None:
    conn.executemany("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def insert_exceptions(conn: sqlite3.Connection, rows: Sequence[tuple[Any, ...]]) -> None:
    conn.executemany("INSERT INTO exceptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def count(conn: sqlite3.Connection, table: str, run_id: str | None = None) -> int:
    if run_id is None:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
    sql = f"SELECT count(*) FROM {table} WHERE run_id = ?"  # noqa: S608
    return int(conn.execute(sql, (run_id,)).fetchone()[0])

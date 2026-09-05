"""Wiring: three CSVs in, a populated ledger database out.

This exists so that `make metrics`, the FastAPI `/run` endpoint and the tests
all execute the *same* sequence. The moment the CLI and the API each grow their
own orchestration, one of them starts reporting a number the other cannot
reproduce.

Note what is not loaded here: `truth.json`. The pipeline reads exactly the three
files a merchant would hand over. Ground truth is loaded separately by
`src/metrics.py`, which is the only module allowed to see the answers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src import db
from src.audit import AuditLog, record_reconciliation
from src.db import as_json, utc_now
from src.ingest import load_dataset
from src.matcher import Reconciliation, reconcile
from src.schema import Dataset

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def seed_dir(seed: str) -> Path:
    return DATA_DIR / f"seed_{seed}"


@dataclass(frozen=True)
class RunResult:
    run_id: str
    seed: str
    dataset: Dataset
    reconciliation: Reconciliation
    db_path: Path


def _next_run_id(conn, seed: str) -> str:
    """Deterministic and readable: run_B_001. A uuid here would make two
    otherwise identical runs differ, which is exactly what the determinism check
    is trying to detect."""
    existing = int(conn.execute("SELECT count(*) FROM runs WHERE seed = ?", (seed,)).fetchone()[0])
    return f"run_{seed}_{existing + 1:03d}"


def run_reconciliation(
    seed: str,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
    fresh: bool = True,
    clock: Callable[[], str] = utc_now,
    source: Path | None = None,
) -> RunResult:
    """Load one seed, run the deterministic layer, persist every decision."""
    directory = Path(source) if source is not None else seed_dir(seed)
    dataset = load_dataset(directory, seed=seed, with_truth=False)

    conn = db.connect(db_path, fresh=fresh)
    try:
        db.load_dataset_into(conn, dataset)
        run_id = _next_run_id(conn, seed)
        started_at = clock()
        db.start_run(conn, run_id, seed, started_at)

        result = reconcile(dataset)

        bank_by_id = {line.txn_id: line for line in dataset.bank}
        db.insert_matches(
            conn,
            [
                (
                    f"{run_id}:M{index:04d}",
                    run_id,
                    match.bank_txn_id,
                    match.layer,
                    match.rule,
                    as_json(match.settlement_entity_ids),
                    as_json(match.order_ids),
                    match.credit_paise,
                    match.expected_paise,
                    match.delta_paise,
                    None,
                    match.reason,
                    started_at,
                )
                for index, match in enumerate(result.matches, start=1)
            ],
        )
        db.insert_exceptions(
            conn,
            [
                (
                    f"{run_id}:X{index:04d}",
                    run_id,
                    exception.bank_txn_id,
                    exception.layer,
                    exception.rule,
                    exception.reason,
                    exception.credit_paise,
                    as_json(exception.candidate_entity_ids),
                    None,
                    "escalate",
                    None,
                    started_at,
                )
                for index, exception in enumerate(result.exceptions, start=1)
            ],
        )

        log = AuditLog(conn, run_id, clock=clock)
        record_reconciliation(log, result.matches, result.exceptions, bank_by_id)

        db.finish_run(
            conn,
            run_id,
            clock(),
            orders=len(dataset.orders),
            settlement_rows=len(dataset.settlements),
            bank_lines=len(dataset.bank),
            match_count=len(result.matches),
            exception_count=len(result.exceptions),
            matched_orders=len(result.matched_order_ids),
        )
    finally:
        conn.close()

    return RunResult(
        run_id=run_id,
        seed=seed,
        dataset=dataset,
        reconciliation=result,
        db_path=Path(db_path),
    )

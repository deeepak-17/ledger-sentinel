"""Shared fixtures for the pipeline tests.

Each seed is reconciled once per test session into a throwaway database. The
run is expensive enough that repeating it per test would slow the suite, and
sharing it is safe because every assertion here is read-only.
"""

from __future__ import annotations

import pytest

from src.ingest import load_truth
from src.metrics import Report, score
from src.pipeline import RunResult, run_reconciliation, seed_dir

SEEDS = ("A", "B")


@pytest.fixture(scope="session", params=SEEDS)
def run_result(request, tmp_path_factory) -> RunResult:
    seed = request.param
    db_path = tmp_path_factory.mktemp("ledger") / f"{seed}.db"
    return run_reconciliation(seed, db_path=db_path)


@pytest.fixture(scope="session")
def report(run_result) -> Report:
    truth = load_truth(seed_dir(run_result.seed) / "truth.json")
    return score(run_result, truth)


@pytest.fixture(scope="session")
def held_out(tmp_path_factory) -> Report:
    """seed_B only -- the number that gets published."""
    db_path = tmp_path_factory.mktemp("ledger-b") / "B.db"
    result = run_reconciliation("B", db_path=db_path)
    return score(result, load_truth(seed_dir("B") / "truth.json"))

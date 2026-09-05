"""Shared fixtures for the pipeline tests.

Each seed is reconciled once per test session into a throwaway database. The
run is expensive enough that repeating it per test would slow the suite, and
sharing it is safe because every assertion here is read-only.
"""

from __future__ import annotations

import os

import pytest

from src.ingest import load_truth
from src.metrics import Report, score
from src.pipeline import RunResult, run_reconciliation, seed_dir

SEEDS = ("A", "B")


@pytest.fixture(scope="session", autouse=True)
def _never_call_the_api():
    """No test may reach the network, even on a machine that has a key.

    `src/llm.py` picks the live backend when OPENAI_API_KEY is set, which is
    correct for `make cache` and wrong for a test suite: a test that quietly
    starts spending money -- and that passes or fails depending on whether the
    developer happens to have a key -- is not a test. CI never had one, so this
    only showed up the moment a real key landed in .env.

    Blanking the variable is enough: `load_env` uses setdefault, so it will not
    overwrite this, and the backend selector treats an empty value as absent and
    falls back to replay.
    """
    previous = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = ""
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous


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

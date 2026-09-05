"""Wiring: three CSVs in, a populated ledger database out.

This exists so that `make metrics`, the FastAPI `/run` endpoint and the tests all
execute the *same* sequence. The moment the CLI and the API each grow their own
orchestration, one of them starts reporting a number the other cannot reproduce.

Two layers run in order. The deterministic matcher closes everything that carries
an identifier. Whatever it refuses goes to the classifier, and whatever the
classifier resolves has to survive the gate. The output of both layers is
expressed in the *same* `Match` and `Exception_` types, so the scorer does not
know or care which layer produced a given row -- a false match booked by the AI
layer counts against exactly the same headline number as one booked by a rule.

Note what is not loaded here: `truth.json`. The pipeline reads exactly the three
files a merchant would hand over. Ground truth is loaded separately by
`src/metrics.py`, which is the only module allowed to see the answers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from config import AUTO_RESOLVE_THRESHOLD
from src import db
from src.audit import AuditLog, record_reconciliation
from src.classifier import Diagnosis, classify
from src.db import as_json, utc_now
from src.gate import Decision, decide, net_paise_lookup
from src.ingest import load_dataset
from src.llm import Backend, default_backend
from src.matcher import Exception_, Match, Reconciliation, reconcile
from src.schema import Dataset, SettlementRow
from src.tools import ToolBox

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
    final: Reconciliation | None = None
    diagnoses: tuple[Diagnosis, ...] = ()
    decisions: tuple[Decision, ...] = ()
    ai_ran: bool = False

    @property
    def outcome(self) -> Reconciliation:
        """What the system as a whole concluded. Falls back to the deterministic
        layer when the AI layer did not run, so every consumer can read one
        attribute without asking which milestone it is looking at."""
        return self.final if self.final is not None else self.reconciliation

    @property
    def input_tokens(self) -> int:
        return sum(d.input_tokens for d in self.diagnoses)

    @property
    def output_tokens(self) -> int:
        return sum(d.output_tokens for d in self.diagnoses)


def _next_run_id(conn, seed: str) -> str:
    """Deterministic and readable: run_B_001. A uuid here would make two
    otherwise identical runs differ, which is exactly what the determinism check
    is trying to detect."""
    existing = int(conn.execute("SELECT count(*) FROM runs WHERE seed = ?", (seed,)).fetchone()[0])
    return f"run_{seed}_{existing + 1:03d}"


def _order_ids_for(rows: list[SettlementRow]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for row in rows:
        if row.order_id is not None:
            seen.setdefault(row.order_id, None)
    return tuple(seen)


def _match_from_decision(
    decision: Decision, exception: Exception_, by_id: dict[str, SettlementRow]
) -> Match:
    """An auto-resolved diagnosis, expressed as the same thing a rule produces.

    Same type, same scoring, same audit shape. The AI layer gets no softer
    accounting than the deterministic one.
    """
    rows = [by_id[eid] for eid in decision.settlement_entity_ids]
    expected = sum(row.net_paise for row in rows)
    return Match(
        bank_txn_id=decision.bank_txn_id,
        rule=f"ai:case_{decision.case_id}",
        settlement_entity_ids=tuple(sorted(decision.settlement_entity_ids)),
        order_ids=_order_ids_for(rows),
        credit_paise=exception.credit_paise,
        expected_paise=expected,
        delta_paise=exception.credit_paise - expected,
        reason=decision.reason,
        layer=decision.layer,
    )


def _exception_from_decision(decision: Decision, exception: Exception_) -> Exception_:
    return Exception_(
        bank_txn_id=decision.bank_txn_id,
        rule=decision.rule,
        reason=decision.reason,
        credit_paise=exception.credit_paise,
        candidate_entity_ids=exception.candidate_entity_ids,
        layer=decision.layer,
    )


def run_classifier(
    dataset: Dataset,
    reconciliation: Reconciliation,
    *,
    backend: Backend | None = None,
    threshold: float = AUTO_RESOLVE_THRESHOLD,
) -> tuple[Reconciliation, tuple[Diagnosis, ...], tuple[Decision, ...]]:
    """Send every deterministic exception through the classifier and the gate."""
    by_id = {row.entity_id: row for row in dataset.settlements}
    bank_by_id = {line.txn_id: line for line in dataset.bank}
    unclaimed = [by_id[eid] for eid in reconciliation.unclaimed_entity_ids]
    toolbox = ToolBox(unclaimed, universe=dataset.settlements)
    lookup = net_paise_lookup(by_id)

    matches = list(reconciliation.matches)
    exceptions: list[Exception_] = []
    diagnoses: list[Diagnosis] = []
    decisions: list[Decision] = []

    for exception in reconciliation.exceptions:
        diagnosis = classify(exception, bank_by_id[exception.bank_txn_id], toolbox, backend)
        decision = decide(
            diagnosis,
            credit_paise=exception.credit_paise,
            net_paise_of=lookup,
            threshold=threshold,
        )
        diagnoses.append(diagnosis)
        decisions.append(decision)

        if decision.auto_resolved:
            matches.append(_match_from_decision(decision, exception, by_id))
        else:
            exceptions.append(_exception_from_decision(decision, exception))

    claimed = {eid for match in matches for eid in match.settlement_entity_ids}
    final = Reconciliation(
        matches=tuple(sorted(matches, key=lambda m: m.bank_txn_id)),
        exceptions=tuple(sorted(exceptions, key=lambda e: e.bank_txn_id)),
        unclaimed_entity_ids=tuple(
            sorted(row.entity_id for row in dataset.settlements if row.entity_id not in claimed)
        ),
    )
    return final, tuple(diagnoses), tuple(decisions)


def _persist(
    conn,
    run_id: str,
    stamp: str,
    outcome: Reconciliation,
) -> None:
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
                stamp,
            )
            for index, match in enumerate(outcome.matches, start=1)
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
                stamp,
            )
            for index, exception in enumerate(outcome.exceptions, start=1)
        ],
    )


def _record_ai(log: AuditLog, diagnoses: tuple[Diagnosis, ...], decisions: tuple[Decision, ...]):
    """Every tool call and every gate decision, logged.

    The tool calls matter as much as the verdict: "just an LLM lookup table" is
    the obvious critique of this design, and the answer to it is a trail showing
    the model enumerating combinations and being refused by a rule.
    """
    by_txn = {d.bank_txn_id: d for d in diagnoses}
    for decision in decisions:
        diagnosis = by_txn[decision.bank_txn_id]
        for call in diagnosis.tool_calls:
            log.record(
                layer="ai",
                rule=f"tool:{call.name}",
                subject_type="bank_txn",
                subject_id=decision.bank_txn_id,
                decision="tool_call",
                reason=f"classifier called {call.name}",
                inputs={
                    "arguments": call.arguments,
                    "result": call.result
                    if call.name != "query_candidates"
                    else {"count": (call.result or {}).get("count")},
                },
            )
        log.record(
            layer="ai",
            rule=decision.rule,
            subject_type="bank_txn",
            subject_id=decision.bank_txn_id,
            decision="match" if decision.auto_resolved else "exception",
            reason=decision.reason,
            inputs={
                "case_id": decision.case_id,
                "confidence": decision.confidence,
                "claimed": list(decision.settlement_entity_ids),
                "turns": diagnosis.turns,
                "model": diagnosis.model,
                "backend": diagnosis.backend,
                "tools_used": list(diagnosis.used_tools),
                "input_tokens": diagnosis.input_tokens,
                "output_tokens": diagnosis.output_tokens,
            },
        )
    log.commit()


def run_reconciliation(
    seed: str,
    *,
    db_path: Path | str = db.DEFAULT_DB_PATH,
    fresh: bool = True,
    clock: Callable[[], str] = utc_now,
    source: Path | None = None,
    use_ai: bool = False,
    backend: Backend | None = None,
    threshold: float = AUTO_RESOLVE_THRESHOLD,
) -> RunResult:
    """Load one seed, run the layers, persist every decision.

    `use_ai` defaults to False so that the deterministic baseline is always
    reproducible without a key, a network, or a cache.
    """
    directory = Path(source) if source is not None else seed_dir(seed)
    dataset = load_dataset(directory, seed=seed, with_truth=False)

    conn = db.connect(db_path, fresh=fresh)
    try:
        db.load_dataset_into(conn, dataset)
        run_id = _next_run_id(conn, seed)
        stamp = clock()
        db.start_run(conn, run_id, seed, stamp)

        deterministic = reconcile(dataset)
        log = AuditLog(conn, run_id, clock=clock)

        diagnoses: tuple[Diagnosis, ...] = ()
        decisions: tuple[Decision, ...] = ()
        final: Reconciliation | None = None

        # The deterministic decisions are logged as they were taken, even for the
        # exceptions the AI layer later resolves -- the trail is a history, not a
        # summary of where things ended up.
        record_reconciliation(
            log,
            deterministic.matches,
            deterministic.exceptions,
            {line.txn_id: line for line in dataset.bank},
        )

        if use_ai:
            final, diagnoses, decisions = run_classifier(
                dataset,
                deterministic,
                backend=backend or default_backend(prefer_live=None),
                threshold=threshold,
            )
            _record_ai(log, diagnoses, decisions)

        outcome = final if final is not None else deterministic
        _persist(conn, run_id, stamp, outcome)

        model = diagnoses[0].model if diagnoses else None
        db.finish_run(
            conn,
            run_id,
            clock(),
            orders=len(dataset.orders),
            settlement_rows=len(dataset.settlements),
            bank_lines=len(dataset.bank),
            match_count=len(outcome.matches),
            exception_count=len(outcome.exceptions),
            matched_orders=len(outcome.matched_order_ids),
            llm_calls=sum(d.turns for d in diagnoses),
            input_tokens=sum(d.input_tokens for d in diagnoses),
            output_tokens=sum(d.output_tokens for d in diagnoses),
            model=model,
        )
    finally:
        conn.close()

    return RunResult(
        run_id=run_id,
        seed=seed,
        dataset=dataset,
        reconciliation=deterministic,
        db_path=Path(db_path),
        final=final,
        diagnoses=diagnoses,
        decisions=decisions,
        ai_ran=use_ai,
    )

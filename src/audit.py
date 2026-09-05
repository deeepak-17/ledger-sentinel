"""The audit trail.

**Zero unlogged decisions is an SLO**, not a nice-to-have. A reconciliation
system that reports a match rate without being able to show its working is
asking to be trusted, and in finance ops that is the wrong ask. Every match and
every exception writes exactly one audit row naming the rule that fired, the
inputs it saw, the decision it took and the reason in words a controller can
read. `tests/test_golden.py` asserts the count, so a future layer that resolves
something quietly will fail the build rather than the demo.

The `layer` column is what makes the trail useful once M3 lands: filtering on
`layer = 'deterministic'` shows what arithmetic decided, and `layer = 'ai'` shows
what the model decided. Nothing merges the two.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.db import utc_now
from src.matcher import MATCH_RULES, Exception_, Match
from src.schema import BankTxn

DECISION_MATCH = "match"
DECISION_EXCEPTION = "exception"


@dataclass
class AuditLog:
    """Append-only decision log for one run.

    `seq` is a per-run counter rather than the autoincrement id so that two runs
    of the same data produce the same sequence numbers -- the determinism script
    compares trails, and a global id would differ purely because of run order.
    """

    conn: sqlite3.Connection
    run_id: str
    clock: Callable[[], str] = utc_now
    _seq: int = 0

    def record(
        self,
        *,
        layer: str,
        rule: str,
        subject_type: str,
        subject_id: str,
        decision: str,
        reason: str,
        inputs: dict[str, Any],
    ) -> None:
        self._seq += 1
        self.conn.execute(
            "INSERT INTO audit "
            "(run_id, seq, layer, rule, subject_type, subject_id, decision, reason, "
            " inputs, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                self._seq,
                layer,
                rule,
                subject_type,
                subject_id,
                decision,
                reason,
                json.dumps(inputs, sort_keys=True, separators=(",", ":")),
                self.clock(),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    @property
    def entries_written(self) -> int:
        return self._seq


def _bank_facts(line: BankTxn | None) -> dict[str, Any]:
    """The statement side of the evidence, recorded as the rule saw it."""
    if line is None:
        return {}
    return {
        "value_date": line.value_date.isoformat(),
        "bank_utr": line.utr,
        "bank_credit_paise": line.credit_paise,
        "description": line.description,
    }


def record_reconciliation(
    log: AuditLog,
    matches: Sequence[Match],
    exceptions: Sequence[Exception_],
    bank_by_id: dict[str, BankTxn],
) -> None:
    """Write one row per decision taken by the deterministic layer.

    Matches and exceptions are logged through the same function because they are
    the same kind of event -- a rule looked at a bank line and reached a
    conclusion. Logging only the failures would make the trail an error log, and
    the interesting audit question is usually "why did you think this one was
    fine?"
    """
    for match in matches:
        if match.rule not in MATCH_RULES:
            raise ValueError(
                f"{match.rule!r} produced a match but is not in MATCH_RULES; "
                "the rule table and the matcher have drifted apart"
            )
        log.record(
            layer=match.layer,
            rule=match.rule,
            subject_type="bank_txn",
            subject_id=match.bank_txn_id,
            decision=DECISION_MATCH,
            reason=match.reason,
            inputs={
                **_bank_facts(bank_by_id.get(match.bank_txn_id)),
                "settlement_entity_ids": list(match.settlement_entity_ids),
                "order_ids": list(match.order_ids),
                "expected_paise": match.expected_paise,
                "delta_paise": match.delta_paise,
            },
        )

    for exception in exceptions:
        log.record(
            layer=exception.layer,
            rule=exception.rule,
            subject_type="bank_txn",
            subject_id=exception.bank_txn_id,
            decision=DECISION_EXCEPTION,
            reason=exception.reason,
            inputs={
                **_bank_facts(bank_by_id.get(exception.bank_txn_id)),
                "candidate_entity_ids": list(exception.candidate_entity_ids),
                "candidate_count": len(exception.candidate_entity_ids),
            },
        )
    log.commit()


def decisions_for(conn: sqlite3.Connection, run_id: str, subject_id: str) -> list[sqlite3.Row]:
    """Everything the system decided about one bank line, in order. This is the
    query a judge runs when they want to interrogate a single number."""
    return list(
        conn.execute(
            "SELECT * FROM audit WHERE run_id = ? AND subject_id = ? ORDER BY seq",
            (run_id, subject_id),
        )
    )


def unlogged_decisions(conn: sqlite3.Connection, run_id: str) -> int:
    """How many recorded matches and exceptions have no audit row. Must be 0."""
    decided = int(
        conn.execute(
            "SELECT (SELECT count(*) FROM matches WHERE run_id = ?) + "
            "(SELECT count(*) FROM exceptions WHERE run_id = ?)",
            (run_id, run_id),
        ).fetchone()[0]
    )
    logged = int(
        conn.execute("SELECT count(*) FROM audit WHERE run_id = ?", (run_id,)).fetchone()[0]
    )
    return decided - logged

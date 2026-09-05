"""The metrics floor.

These are the numbers M2 is allowed to be judged on. They are asserted rather
than printed because a match rate in a README is a claim, and a claim nobody
re-checks decays. If a future change moves any of these, the build fails and
somebody has to decide -- deliberately -- whether the new number is better.

The floor is deliberately two-sided. A match rate that goes *up* unexpectedly is
as alarming as one that drops: the deterministic layer is supposed to close
exactly the cases that carry an identifier, and closing more than that means it
started guessing.
"""

from __future__ import annotations

import sqlite3

from src import db
from src.audit import DECISION_EXCEPTION, DECISION_MATCH, unlogged_decisions
from src.matcher import MATCH_RULES, reconcile
from src.metrics import render_markdown, render_text

# 56 of 80 orders sit behind a bank credit that carries a UTR. That is a
# property of the dataset, not a tuning target -- see data/README.md.
EXPECTED_MATCH_RATE = 0.70
MATCH_RATE_FLOOR = 0.68
MATCH_RATE_CEILING = 0.72


class TestHeadlineMetrics:
    def test_false_match_rate_is_zero(self, report):
        assert report.false_matches == ()
        assert report.false_match_rate == 0.0

    def test_match_rate_sits_in_the_declared_band(self, report):
        assert MATCH_RATE_FLOOR <= report.match_rate <= MATCH_RATE_CEILING

    def test_held_out_match_rate_is_exactly_as_published(self, held_out):
        assert held_out.match_rate == EXPECTED_MATCH_RATE
        assert held_out.matched_orders == 56
        assert held_out.orders == 80

    def test_every_must_escalate_case_was_escalated(self, report):
        assert report.escalation_required > 0
        assert report.escalation_achieved == report.escalation_required


class TestEveryDecisionIsAccountedFor:
    def test_matches_and_exceptions_partition_the_credits(self, run_result):
        reconciliation = run_result.reconciliation
        credits = {line.txn_id for line in run_result.dataset.bank if line.credit_paise > 0}
        matched = reconciliation.matched_bank_txn_ids
        raised = {exception.bank_txn_id for exception in reconciliation.exceptions}

        assert matched | raised == credits
        assert matched & raised == set()

    def test_no_settlement_row_is_claimed_twice(self, run_result):
        claimed = [
            entity_id
            for match in run_result.reconciliation.matches
            for entity_id in match.settlement_entity_ids
        ]
        assert len(claimed) == len(set(claimed))

    def test_only_match_rules_produce_matches(self, run_result):
        assert {m.rule for m in run_result.reconciliation.matches} <= MATCH_RULES

    def test_every_exception_carries_a_reason(self, run_result):
        for exception in run_result.reconciliation.exceptions:
            assert exception.reason.strip()
            assert exception.rule.strip()


class TestAuditTrail:
    def test_zero_unlogged_decisions(self, run_result):
        conn = db.connect(run_result.db_path)
        try:
            assert unlogged_decisions(conn, run_result.run_id) == 0
        finally:
            conn.close()

    def test_every_match_has_an_audit_row_naming_its_rule(self, run_result):
        conn = db.connect(run_result.db_path)
        try:
            rows = conn.execute(
                "SELECT subject_id, rule, decision FROM audit WHERE run_id = ? AND decision = ?",
                (run_result.run_id, DECISION_MATCH),
            ).fetchall()
        finally:
            conn.close()

        logged = {(row["subject_id"], row["rule"]) for row in rows}
        expected = {(m.bank_txn_id, m.rule) for m in run_result.reconciliation.matches}
        assert logged == expected

    def test_every_exception_has_an_audit_row(self, run_result):
        conn = db.connect(run_result.db_path)
        try:
            rows = conn.execute(
                "SELECT subject_id, rule FROM audit WHERE run_id = ? AND decision = ?",
                (run_result.run_id, DECISION_EXCEPTION),
            ).fetchall()
        finally:
            conn.close()

        logged = {(row["subject_id"], row["rule"]) for row in rows}
        expected = {(e.bank_txn_id, e.rule) for e in run_result.reconciliation.exceptions}
        assert logged == expected

    def test_run_row_records_the_totals(self, run_result):
        conn = db.connect(run_result.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_result.run_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["match_count"] == len(run_result.reconciliation.matches)
        assert row["exception_count"] == len(run_result.reconciliation.exceptions)
        assert row["finished_at"] is not None


class TestDeterminism:
    def test_reconciling_twice_gives_identical_decisions(self, run_result):
        again = reconcile(run_result.dataset)
        assert again == run_result.reconciliation


class TestGroundTruthIsNeverInThePipeline:
    def test_the_pipeline_does_not_load_truth(self, run_result):
        assert run_result.dataset.truth == ()

    def test_the_database_has_no_truth_table(self, run_result):
        conn = sqlite3.connect(run_result.db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        assert not any("truth" in name for name in tables)


class TestReportsRender:
    def test_text_report_leads_with_the_false_match_rate(self, report):
        body = render_text(report)
        assert body.index("FALSE-MATCH RATE") < body.index("match rate")

    def test_markdown_report_contains_every_unresolved_line(self, report):
        body = render_markdown(report)
        for item in report.unresolved:
            assert item.bank_txn_id in body

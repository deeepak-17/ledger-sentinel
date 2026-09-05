"""Case 10 must not be matched by the deterministic layer.

This is the test that protects the headline number. The amount collision is
built so that a single settlement row explains the credit exactly *and* a
disjoint pair of rows explains it exactly, with nothing in the data to choose
between them. Any layer confident enough to close it is wrong half the time by
construction, and would be wrong silently.

`tests/test_unit_dataset.py` proves the trap is genuinely set. This file proves
the matcher walks around it.
"""

from __future__ import annotations

from src.cases import ADVERSARIAL_CASES, Case
from src.ingest import load_truth
from src.matcher import MATCH_RULES, RULE_NO_IDENTIFIER
from src.pipeline import seed_dir


def _adversarial_records(truth):
    records = [t for t in truth if t.case_id in {int(c) for c in ADVERSARIAL_CASES}]
    assert records, "the dataset no longer plants an adversarial case"
    return records


class TestCollisionIsNotMatched:
    def test_no_adversarial_bank_line_is_matched(self, run_result):
        records = _adversarial_records(load_truth(seed_dir(run_result.seed) / "truth.json"))
        forbidden = {tid for r in records for tid in r.bank_txn_ids}
        assert forbidden & run_result.reconciliation.matched_bank_txn_ids == set()

    def test_each_adversarial_line_is_raised_as_an_exception_with_a_reason(self, run_result):
        records = _adversarial_records(load_truth(seed_dir(run_result.seed) / "truth.json"))
        expected = {tid for r in records for tid in r.bank_txn_ids}
        raised = {
            e.bank_txn_id: e
            for e in run_result.reconciliation.exceptions
            if e.bank_txn_id in expected
        }

        assert set(raised) == expected
        for exception in raised.values():
            # It is escalated because it carries no identifier -- not because a
            # rule recognised it as "the adversarial one". The matcher has no
            # idea which case this is, and that is the point.
            assert exception.rule == RULE_NO_IDENTIFIER
            assert exception.reason.strip()

    def test_no_adversarial_order_is_claimed_by_any_match(self, run_result):
        records = _adversarial_records(load_truth(seed_dir(run_result.seed) / "truth.json"))
        forbidden = {oid for r in records for oid in r.order_ids}
        assert forbidden & run_result.reconciliation.matched_order_ids == set()


class TestEscalationIsScored:
    def test_the_report_scores_escalation_as_a_success(self, report):
        assert report.escalation_required >= 2
        assert report.escalation_achieved == report.escalation_required

    def test_the_collision_appears_in_the_unresolved_list(self, report):
        planted = [u for u in report.unresolved if u.planted_case_id == int(Case.AMOUNT_COLLISION)]
        assert len(planted) == 2
        for item in planted:
            assert item.planted_expectation == "escalate"
            assert item.candidate_count > 0


class TestTheLayerCannotBeTalkedIntoGuessing:
    def test_matching_never_uses_a_rule_outside_the_match_table(self, run_result):
        assert {m.rule for m in run_result.reconciliation.matches} <= MATCH_RULES

    def test_no_match_was_closed_without_an_identifier(self, run_result):
        bank_by_id = {b.txn_id: b for b in run_result.dataset.bank}
        for match in run_result.reconciliation.matches:
            assert bank_by_id[match.bank_txn_id].utr is not None

"""The tools, tested without the model in the loop.

If these are right, the classifier cannot be wrong about arithmetic -- it can
only be wrong about judgement, which is the thing we actually want to measure.

The class that matters most here is `TestBatchStructureDoesNotSolveTheCollision`.
`subset_sum` reports whether a combination is a complete settlement batch,
because that is what tells an ordinary consolidated payout apart from a
coincidental sum. It would be very easy for a future change to lean on that
signal to "resolve" the amount collision and book a higher match rate. It cannot,
because on the collision *both* readings are complete batches -- and that test
exists so the day someone breaks the property, the build says so.
"""

from __future__ import annotations

import pytest

from config import MAX_SUBSET_SIZE
from src.cases import ADVERSARIAL_CASES
from src.ingest import load_truth
from src.money import expected_deduction
from src.pipeline import seed_dir
from src.tools import ToolBox, ToolError


@pytest.fixture(scope="module")
def toolbox(request):
    """Built exactly as the pipeline builds it: unclaimed rows only, with the
    full cycle as the universe so batch completeness is judged honestly."""
    from src.pipeline import run_reconciliation

    result = run_reconciliation("B", db_path=request.config.cache.makedir("tools") / "b.db")
    by_id = {row.entity_id: row for row in result.dataset.settlements}
    unclaimed = [by_id[eid] for eid in result.reconciliation.unclaimed_entity_ids]
    return ToolBox(unclaimed, universe=result.dataset.settlements), result


class TestExpectedFee:
    def test_it_is_the_same_arithmetic_the_matcher_uses(self, toolbox):
        box, _ = toolbox
        for amount, method in ((729900, "card"), (100, "upi"), (1, "netbanking")):
            fee, tax = expected_deduction(amount, method)
            answer = box.expected_fee(amount_paise=amount, method=method)
            assert (answer["fee_paise"], answer["tax_paise"]) == (fee, tax)
            assert answer["net_paise"] == amount - fee - tax

    def test_upi_is_zero_rated_in_this_model(self, toolbox):
        box, _ = toolbox
        assert box.expected_fee(amount_paise=500000, method="upi")["fee_paise"] == 0

    def test_an_unknown_method_is_an_error_not_a_guess(self, toolbox):
        box, _ = toolbox
        with pytest.raises(ToolError):
            box.expected_fee(amount_paise=100, method="crypto")

    def test_a_negative_amount_is_rejected(self, toolbox):
        box, _ = toolbox
        with pytest.raises(ToolError):
            box.expected_fee(amount_paise=-1, method="card")


class TestQueryCandidates:
    def test_it_only_offers_unclaimed_rows(self, toolbox):
        box, result = toolbox
        offered = {c["entity_id"] for c in box.query_candidates()["candidates"]}
        claimed = {eid for m in result.reconciliation.matches for eid in m.settlement_entity_ids}
        assert offered & claimed == set()
        assert offered == set(result.reconciliation.unclaimed_entity_ids)

    def test_the_date_window_narrows_the_pool(self, toolbox):
        box, _ = toolbox
        everything = box.query_candidates()["count"]
        narrow = box.query_candidates(value_date="2026-09-14", window_days=0)["count"]
        assert 0 < narrow < everything

    def test_filters_are_anded(self, toolbox):
        box, _ = toolbox
        refunds = box.query_candidates(entity_type="refund")["candidates"]
        assert refunds
        assert all(r["entity_type"] == "refund" for r in refunds)

    def test_a_malformed_date_is_an_error(self, toolbox):
        box, _ = toolbox
        with pytest.raises(ToolError):
            box.query_candidates(value_date="14/09/2026")


class TestSubsetSum:
    def test_it_finds_the_batch_behind_a_consolidated_credit(self, toolbox):
        box, result = toolbox
        truth = {
            t.bank_txn_ids[0]: t for t in load_truth(seed_dir("B") / "truth.json") if t.bank_txn_ids
        }
        for exception in result.reconciliation.exceptions:
            record = truth[exception.bank_txn_id]
            if record.expected_resolution != "matched":
                continue
            answer = box.subset_sum(
                target_paise=exception.credit_paise,
                entity_ids=list(exception.candidate_entity_ids),
            )
            assert sorted(record.settlement_ids) in answer["solutions"]

    def test_refunds_count_as_negative_so_netted_payouts_are_reachable(self, toolbox):
        box, result = toolbox
        truth = {
            t.bank_txn_ids[0]: t for t in load_truth(seed_dir("B") / "truth.json") if t.bank_txn_ids
        }
        netted = [e for e in result.reconciliation.exceptions if truth[e.bank_txn_id].case_id == 5]
        assert netted, "seed_B should contain netted-refund payouts"
        for exception in netted:
            answer = box.subset_sum(
                target_paise=exception.credit_paise,
                entity_ids=list(exception.candidate_entity_ids),
            )
            solution = sorted(truth[exception.bank_txn_id].settlement_ids)
            assert solution in answer["solutions"]
            assert any(eid.startswith("rfnd_") for eid in solution)

    def test_it_returns_every_solution_not_the_first(self, toolbox):
        box, result = toolbox
        multiple = [
            box.subset_sum(target_paise=e.credit_paise, entity_ids=list(e.candidate_entity_ids))
            for e in result.reconciliation.exceptions
        ]
        assert any(answer["solution_count"] > 1 for answer in multiple)
        for answer in multiple:
            assert len(answer["solutions"]) == min(answer["solution_count"], 25)

    def test_an_impossible_target_returns_nothing_rather_than_a_near_miss(self, toolbox):
        box, _ = toolbox
        answer = box.subset_sum(target_paise=7)
        assert answer["solution_count"] == 0
        assert "no combination" in answer["note"]

    def test_max_k_is_bounded(self, toolbox):
        box, _ = toolbox
        with pytest.raises(ToolError, match="MAX_SUBSET_SIZE"):
            box.subset_sum(target_paise=100, max_k=MAX_SUBSET_SIZE + 1)

    def test_a_claimed_row_cannot_be_pulled_back_in(self, toolbox):
        box, result = toolbox
        claimed = next(
            eid for m in result.reconciliation.matches for eid in m.settlement_entity_ids
        )
        with pytest.raises(ToolError, match="already-claimed"):
            box.subset_sum(target_paise=100, entity_ids=[claimed])


class TestBatchStructureDoesNotSolveTheCollision:
    """The load-bearing test of this file. Read the module docstring."""

    @pytest.fixture(params=("A", "B"))
    def seed_case(self, request):
        from src.pipeline import run_reconciliation

        seed = request.param
        result = run_reconciliation(seed, db_path=f"/tmp/collision_{seed}.db")
        by_id = {r.entity_id: r for r in result.dataset.settlements}
        box = ToolBox(
            [by_id[e] for e in result.reconciliation.unclaimed_entity_ids],
            universe=result.dataset.settlements,
        )
        truth = {
            t.bank_txn_ids[0]: t
            for t in load_truth(seed_dir(seed) / "truth.json")
            if t.bank_txn_ids
        }
        adversarial = {int(c) for c in ADVERSARIAL_CASES}
        credits = [
            e
            for e in result.reconciliation.exceptions
            if truth[e.bank_txn_id].case_id in adversarial
        ]
        return box, credits

    def test_the_collision_has_two_readings(self, seed_case):
        box, credits = seed_case
        assert credits
        for exception in credits:
            answer = box.subset_sum(
                target_paise=exception.credit_paise,
                entity_ids=list(exception.candidate_entity_ids),
            )
            assert answer["solution_count"] >= 2
            assert answer["disjoint_alternatives"], "the readings must not overlap"

    def test_both_readings_are_complete_batches(self, seed_case):
        box, credits = seed_case
        for exception in credits:
            answer = box.subset_sum(
                target_paise=exception.credit_paise,
                entity_ids=list(exception.candidate_entity_ids),
            )
            structures = answer["solution_structure"]
            assert all(s["is_complete_batch"] for s in structures), (
                "batch structure now distinguishes the two readings of the amount "
                "collision. That makes case 10 solvable and the 'must escalate' "
                "claim theatre. Do not relax this test -- fix the change that "
                "broke the property."
            )

    def test_ordinary_batched_payouts_are_distinguishable_by_structure(self, toolbox):
        """The other half of the argument: structure resolves the ordinary cases.

        seed_B contains an unplanted collision on a case-4 payout -- two exact
        readings the generator did not intend. Recorded in FAILURES.md. The
        intended reading is one complete batch and the accidental one is stitched
        from three unrelated batches, which is what makes it resolvable at all.
        """
        box, result = toolbox
        truth = {
            t.bank_txn_ids[0]: t for t in load_truth(seed_dir("B") / "truth.json") if t.bank_txn_ids
        }
        for exception in result.reconciliation.exceptions:
            record = truth[exception.bank_txn_id]
            if record.expected_resolution != "matched":
                continue
            answer = box.subset_sum(
                target_paise=exception.credit_paise,
                entity_ids=list(exception.candidate_entity_ids),
            )
            complete = [
                solution
                for solution, structure in zip(
                    answer["solutions"], answer["solution_structure"], strict=True
                )
                if structure["is_complete_batch"]
            ]
            assert len(complete) == 1
            assert complete[0] == sorted(record.settlement_ids)


class TestDispatch:
    def test_an_unknown_tool_comes_back_as_a_result_not_a_crash(self, toolbox):
        box, _ = toolbox
        assert "error" in box.dispatch("delete_everything", {})

    def test_a_bad_argument_costs_a_turn_not_the_run(self, toolbox):
        box, _ = toolbox
        assert "error" in box.dispatch("expected_fee", {"amount_paise": 1, "method": "gold"})
        assert "error" in box.dispatch("subset_sum", {"nonsense": 1})

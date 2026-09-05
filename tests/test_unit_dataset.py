"""The dataset is the quality ceiling, so it is tested harder than the pipeline.

Two things are being defended here. First, ground truth must be total and
unambiguous -- every row belongs to exactly one case, or the match rate means
nothing. Second, the adversarial case must genuinely be adversarial: it is easy
to label a case "escalate" and accidentally build something a competent matcher
solves. `TestCollisionIsGenuinelyAmbiguous` proves the trap is really set, and
that no field in the data quietly gives the answer away.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations

import pytest

from config import RECORDS_PER_SEED
from data.generator import SEED_CONFIG, build_dataset, distribution, planned_record_count
from src.cases import Case
from src.schema import Dataset


@pytest.fixture(scope="module")
def seed_a() -> Dataset:
    return build_dataset("A")


@pytest.fixture(scope="module")
def seed_b() -> Dataset:
    return build_dataset("B")


@pytest.fixture(scope="module", params=sorted(SEED_CONFIG))
def any_seed(request) -> Dataset:
    return build_dataset(request.param)


class TestPlan:
    def test_active_plan_emits_the_configured_record_count(self):
        assert planned_record_count() == RECORDS_PER_SEED

    def test_every_seed_has_the_same_shape(self, seed_a, seed_b):
        assert distribution(seed_a) == distribution(seed_b)


class TestGroundTruthIsTotal:
    def test_every_order_is_claimed_by_exactly_one_truth_record(self, any_seed):
        claimed = Counter(oid for t in any_seed.truth for oid in t.order_ids)
        actual = {o.order_id for o in any_seed.orders}

        assert set(claimed) == actual
        assert not [oid for oid, n in claimed.items() if n != 1]

    def test_every_settlement_row_is_claimed_exactly_once(self, any_seed):
        claimed = Counter(sid for t in any_seed.truth for sid in t.settlement_ids)
        actual = {s.entity_id for s in any_seed.settlements}

        assert set(claimed) == actual
        assert not [sid for sid, n in claimed.items() if n != 1]

    def test_every_bank_line_is_claimed_exactly_once(self, any_seed):
        claimed = Counter(bid for t in any_seed.truth for bid in t.bank_txn_ids)
        actual = {b.txn_id for b in any_seed.bank}

        assert set(claimed) == actual
        assert not [bid for bid, n in claimed.items() if n != 1]

    def test_truth_records_carry_a_reason(self, any_seed):
        assert all(record.note for record in any_seed.truth)


class TestDeterminism:
    def test_rebuilding_a_seed_is_identical(self):
        assert build_dataset("A") == build_dataset("A")

    def test_seeds_share_no_identifiers(self, seed_a, seed_b):
        a_ids = {o.order_id for o in seed_a.orders} | {s.utr for s in seed_a.settlements}
        b_ids = {o.order_id for o in seed_b.orders} | {s.utr for s in seed_b.settlements}

        assert not a_ids & b_ids

    def test_bank_statement_is_in_date_order(self, any_seed):
        dates = [line.value_date for line in any_seed.bank]
        assert dates == sorted(dates)


class TestIdentifierAvailability:
    """The split between "has a UTR" and "has none" is the split between the
    deterministic layer and the AI layer, so it is asserted, not assumed."""

    SINGLE_ROW_CASES = {
        int(Case.CLEAN_MATCH),
        int(Case.FEE_AND_GST),
        int(Case.SETTLEMENT_TIMING),
        int(Case.ROUNDING_DRIFT),
    }
    MULTI_ROW_CASES = {
        int(Case.BATCHED_PAYOUT),
        int(Case.NETTED_REFUND),
        int(Case.AMOUNT_COLLISION),
    }

    @staticmethod
    def _bank_by_case(dataset: Dataset) -> dict[int, list]:
        by_txn = {b.txn_id: b for b in dataset.bank}
        grouped: dict[int, list] = {}
        for record in dataset.truth:
            grouped.setdefault(record.case_id, []).extend(
                by_txn[tid] for tid in record.bank_txn_ids
            )
        return grouped

    def test_single_settlement_payouts_carry_a_utr(self, any_seed):
        grouped = self._bank_by_case(any_seed)
        for case_id in self.SINGLE_ROW_CASES:
            assert all(line.utr for line in grouped[case_id]), case_id

    def test_consolidated_payouts_carry_none(self, any_seed):
        grouped = self._bank_by_case(any_seed)
        for case_id in self.MULTI_ROW_CASES:
            assert all(line.utr is None for line in grouped[case_id]), case_id

    def test_a_utr_never_identifies_more_than_one_settlement_row_group(self, any_seed):
        """A UTR that mapped to two different payouts would make the
        deterministic rule unsound. Case 11 will break this deliberately; until
        it is switched on, the invariant must hold."""
        by_utr: dict[str, set[str]] = {}
        for row in any_seed.settlements:
            by_utr.setdefault(row.utr, set()).add(row.settlement_id)
        assert all(len(ids) == 1 for ids in by_utr.values())


class TestAdversarialCoverage:
    def test_collision_case_appears_at_least_twice(self, any_seed):
        counts = distribution(any_seed)
        assert counts[int(Case.AMOUNT_COLLISION)]["units"] >= 2

    def test_collision_units_are_labelled_escalate(self, any_seed):
        collisions = [t for t in any_seed.truth if t.case_id == int(Case.AMOUNT_COLLISION)]
        assert collisions
        assert all(t.expected_resolution == "escalate" for t in collisions)

    def test_no_other_case_is_labelled_escalate(self, any_seed):
        escalating = {t.case_id for t in any_seed.truth if t.expected_resolution == "escalate"}
        assert escalating == {int(Case.AMOUNT_COLLISION)}


class TestCollisionIsGenuinelyAmbiguous:
    """Case 10 only means something if two readings of one bank credit are both
    arithmetically perfect AND nothing in the data prefers one. If a single
    reading existed, a competent matcher would close it; if a field decided it,
    escalating would be timidity rather than judgement."""

    @staticmethod
    def _collision_units(dataset: Dataset):
        by_entity = {s.entity_id: s for s in dataset.settlements}
        by_txn = {b.txn_id: b for b in dataset.bank}
        for record in dataset.truth:
            if record.case_id != int(Case.AMOUNT_COLLISION):
                continue
            rows = [by_entity[sid] for sid in record.settlement_ids]
            credit = by_txn[record.bank_txn_ids[0]]
            yield rows, credit

    @staticmethod
    def _readings(rows, credit):
        single = next(r for r in rows if r.net_paise == credit.credit_paise)
        pair = next(
            p for p in combinations(rows, 2) if sum(r.net_paise for r in p) == credit.credit_paise
        )
        return single, pair

    def test_a_single_row_explains_the_credit_exactly(self, any_seed):
        for rows, credit in self._collision_units(any_seed):
            assert [r for r in rows if r.net_paise == credit.credit_paise], (
                "no single-row reading exists, so nothing tempts the matcher"
            )

    def test_a_pair_of_rows_also_explains_it_exactly(self, any_seed):
        for rows, credit in self._collision_units(any_seed):
            pairs = [
                p
                for p in combinations(rows, 2)
                if sum(r.net_paise for r in p) == credit.credit_paise
            ]
            assert pairs, "no multi-row reading exists, so the case is not ambiguous"

    def test_the_two_readings_are_disjoint(self, any_seed):
        """The trap only works if choosing one reading rules the other out."""
        for rows, credit in self._collision_units(any_seed):
            single, pair = self._readings(rows, credit)
            assert single.entity_id not in {r.entity_id for r in pair}

    def test_no_field_distinguishes_the_two_readings(self, any_seed):
        """The decoy is not on hold, not late, and not flagged. There is no
        tiebreak hiding in the data -- which is exactly why the honest output is
        an escalation rather than a confident pick."""
        for rows, credit in self._collision_units(any_seed):
            single, pair = self._readings(rows, credit)

            assert single.on_hold is False
            assert all(r.on_hold is False for r in pair)
            assert all(r.settled_at == single.settled_at for r in pair)
            assert all(r.entity_type == single.entity_type for r in pair)

    def test_the_credit_gives_the_matcher_nothing_to_join_on(self, any_seed):
        for _rows, credit in self._collision_units(any_seed):
            assert credit.utr is None


class TestMoneyIntegrity:
    def test_no_row_both_credits_and_debits(self, any_seed):
        for row in any_seed.settlements:
            assert not (row.credit_paise and row.debit_paise)

    def test_payment_rows_reconcile_gross_to_net(self, any_seed):
        for row in any_seed.settlements:
            if row.entity_type != "payment":
                continue
            assert row.credit_paise + row.fee_paise + row.tax_paise == row.amount_paise

    def test_refund_rows_return_gross_without_the_fee(self, any_seed):
        refunds = [r for r in any_seed.settlements if r.entity_type == "refund"]
        assert refunds
        for row in refunds:
            assert row.debit_paise == row.amount_paise
            assert row.fee_paise == 0

    def test_every_bank_line_is_a_credit_in_this_cycle(self, any_seed):
        assert all(line.credit_paise > 0 and line.debit_paise == 0 for line in any_seed.bank)

    def test_all_amounts_are_integers(self, any_seed):
        for order in any_seed.orders:
            assert isinstance(order.amount_paise, int)
        for row in any_seed.settlements:
            assert isinstance(row.credit_paise, int)

"""The AI layer, tested without an API key.

Two different things are under test and they must not be confused.

`TestClassifierLoop` and `TestGate` test *our* code -- the tool-use loop, the
error paths, and the rules that decide what may be booked. They drive a scripted
backend, so they are exact and free and run in CI.

`TestEndToEndWithAScriptedAnalyst` runs the whole pipeline with a stand-in that
plays a competent analyst deterministically. **It measures the plumbing, not the
model.** It proves that if a classifier reaches the right conclusions the system
books them correctly, that the gate refuses what it should refuse, and that a
false match cannot slip through the arithmetic re-check. What the real model
actually concludes is measured by `make metrics` against the recorded cache, and
that number is the one that gets published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from config import ALWAYS_ESCALATE_CASES, AUTO_RESOLVE_THRESHOLD, MAX_TOOL_TURNS
from src.classifier import RESOLUTION_ESCALATE, RESOLUTION_MATCHED, classify
from src.gate import (
    RULE_ALWAYS_ESCALATE,
    RULE_AMBIGUOUS_EVIDENCE,
    RULE_ARITHMETIC_FAILS,
    RULE_AUTO_RESOLVED,
    RULE_BELOW_THRESHOLD,
    RULE_MODEL_ESCALATED,
    RULE_NO_ROWS_CLAIMED,
    decide,
    net_paise_lookup,
)
from src.llm import Response
from src.pipeline import run_classifier, run_reconciliation
from src.tools import ToolBox


def tool_call(name: str, **arguments) -> dict:
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(arguments)}


def reply(*calls, content: str | None = None) -> Response:
    return Response(
        content=content,
        tool_calls=tuple(calls),
        input_tokens=100,
        output_tokens=20,
        model="scripted",
        backend="scripted",
    )


@dataclass
class ScriptedBackend:
    """Plays a fixed list of assistant turns, in order."""

    turns: list[Response]
    name: str = "scripted"
    seen: list[list[dict]] = field(default_factory=list)

    def complete(self, messages, tools):
        self.seen.append(list(messages))
        if not self.turns:
            return reply(content="(out of script)")
        return self.turns.pop(0)


@pytest.fixture(scope="module")
def world():
    result = run_reconciliation("B", db_path="/tmp/ai_world.db")
    by_id = {row.entity_id: row for row in result.dataset.settlements}
    bank = {line.txn_id: line for line in result.dataset.bank}
    box = ToolBox(
        [by_id[eid] for eid in result.reconciliation.unclaimed_entity_ids],
        universe=result.dataset.settlements,
    )
    return result, by_id, bank, box


class TestClassifierLoop:
    def test_a_full_investigation_is_recorded(self, world):
        result, _, bank, box = world
        exception = result.reconciliation.exceptions[0]
        backend = ScriptedBackend(
            [
                reply(tool_call("query_candidates", value_date="2026-09-14")),
                reply(tool_call("subset_sum", target_paise=exception.credit_paise)),
                reply(
                    tool_call(
                        "submit_diagnosis",
                        case_id=4,
                        resolution=RESOLUTION_MATCHED,
                        settlement_entity_ids=["a", "b"],
                        confidence=0.93,
                        reasoning="one complete batch sums to the credit",
                    )
                ),
            ]
        )
        diagnosis = classify(exception, bank[exception.bank_txn_id], box, backend)

        assert diagnosis.case_id == 4
        assert diagnosis.resolution == RESOLUTION_MATCHED
        assert diagnosis.confidence == 0.93
        assert diagnosis.used_tools == ("query_candidates", "subset_sum", "submit_diagnosis")
        assert diagnosis.turns == 3
        assert diagnosis.input_tokens == 300

    def test_the_system_prompt_never_carries_the_answer_key(self, world):
        result, _, bank, box = world
        exception = result.reconciliation.exceptions[0]
        backend = ScriptedBackend([])
        classify(exception, bank[exception.bank_txn_id], box, backend)

        system = backend.seen[0][0]["content"].lower()
        for leak in ("escalate this", "adversarial", "resolvable_by", "ground truth", "truth.json"):
            assert leak not in system
        # the taxonomy is domain knowledge and belongs in the prompt; the verdict
        # attached to each case in src/cases.py does not.
        assert "amount collision" in system

    def test_a_model_that_never_concludes_escalates_rather_than_hangs(self, world):
        result, _, bank, box = world
        exception = result.reconciliation.exceptions[0]
        backend = ScriptedBackend(
            [reply(tool_call("query_candidates")) for _ in range(MAX_TOOL_TURNS + 2)]
        )
        diagnosis = classify(exception, bank[exception.bank_txn_id], box, backend)

        assert diagnosis.resolution == RESOLUTION_ESCALATE
        assert diagnosis.confidence == 0.0
        assert "did not reach a conclusion" in diagnosis.reasoning

    def test_prose_instead_of_a_tool_call_is_nudged_once(self, world):
        result, _, bank, box = world
        exception = result.reconciliation.exceptions[0]
        backend = ScriptedBackend(
            [
                reply(content="I think this is a batched payout."),
                reply(
                    tool_call(
                        "submit_diagnosis",
                        case_id=4,
                        resolution=RESOLUTION_MATCHED,
                        settlement_entity_ids=["a"],
                        confidence=0.5,
                        reasoning="ok",
                    )
                ),
            ]
        )
        diagnosis = classify(exception, bank[exception.bank_txn_id], box, backend)
        assert diagnosis.case_id == 4
        assert any(
            m.get("content") == "Call submit_diagnosis to record your conclusion."
            for m in backend.seen[-1]
        )

    def test_a_bad_tool_call_costs_a_turn_not_the_run(self, world):
        result, _, bank, box = world
        exception = result.reconciliation.exceptions[0]
        backend = ScriptedBackend(
            [
                reply(tool_call("expected_fee", amount_paise=100, method="gold")),
                reply(
                    tool_call(
                        "submit_diagnosis",
                        case_id=4,
                        resolution=RESOLUTION_ESCALATE,
                        settlement_entity_ids=[],
                        confidence=0.1,
                        reasoning="could not price it",
                    )
                ),
            ]
        )
        diagnosis = classify(exception, bank[exception.bank_txn_id], box, backend)
        assert "error" in diagnosis.tool_calls[0].result
        assert diagnosis.resolution == RESOLUTION_ESCALATE


def _diagnosis(world, **overrides):
    from src.classifier import Diagnosis

    defaults = dict(
        bank_txn_id="BNK00001",
        case_id=4,
        resolution=RESOLUTION_MATCHED,
        settlement_entity_ids=(),
        confidence=0.99,
        reasoning="because",
        tool_calls=(),
        turns=2,
        input_tokens=1,
        output_tokens=1,
        model="scripted",
        backend="scripted",
    )
    defaults.update(overrides)
    return Diagnosis(**defaults)


class TestGate:
    def test_a_declined_diagnosis_is_never_booked(self, world):
        _, by_id, _, _ = world
        decision = decide(
            _diagnosis(world, resolution=RESOLUTION_ESCALATE),
            credit_paise=100,
            net_paise_of=net_paise_lookup(by_id),
        )
        assert not decision.auto_resolved
        assert decision.rule == RULE_MODEL_ESCALATED

    def test_a_match_naming_no_rows_is_not_a_match(self, world):
        _, by_id, _, _ = world
        decision = decide(_diagnosis(world), credit_paise=100, net_paise_of=net_paise_lookup(by_id))
        assert decision.rule == RULE_NO_ROWS_CLAIMED

    def test_confidence_cannot_rescue_arithmetic_that_does_not_hold(self, world):
        result, by_id, _, _ = world
        rows = list(result.reconciliation.unclaimed_entity_ids[:2])
        decision = decide(
            _diagnosis(world, settlement_entity_ids=tuple(rows), confidence=1.0),
            credit_paise=1,
            net_paise_of=net_paise_lookup(by_id),
        )
        assert not decision.auto_resolved
        assert decision.rule == RULE_ARITHMETIC_FAILS

    def test_unknown_rows_are_refused(self, world):
        _, by_id, _, _ = world
        decision = decide(
            _diagnosis(world, settlement_entity_ids=("pay_does_not_exist",)),
            credit_paise=100,
            net_paise_of=net_paise_lookup(by_id),
        )
        assert decision.rule == RULE_ARITHMETIC_FAILS

    def test_ambiguous_evidence_vetoes_any_confidence(self, world):
        result, by_id, _, box = world
        collision = next(e for e in result.reconciliation.exceptions if e.bank_txn_id == "BNK00031")
        answer = box.subset_sum(
            target_paise=collision.credit_paise,
            entity_ids=list(collision.candidate_entity_ids),
        )
        from src.tools import ToolCall

        claimed = tuple(answer["solutions"][0])
        decision = decide(
            _diagnosis(
                world,
                bank_txn_id="BNK00031",
                case_id=4,  # the model has misdiagnosed it, deliberately
                settlement_entity_ids=claimed,
                confidence=1.0,
                tool_calls=(ToolCall("subset_sum", {}, answer),),
            ),
            credit_paise=collision.credit_paise,
            net_paise_of=net_paise_lookup(by_id),
        )
        assert not decision.auto_resolved
        assert decision.rule == RULE_AMBIGUOUS_EVIDENCE

    def test_named_adversarial_cases_never_auto_resolve(self, world):
        result, by_id, _, _ = world
        exception = next(e for e in result.reconciliation.exceptions if e.bank_txn_id == "BNK00015")
        rows = tuple(sorted(["pay_1HpHPa2J9bJdKn", "pay_R67FnyfpPM6hUH", "pay_XgdV5DyNS1vlk6"]))
        decision = decide(
            _diagnosis(
                world,
                bank_txn_id="BNK00015",
                case_id=next(int(c) for c in ALWAYS_ESCALATE_CASES),
                settlement_entity_ids=rows,
                confidence=1.0,
            ),
            credit_paise=exception.credit_paise,
            net_paise_of=net_paise_lookup(by_id),
        )
        assert decision.rule == RULE_ALWAYS_ESCALATE

    def test_low_confidence_escalates_with_the_reasoning_attached(self, world):
        result, by_id, _, _ = world
        exception = next(e for e in result.reconciliation.exceptions if e.bank_txn_id == "BNK00029")
        answer_rows = tuple(
            sorted(
                r
                for r in result.reconciliation.unclaimed_entity_ids
                if by_id[r].settlement_id
                == by_id[result.reconciliation.unclaimed_entity_ids[0]].settlement_id
            )
        )
        decision = decide(
            _diagnosis(
                world,
                bank_txn_id=exception.bank_txn_id,
                settlement_entity_ids=answer_rows,
                confidence=AUTO_RESOLVE_THRESHOLD - 0.01,
            ),
            credit_paise=sum(by_id[r].net_paise for r in answer_rows),
            net_paise_of=net_paise_lookup(by_id),
        )
        assert decision.rule == RULE_BELOW_THRESHOLD
        assert "because" in decision.reason

    def test_a_clean_confident_verified_diagnosis_is_booked(self, world):
        result, by_id, _, _ = world
        rows = tuple(sorted(result.reconciliation.unclaimed_entity_ids[:2]))
        decision = decide(
            _diagnosis(world, settlement_entity_ids=rows, confidence=0.97),
            credit_paise=sum(by_id[r].net_paise for r in rows),
            net_paise_of=net_paise_lookup(by_id),
        )
        assert decision.auto_resolved
        assert decision.rule == RULE_AUTO_RESOLVED


@dataclass
class ScriptedAnalyst:
    """A deterministic stand-in for the model. NOT shipped, NOT the classifier.

    It runs one subset_sum and then reasons the way a competent analyst would:
    take the reading that is a complete settlement batch, and if two disjoint
    complete batches both fit, refuse. Its only job is to prove the plumbing
    around the model is correct; the published accuracy numbers come from the
    real model against the recorded cache.
    """

    box: ToolBox
    credit_by_txn: dict[str, int]
    candidates_by_txn: dict[str, tuple[str, ...]]
    name: str = "scripted-analyst"
    _stage: dict = field(default_factory=dict)

    def complete(self, messages, tools):
        txn = next(
            word.strip()
            for word in messages[1]["content"].split("\n")[0].split()
            if word.startswith("BNK")
        )
        stage = self._stage.get(txn, 0)
        self._stage[txn] = stage + 1

        if stage == 0:
            return reply(
                tool_call(
                    "subset_sum",
                    target_paise=self.credit_by_txn[txn],
                    entity_ids=list(self.candidates_by_txn[txn]),
                )
            )

        answer = self.box.subset_sum(
            target_paise=self.credit_by_txn[txn],
            entity_ids=list(self.candidates_by_txn[txn]),
        )
        complete = [
            solution
            for solution, structure in zip(
                answer["solutions"], answer["solution_structure"], strict=True
            )
            if structure["is_complete_batch"]
        ]
        disjoint = len(complete) > 1 and not (set(complete[0]) & set(complete[1]))

        if disjoint or not complete:
            return reply(
                tool_call(
                    "submit_diagnosis",
                    case_id=10 if disjoint else 6,
                    resolution=RESOLUTION_ESCALATE,
                    settlement_entity_ids=[],
                    confidence=0.2,
                    reasoning="more than one complete batch explains this credit exactly",
                )
            )
        return reply(
            tool_call(
                "submit_diagnosis",
                case_id=5 if any(e.startswith("rfnd_") for e in complete[0]) else 4,
                resolution=RESOLUTION_MATCHED,
                settlement_entity_ids=complete[0],
                confidence=0.95,
                reasoning="exactly one complete settlement batch sums to this credit",
            )
        )


class TestEndToEndWithAScriptedAnalyst:
    @pytest.fixture(scope="class")
    def outcome(self):
        result = run_reconciliation("B", db_path="/tmp/ai_e2e.db")
        by_id = {r.entity_id: r for r in result.dataset.settlements}
        box = ToolBox(
            [by_id[e] for e in result.reconciliation.unclaimed_entity_ids],
            universe=result.dataset.settlements,
        )
        analyst = ScriptedAnalyst(
            box=box,
            credit_by_txn={e.bank_txn_id: e.credit_paise for e in result.reconciliation.exceptions},
            candidates_by_txn={
                e.bank_txn_id: e.candidate_entity_ids for e in result.reconciliation.exceptions
            },
        )
        final, diagnoses, decisions = run_classifier(
            result.dataset, result.reconciliation, backend=analyst
        )
        return result, final, diagnoses, decisions

    def test_the_ordinary_consolidated_payouts_get_resolved(self, outcome):
        _, final, _, decisions = outcome
        booked = [d for d in decisions if d.auto_resolved]
        assert len(booked) == 7
        assert len(final.matches) == 63

    def test_the_collisions_are_still_refused(self, outcome):
        _, final, _, decisions = outcome
        refused = {d.bank_txn_id for d in decisions if not d.auto_resolved}
        assert refused == {"BNK00031", "BNK00037"}
        assert {e.bank_txn_id for e in final.exceptions} == refused

    @staticmethod
    def _report(outcome):
        from src.ingest import load_truth
        from src.metrics import score
        from src.pipeline import seed_dir

        result, final, diagnoses, decisions = outcome
        enriched = type(result)(
            run_id=result.run_id,
            seed=result.seed,
            dataset=result.dataset,
            reconciliation=result.reconciliation,
            db_path=result.db_path,
            final=final,
            diagnoses=diagnoses,
            decisions=decisions,
            ai_ran=True,
        )
        return score(enriched, load_truth(seed_dir("B") / "truth.json"))

    def test_the_scorer_sees_no_false_matches(self, outcome):
        report = self._report(outcome)
        assert report.false_matches == ()
        assert report.match_rate == 1.0 - 6 / 80
        assert report.escalation_achieved == report.escalation_required

    def test_the_ai_layer_is_scored_separately_from_the_rules(self, outcome):
        report = self._report(outcome)
        assert report.ai is not None
        assert report.ai.exceptions_seen == 9
        assert report.ai.auto_resolved == 7
        assert report.ai.auto_resolve_precision == 1.0
        assert report.ai.adversarial_escalated == report.ai.adversarial_seen == 2
        assert report.ai.calibration

    def test_both_reports_render_the_ai_section(self, outcome):
        from src.metrics import render_markdown, render_text

        report = self._report(outcome)
        body = render_text(report)
        assert "auto-resolve precision" in body
        assert "Calibration" in body
        assert body.index("FALSE-MATCH RATE") < body.index("auto-resolve precision")

        markdown = render_markdown(report)
        assert "## AI layer" in markdown
        assert "Why each escalation happened" in markdown

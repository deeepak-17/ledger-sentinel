"""The exception classifier: an LLM that reasons through tools.

Everything the deterministic layer refused arrives here as a bank credit with no
reference and a pool of unclaimed settlement rows. The model's job is to say
*what happened* -- which case in the taxonomy, which rows explain the credit, and
how sure it is. It is then the gate, not the model, that decides whether that is
good enough to book.

Three things about this design are deliberate and worth defending:

**The model never computes money.** Every fee, every net, every sum comes back
from a tool that shares its implementation with the deterministic matcher. A
model doing mental arithmetic on paise is a liability dressed as a feature.

**The model is not told the answer, and it is not told the shape of the answer.**
`src/cases.py` carries a `resolvable_by` field recording which layer is supposed
to close each case; it is never sent. The case descriptions below are written
fresh here, describing each phenomenon and nothing about what to do with it, and
a test asserts they contain no verdict language. The taxonomy is domain knowledge
a controller would have. Which case *this* credit is, is the thing being measured.

**The model is told the risk posture, because a controller would be.** The prompt
says plainly that an unresolved item is cheap and a wrong attribution is
expensive, and that two readings with nothing between them should be escalated.
That is policy, not an answer key -- it applies to every exception equally and
tells it nothing about which one is the trap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from config import ACTIVE_CASES, MAX_TOOL_TURNS, MODEL
from src.cases import Case
from src.llm import Backend, Response, default_backend
from src.matcher import Exception_
from src.money import format_inr
from src.schema import BankTxn
from src.tools import TOOL_SCHEMAS, ToolBox, ToolCall

RESOLUTION_MATCHED = "matched"
RESOLUTION_ESCALATE = "escalate"

# Model-facing descriptions. Each says what the phenomenon looks like in the
# data and nothing about how the system should respond to it. Kept here rather
# than in src/cases.py precisely so the internal contract and the prompt cannot
# drift into each other -- cases.py records what we know, this records what the
# model is told.
CASE_BRIEFS: dict[Case, str] = {
    Case.CLEAN_MATCH: (
        "One order, one settlement row, one credit. The credit equals the order "
        "amount because no fee applied on that method."
    ),
    Case.FEE_AND_GST: (
        "The credit is short of the order amount by the platform fee plus 18% GST "
        "levied on that fee."
    ),
    Case.SETTLEMENT_TIMING: (
        "The amount agrees but the credit lands some days after capture, stretched "
        "by weekends or bank holidays."
    ),
    Case.BATCHED_PAYOUT: (
        "Several settlement rows leave as one consolidated credit. No single row "
        "explains the figure; a combination of them does."
    ),
    Case.NETTED_REFUND: (
        "A refund is deducted inside the same payout rather than debited "
        "separately, so the credit is smaller than the payments in it and the "
        "original fee is not returned."
    ),
    Case.PARTIAL_REFUND: (
        "Part of an order is refunded after capture but before settlement, so "
        "neither the gross nor the net order amount matches the credit."
    ),
    Case.CHARGEBACK: (
        "A dispute reverses an earlier cycle's payment inside this payout. The "
        "offsetting payment is not among these rows."
    ),
    Case.ROUNDING_DRIFT: (
        "The credit differs from the computed net by one or two paise, because two "
        "systems rounded the same fee independently."
    ),
    Case.DUPLICATE_PAYMENT: (
        "One order carries two successful payments -- a retry. Only one was settled."
    ),
    Case.AMOUNT_COLLISION: (
        "Two or more different sets of settlement rows sum to exactly the same "
        "figure as this credit, and no field in the data indicates which set the "
        "money actually came from."
    ),
    Case.UTR_REUSE: (
        "A bank reference number that already appeared on an earlier credit "
        "reappears on this one with a plausible amount."
    ),
}

SUBMIT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_diagnosis",
        "description": (
            "Record your conclusion about this bank credit and end the "
            "investigation. Call this exactly once, when you are done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "integer",
                    "enum": [int(c) for c in Case],
                    "description": "which case in the taxonomy this credit is",
                },
                "resolution": {
                    "type": "string",
                    "enum": [RESOLUTION_MATCHED, RESOLUTION_ESCALATE],
                    "description": (
                        "'matched' if you can say which settlement rows this credit "
                        "paid; 'escalate' if a human has to decide"
                    ),
                },
                "settlement_entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "the rows this credit paid. Required when resolution is "
                        "'matched'; leave empty when escalating."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "description": (
                        "0 to 1. This is read by an automatic gate: above a "
                        "threshold the resolution is booked without review."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Two or three sentences a finance controller could act on: "
                        "what the evidence was and what it ruled out."
                    ),
                },
            },
            "required": [
                "case_id",
                "resolution",
                "settlement_entity_ids",
                "confidence",
                "reasoning",
            ],
            "additionalProperties": False,
        },
    },
}

ALL_TOOLS: list[dict[str, Any]] = [*TOOL_SCHEMAS, SUBMIT_TOOL]


def _taxonomy_block() -> str:
    lines = []
    for case_id in sorted(int(c) for c in Case):
        case = Case(case_id)
        active = "" if case_id in ACTIVE_CASES else "  (rare in this cycle)"
        lines.append(
            f"  {case_id:>2}. {case.name.replace('_', ' ').title()}{active}\n"
            f"      {CASE_BRIEFS[case]}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = f"""\
You are a reconciliation analyst for a merchant using an Indian payment gateway.

Three sources disagree: the merchant's orders, the gateway's settlement report,
and the bank statement. Credits that carried a bank reference (UTR) have already
been matched automatically and their settlement rows removed from the pool. What
reaches you is a credit with nothing to join on -- a consolidated payout -- and
the settlement rows still unaccounted for.

Your job is to say what happened, using the tools. Rules:

1. Do not do arithmetic yourself. Call `expected_fee` for any fee or GST figure
   and `subset_sum` for any combination. The tools share their implementation
   with the automatic matcher, so their answers are authoritative.
2. Start by looking at what is available (`query_candidates`), then test
   explanations against the credit.
3. A real consolidated credit is one payout leaving the gateway, and every row in
   a payout carries the same settlement_id. `subset_sum` tells you which
   settlement batches each solution draws from and whether it takes one whole.
4. Finish by calling `submit_diagnosis` exactly once.

The taxonomy of cases:

{_taxonomy_block()}

How to weigh your answer. An unresolved item costs a controller ten minutes. A
wrong attribution closes a discrepancy that was real and nobody looks at it
again. The costs are not symmetric, so:

- If exactly one explanation survives the evidence, say so and be confident.
- If two or more explanations fit the credit exactly and nothing in the data
  distinguishes them, that IS the finding. Set resolution to "escalate", state
  both readings in your reasoning, and do not pick one to look decisive.
- Confidence is read by an automatic gate. Do not inflate it to get an answer
  booked; a low number is a legitimate result.
"""


@dataclass(frozen=True)
class Diagnosis:
    """What the model concluded, before the gate has had its say."""

    bank_txn_id: str
    case_id: int | None
    resolution: str
    settlement_entity_ids: tuple[str, ...]
    confidence: float
    reasoning: str
    tool_calls: tuple[ToolCall, ...]
    turns: int
    input_tokens: int
    output_tokens: int
    model: str
    backend: str

    @property
    def used_tools(self) -> tuple[str, ...]:
        return tuple(call.name for call in self.tool_calls)


def _evidence_prompt(exception: Exception_, line: BankTxn) -> str:
    return (
        f"Bank credit {line.txn_id}\n"
        f"  value date   {line.value_date.isoformat()}\n"
        f"  amount       {format_inr(line.credit_paise)}  ({line.credit_paise} paise)\n"
        f"  narration    {line.description!r}\n"
        f"  reference    {line.utr or 'none -- this is why it reached you'}\n\n"
        f"The automatic matcher raised this as `{exception.rule}`.\n"
        f"{len(exception.candidate_entity_ids)} settlement rows are unaccounted for "
        f"inside the date window.\n\n"
        "Investigate and submit a diagnosis."
    )


def _assistant_message(response: Response) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in response.tool_calls
        ]
    return message


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _escalation(
    exception: Exception_,
    reason: str,
    trace: list[ToolCall],
    turns: int,
    tokens: tuple[int, int],
    model: str,
    backend: str,
) -> Diagnosis:
    """The outcome whenever the loop cannot produce a clean diagnosis.

    Failing to a match would be the dangerous direction; failing to an
    escalation costs a controller ten minutes and is always safe.
    """
    return Diagnosis(
        bank_txn_id=exception.bank_txn_id,
        case_id=None,
        resolution=RESOLUTION_ESCALATE,
        settlement_entity_ids=(),
        confidence=0.0,
        reasoning=reason,
        tool_calls=tuple(trace),
        turns=turns,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        model=model,
        backend=backend,
    )


def classify(
    exception: Exception_,
    line: BankTxn,
    toolbox: ToolBox,
    backend: Backend | None = None,
) -> Diagnosis:
    """Run one exception through the tool-use loop."""
    backend = backend or default_backend()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _evidence_prompt(exception, line)},
    ]
    trace: list[ToolCall] = []
    input_tokens = output_tokens = 0
    model_name = MODEL
    backend_name = getattr(backend, "name", "unknown")

    for turn in range(1, MAX_TOOL_TURNS + 1):
        response = backend.complete(messages, ALL_TOOLS)
        input_tokens += response.input_tokens
        output_tokens += response.output_tokens
        model_name = response.model
        backend_name = response.backend

        if not response.tool_calls:
            # The model answered in prose instead of submitting. One nudge, then
            # we take the escalation rather than parsing free text for a verdict.
            if turn >= MAX_TOOL_TURNS:
                break
            messages.append(_assistant_message(response))
            messages.append(
                {
                    "role": "user",
                    "content": "Call submit_diagnosis to record your conclusion.",
                }
            )
            continue

        messages.append(_assistant_message(response))

        for call in response.tool_calls:
            arguments = _parse_arguments(call["arguments"])

            if call["name"] == "submit_diagnosis":
                trace.append(ToolCall("submit_diagnosis", arguments, "submitted"))
                return Diagnosis(
                    bank_txn_id=exception.bank_txn_id,
                    case_id=arguments.get("case_id"),
                    resolution=(arguments.get("resolution") or RESOLUTION_ESCALATE),
                    settlement_entity_ids=tuple(
                        sorted(arguments.get("settlement_entity_ids") or ())
                    ),
                    confidence=float(arguments.get("confidence") or 0.0),
                    reasoning=(arguments.get("reasoning") or "").strip(),
                    tool_calls=tuple(trace),
                    turns=turn,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model=model_name,
                    backend=backend_name,
                )

            result = toolbox.dispatch(call["name"], arguments)
            trace.append(ToolCall(call["name"], arguments, result))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, sort_keys=True, default=str),
                }
            )

    return _escalation(
        exception,
        (
            f"the classifier did not reach a conclusion within {MAX_TOOL_TURNS} "
            "tool-use turns; escalated rather than guessed"
        ),
        trace,
        MAX_TOOL_TURNS,
        (input_tokens, output_tokens),
        model_name,
        backend_name,
    )

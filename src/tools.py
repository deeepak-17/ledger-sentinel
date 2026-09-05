"""The three tools the classifier reasons with.

Every one of these is pure arithmetic over data the model cannot otherwise see,
and every one is unit-tested without the model in the loop. That split is the
point: **the model classifies and explains, it never computes money.** Anything a
tool returns was computed by the same functions the deterministic layer uses, so
the two halves of the system cannot disagree about a fee, a net, or a sum.

The most important design decision in this file is one line in `subset_sum`:

    it returns EVERY solution, not the first one.

Returning the first is how a reconciliation system auto-resolves case 10. The
whole adversarial construction is that a single row explains the credit exactly
*and* a disjoint pair explains it exactly; a tool that stops at the first hit
hands the model one answer and hides the ambiguity that was the entire finding.
Returning all of them makes "there are two readings and nothing chooses between
them" a fact the model has to confront rather than a fact the tooling erased.

The toolbox is constructed over the rows the deterministic layer left unclaimed.
The model therefore cannot reach a settlement row that a UTR already spoke for --
its search space contains no known-wrong answers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations
from typing import Any

from config import DATE_WINDOW_DAYS, MAX_SUBSET_SIZE
from src.money import expected_deduction, format_inr
from src.schema import SettlementRow

# A single credit with 20-odd candidates and k up to 6 is ~60k combinations,
# which is instant. This cap is not about speed -- it is a guard against handing
# the model a wall of coincidences it will feel obliged to choose from.
MAX_SOLUTIONS_RETURNED = 25


class ToolError(ValueError):
    """A tool was called with arguments it cannot honour. Surfaced to the model
    as a result rather than raised, so a bad call costs a turn and not the run."""


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation, recorded for the audit trail."""

    name: str
    arguments: dict[str, Any]
    result: Any

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments, "result": self.result}


def _row_view(row: SettlementRow) -> dict[str, Any]:
    """What the model is allowed to see about a settlement row.

    Note what is absent: nothing here says which case this row belongs to, and
    `truth.json` is not loaded anywhere in this process. The model is working
    from the same evidence a controller would have.
    """
    return {
        "entity_id": row.entity_id,
        "entity_type": row.entity_type,
        "settlement_id": row.settlement_id,
        "order_id": row.order_id,
        "method": row.method,
        "amount_paise": row.amount_paise,
        "fee_paise": row.fee_paise,
        "tax_paise": row.tax_paise,
        "net_paise": row.net_paise,
        "on_hold": row.on_hold,
        "settled_at": row.settled_at.isoformat(),
    }


class ToolBox:
    """Bound to one run's unclaimed settlement rows.

    `universe` is every settlement row in the cycle, claimed or not. It is used
    only to judge whether a combination is a *complete* payout batch: a group
    that looks whole among the unclaimed rows but has had two rows taken by a
    UTR is not a payout, and calling it one would be the tool lying by omission.
    """

    def __init__(
        self,
        rows: Iterable[SettlementRow],
        universe: Iterable[SettlementRow] | None = None,
    ) -> None:
        self._rows: tuple[SettlementRow, ...] = tuple(
            sorted(rows, key=lambda r: (r.settled_at, r.entity_id))
        )
        self._by_id = {row.entity_id: row for row in self._rows}

        everything = tuple(universe) if universe is not None else self._rows
        self._batch_members: dict[str, set[str]] = defaultdict(set)
        for row in everything:
            self._batch_members[row.settlement_id].add(row.entity_id)

    # -- tool 1 ------------------------------------------------------------
    def query_candidates(
        self,
        *,
        value_date: str | None = None,
        window_days: int = DATE_WINDOW_DAYS,
        amount_paise: int | None = None,
        tolerance_paise: int = 0,
        order_id: str | None = None,
        method: str | None = None,
        entity_type: str | None = None,
    ) -> dict[str, Any]:
        """Settlement rows still unaccounted for, filtered. All filters are ANDed."""
        rows = list(self._rows)

        if value_date is not None:
            try:
                centre = date.fromisoformat(value_date)
            except ValueError as exc:
                raise ToolError(f"value_date {value_date!r} is not an ISO date") from exc
            if window_days < 0:
                raise ToolError("window_days must not be negative")
            span = timedelta(days=window_days)
            rows = [r for r in rows if abs(r.settled_at - centre) <= span]

        if amount_paise is not None:
            if tolerance_paise < 0:
                raise ToolError("tolerance_paise must not be negative")
            rows = [r for r in rows if abs(r.net_paise - amount_paise) <= tolerance_paise]

        if order_id is not None:
            rows = [r for r in rows if r.order_id == order_id]
        if method is not None:
            rows = [r for r in rows if r.method == method]
        if entity_type is not None:
            rows = [r for r in rows if r.entity_type == entity_type]

        return {
            "count": len(rows),
            "candidates": [_row_view(r) for r in rows],
        }

    # -- tool 2 ------------------------------------------------------------
    def expected_fee(self, *, amount_paise: int, method: str) -> dict[str, Any]:
        """The fee and GST that should have been deducted from a payment.

        A thin wrapper over `src.money` on purpose. If the model wants to check
        whether a shortfall is explained by fees, it gets the same answer the
        matcher would have got -- there is exactly one implementation of this
        arithmetic in the repository.
        """
        if amount_paise < 0:
            raise ToolError("amount_paise must not be negative")
        try:
            fee, tax = expected_deduction(amount_paise, method)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "amount_paise": amount_paise,
            "method": method,
            "fee_paise": fee,
            "tax_paise": tax,
            "net_paise": amount_paise - fee - tax,
            "human": (
                f"{format_inr(amount_paise)} less fee {format_inr(fee)} and GST "
                f"{format_inr(tax)} nets {format_inr(amount_paise - fee - tax)}"
            ),
        }

    # -- tool 3 ------------------------------------------------------------
    def subset_sum(
        self,
        *,
        target_paise: int,
        entity_ids: Sequence[str] | None = None,
        max_k: int = MAX_SUBSET_SIZE,
    ) -> dict[str, Any]:
        """Every combination of candidate rows whose net sums to the target.

        Rows may be negative -- a refund debits the payout -- so this cannot
        prune by sorting and simply enumerates combinations up to `max_k`. That
        is what makes case 5 findable: no subset of the *payment* rows reaches
        the credit, but payments-plus-refund does.

        Returns ALL solutions. See the module docstring for why that matters
        more than any other line in this file.
        """
        if max_k < 1:
            raise ToolError("max_k must be at least 1")
        if max_k > MAX_SUBSET_SIZE:
            raise ToolError(
                f"max_k {max_k} exceeds MAX_SUBSET_SIZE ({MAX_SUBSET_SIZE}); a "
                "combination that wide is more likely a coincidence than a payout"
            )

        if entity_ids is None:
            pool = list(self._rows)
        else:
            unknown = [eid for eid in entity_ids if eid not in self._by_id]
            if unknown:
                raise ToolError(
                    f"unknown or already-claimed entity_ids: {sorted(unknown)}; "
                    "only unclaimed settlement rows can be combined"
                )
            pool = [self._by_id[eid] for eid in entity_ids]

        solutions: list[tuple[str, ...]] = []
        for size in range(1, min(max_k, len(pool)) + 1):
            for combo in combinations(pool, size):
                if sum(row.net_paise for row in combo) == target_paise:
                    solutions.append(tuple(sorted(row.entity_id for row in combo)))

        solutions.sort(key=lambda s: (len(s), s))
        truncated = len(solutions) > MAX_SOLUTIONS_RETURNED
        shown = solutions[:MAX_SOLUTIONS_RETURNED]

        return {
            "target_paise": target_paise,
            "pool_size": len(pool),
            "max_k": max_k,
            "solution_count": len(solutions),
            "solutions": [list(s) for s in shown],
            "solution_structure": [self._describe_batches(s) for s in shown],
            "truncated": truncated,
            "disjoint_alternatives": _disjoint_pairs(shown),
            "note": (
                "more than one combination sums to the target; if no evidence "
                "distinguishes them the correct answer is to escalate"
                if len(solutions) > 1
                else "exactly one combination sums to the target"
                if solutions
                else "no combination of these rows sums to the target"
            ),
        }

    def _describe_batches(self, solution: tuple[str, ...]) -> dict[str, Any]:
        """Which payouts a combination is drawn from, and whether it takes them whole.

        This is a *fact about the data*, not a verdict. A real consolidated
        credit is one payout leaving the gateway, and every row in a payout
        carries the same `settlement_id` -- so a combination that is exactly one
        complete batch is structurally a payout, and one that takes three rows
        from three unrelated batches is structurally a coincidence.

        It is deliberately not a score and deliberately not applied here. The
        model is handed the structure and has to decide whether it settles the
        question, because on the adversarial case it does not: both readings of
        an amount collision are complete batches, which is exactly why nothing
        in the data resolves it.
        """
        spanned = sorted({self._by_id[eid].settlement_id for eid in solution})
        members = set(solution)
        return {
            "settlement_ids": spanned,
            "spans_batches": len(spanned),
            "is_complete_batch": (len(spanned) == 1 and self._batch_members[spanned[0]] == members),
            "rows_missing_from_batches": sorted(
                eid for sid in spanned for eid in self._batch_members[sid] if eid not in members
            ),
        }

    # -- plumbing ----------------------------------------------------------
    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool by name. Errors come back as results, not exceptions --
        a malformed call should cost the model a turn to correct, not kill the run."""
        handlers = {
            "query_candidates": self.query_candidates,
            "expected_fee": self.expected_fee,
            "subset_sum": self.subset_sum,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"no such tool {name!r}; available: {sorted(handlers)}"}
        try:
            return handler(**arguments)
        except ToolError as exc:
            return {"error": str(exc)}
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}


def _disjoint_pairs(solutions: Sequence[tuple[str, ...]]) -> list[list[list[str]]]:
    """Pairs of solutions that share no row.

    Two overlapping solutions are usually the same story told twice. Two
    *disjoint* solutions are two genuinely different stories about where the
    money came from, which is the shape of an unresolvable collision. Surfacing
    them explicitly means the model does not have to notice it unaided.
    """
    pairs: list[list[list[str]]] = []
    for left, right in combinations(solutions, 2):
        if not set(left) & set(right):
            pairs.append([list(left), list(right)])
    return pairs[:10]


# ---------------------------------------------------------------------------
# schemas handed to the model
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_candidates",
            "description": (
                "List settlement rows that are still unaccounted for, optionally "
                "filtered by settlement date window, net amount, order, method or "
                "entity type. These are the only rows available -- anything a bank "
                "reference already claimed has been removed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "value_date": {
                        "type": "string",
                        "description": "ISO date to centre the window on, e.g. 2026-09-14",
                    },
                    "window_days": {"type": "integer", "description": "default 3"},
                    "amount_paise": {"type": "integer"},
                    "tolerance_paise": {"type": "integer"},
                    "order_id": {"type": "string"},
                    "method": {
                        "type": "string",
                        "enum": ["card", "upi", "netbanking", "wallet"],
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": ["payment", "refund", "adjustment"],
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expected_fee",
            "description": (
                "The platform fee and 18% GST that should have been deducted from a "
                "payment of this amount on this method, and the resulting net. Use "
                "this instead of doing the arithmetic yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount_paise": {"type": "integer"},
                    "method": {
                        "type": "string",
                        "enum": ["card", "upi", "netbanking", "wallet"],
                    },
                },
                "required": ["amount_paise", "method"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subset_sum",
            "description": (
                "Every combination of up to max_k unclaimed settlement rows whose "
                "net amounts sum exactly to the target. Refund rows count as "
                "negative. Returns ALL solutions, and flags any two that share no "
                "row -- two disjoint solutions mean the credit has more than one "
                "arithmetically perfect explanation. Each solution also reports "
                "which settlement batches it draws from and whether it takes a "
                "batch whole, since a real consolidated credit is one payout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_paise": {"type": "integer"},
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "restrict the pool; omit to use every unclaimed row",
                    },
                    "max_k": {"type": "integer", "description": f"default {MAX_SUBSET_SIZE}"},
                },
                "required": ["target_paise"],
                "additionalProperties": False,
            },
        },
    },
]

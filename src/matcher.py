"""The deterministic layer.

The thesis of this system is one sentence: **a match requires an identifier.**

Amount agreement alone never closes a match, because amounts collide -- that is
what case 10 exists to prove. So the only thing that opens a match here is a UTR
shared by a bank line and a settlement payout; the amount and the value date are
then used as *confirmation*, and a disagreement in either produces an exception
rather than a match. This is why the false-match rate can be reported as a
headline number instead of an aspiration.

What this module deliberately does not do
-----------------------------------------
**No subset-sum.** A bank credit with no reference could be explained by some
combination of settlement rows, and finding those combinations is deterministic
arithmetic that belongs in `src/tools.py` for the classifier to call. But
*deciding what to do when two disjoint combinations both explain the credit
exactly* is a judgement, and pushing that judgement down here would mean the
matcher silently picking one. It would raise the match rate and it would be the
single most expensive bug in the system. Everything without an identifier leaves
here as an exception, carrying its candidate rows, and M3 decides.

**No mutation, no I/O.** `reconcile()` is a pure function of the dataset.
Persistence and the audit trail are wrapped around it by `src/pipeline.py`, so
this logic is testable without a database and identical between the CLI, the API
and the tests.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from config import DATE_WINDOW_DAYS, ROUNDING_TOLERANCE_PAISE
from src.money import format_inr
from src.schema import BankTxn, Dataset, SettlementRow

LAYER = "deterministic"

# Rules that close a match.
RULE_UTR_EXACT = "utr_exact"
RULE_UTR_WITHIN_ROUNDING = "utr_within_rounding"

# Rules that raise an exception. Each names the specific evidence that failed,
# because "unmatched" is not an exception report -- a reason is.
RULE_UTR_AMOUNT_MISMATCH = "utr_amount_mismatch"
RULE_UTR_DATE_OUTSIDE_WINDOW = "utr_date_outside_window"
RULE_UTR_ALREADY_CLAIMED = "utr_already_claimed"
RULE_UNKNOWN_UTR = "unknown_utr"
RULE_NO_IDENTIFIER = "no_identifier"
RULE_NOT_A_CREDIT = "not_a_credit"

MATCH_RULES = frozenset({RULE_UTR_EXACT, RULE_UTR_WITHIN_ROUNDING})


@dataclass(frozen=True)
class Match:
    """A bank line explained, with the evidence that explained it."""

    bank_txn_id: str
    rule: str
    settlement_entity_ids: tuple[str, ...]
    order_ids: tuple[str, ...]
    credit_paise: int
    expected_paise: int
    delta_paise: int
    reason: str
    layer: str = LAYER

    @property
    def subject_id(self) -> str:
        return self.bank_txn_id


@dataclass(frozen=True)
class Exception_:
    """A bank line the deterministic layer refuses to explain, and why.

    `candidate_entity_ids` is what the next layer gets to work with: the
    settlement rows sitting in this credit's date window that no identifier has
    already claimed. Handing them over here keeps the classifier's search space
    honest -- it cannot reach for a row that was already spoken for.
    """

    bank_txn_id: str
    rule: str
    reason: str
    credit_paise: int
    candidate_entity_ids: tuple[str, ...]
    layer: str = LAYER

    @property
    def subject_id(self) -> str:
        return self.bank_txn_id


@dataclass(frozen=True)
class Reconciliation:
    matches: tuple[Match, ...]
    exceptions: tuple[Exception_, ...]
    unclaimed_entity_ids: tuple[str, ...] = field(default=())

    @property
    def matched_order_ids(self) -> frozenset[str]:
        return frozenset(oid for m in self.matches for oid in m.order_ids)

    @property
    def matched_bank_txn_ids(self) -> frozenset[str]:
        return frozenset(m.bank_txn_id for m in self.matches)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _group_by_utr(settlements: tuple[SettlementRow, ...]) -> dict[str, list[SettlementRow]]:
    """A payout, not a row, is the unit a bank credit corresponds to.

    Cases 1/2/3/8 happen to put one row under one UTR, but a consolidated payout
    puts several under the same one. Grouping means the same rule handles both
    and we are not quietly relying on the dataset's current shape.
    """
    grouped: dict[str, list[SettlementRow]] = defaultdict(list)
    for row in settlements:
        grouped[row.utr].append(row)
    return {utr: sorted(rows, key=lambda r: r.entity_id) for utr, rows in grouped.items()}


def _expected_paise(rows: list[SettlementRow]) -> int:
    """What the payout says should land: credits less debits, in paise."""
    return sum(row.net_paise for row in rows)


def _entity_ids(rows: list[SettlementRow]) -> tuple[str, ...]:
    return tuple(row.entity_id for row in rows)


def _order_ids(rows: list[SettlementRow]) -> tuple[str, ...]:
    """Distinct orders behind a payout, in a stable order.

    A refund row points at the same order as the payment it reverses, so this
    deduplicates -- otherwise a netted refund would inflate the order count.
    """
    seen: dict[str, None] = {}
    for row in rows:
        if row.order_id is not None:
            seen.setdefault(row.order_id, None)
    return tuple(seen)


def _date_span(rows: list[SettlementRow]) -> tuple[date, date]:
    dates = [row.settled_at for row in rows]
    return min(dates), max(dates)


def _within_window(value_date: date, rows: list[SettlementRow]) -> bool:
    earliest, latest = _date_span(rows)
    window = timedelta(days=DATE_WINDOW_DAYS)
    return earliest - window <= value_date <= latest + window


def _candidates(
    line: BankTxn, settlements: tuple[SettlementRow, ...], claimed: set[str]
) -> tuple[str, ...]:
    """Settlement rows that could plausibly belong to an unidentified credit:
    inside the date window and not already claimed by an identifier."""
    window = timedelta(days=DATE_WINDOW_DAYS)
    return tuple(
        row.entity_id
        for row in settlements
        if row.entity_id not in claimed
        and abs((row.settled_at - line.value_date).days) <= window.days
    )


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def reconcile(dataset: Dataset) -> Reconciliation:
    """Run the deterministic layer over one dataset.

    Two passes on purpose. Identified credits are resolved first so that the
    rows they consume are removed from the candidate pool before any
    unidentified credit is considered. Doing it in one pass would let an
    unidentified credit be offered rows that a UTR was about to claim, which
    would hand the classifier a search space containing known-wrong answers.
    """
    by_utr = _group_by_utr(dataset.settlements)
    bank_lines = sorted(dataset.bank, key=lambda b: b.txn_id)

    matches: list[Match] = []
    exceptions: list[Exception_] = []
    claimed: set[str] = set()
    claimed_utrs: dict[str, str] = {}

    unidentified: list[BankTxn] = []

    # --- pass 1: credits that carry an identifier -------------------------
    for line in bank_lines:
        if line.credit_paise <= 0:
            exceptions.append(
                Exception_(
                    bank_txn_id=line.txn_id,
                    rule=RULE_NOT_A_CREDIT,
                    reason=(
                        "statement line is a debit; the settlement report describes "
                        "money coming in, so this needs a human to classify it"
                    ),
                    credit_paise=line.credit_paise,
                    candidate_entity_ids=(),
                )
            )
            continue

        if line.utr is None:
            unidentified.append(line)
            continue

        rows = by_utr.get(line.utr)
        if rows is None:
            exceptions.append(
                Exception_(
                    bank_txn_id=line.txn_id,
                    rule=RULE_UNKNOWN_UTR,
                    reason=(
                        f"bank reference {line.utr} appears on the statement but on no "
                        "settlement row in this cycle; the payout may belong to an "
                        "earlier export"
                    ),
                    credit_paise=line.credit_paise,
                    candidate_entity_ids=(),
                )
            )
            continue

        if line.utr in claimed_utrs:
            # Case 11 territory. An identifier that matches twice is an identifier
            # that has stopped identifying, and the earlier claim does not make
            # this credit true.
            exceptions.append(
                Exception_(
                    bank_txn_id=line.txn_id,
                    rule=RULE_UTR_ALREADY_CLAIMED,
                    reason=(
                        f"bank reference {line.utr} was already matched to "
                        f"{claimed_utrs[line.utr]}; a reused reference cannot close a "
                        "second match"
                    ),
                    credit_paise=line.credit_paise,
                    candidate_entity_ids=_entity_ids(rows),
                )
            )
            continue

        if not _within_window(line.value_date, rows):
            earliest, latest = _date_span(rows)
            exceptions.append(
                Exception_(
                    bank_txn_id=line.txn_id,
                    rule=RULE_UTR_DATE_OUTSIDE_WINDOW,
                    reason=(
                        f"reference {line.utr} matches, but the credit is dated "
                        f"{line.value_date.isoformat()} while the payout settled "
                        f"{earliest.isoformat()}..{latest.isoformat()} -- outside the "
                        f"{DATE_WINDOW_DAYS}-day window, so the identifier is not enough"
                    ),
                    credit_paise=line.credit_paise,
                    candidate_entity_ids=_entity_ids(rows),
                )
            )
            continue

        expected = _expected_paise(rows)
        delta = line.credit_paise - expected

        if delta == 0:
            rule, reason = (
                RULE_UTR_EXACT,
                f"reference {line.utr} resolves to {len(rows)} settlement row(s) "
                f"netting {format_inr(expected)}, which the credit matches exactly",
            )
        elif abs(delta) <= ROUNDING_TOLERANCE_PAISE:
            rule, reason = (
                RULE_UTR_WITHIN_ROUNDING,
                f"reference {line.utr} resolves to {format_inr(expected)}; credit "
                f"differs by {delta:+d}p, within the {ROUNDING_TOLERANCE_PAISE}p "
                "tolerance for independent fee rounding",
            )
        else:
            exceptions.append(
                Exception_(
                    bank_txn_id=line.txn_id,
                    rule=RULE_UTR_AMOUNT_MISMATCH,
                    reason=(
                        f"reference {line.utr} resolves to a payout of "
                        f"{format_inr(expected)} but the credit is "
                        f"{format_inr(line.credit_paise)}, off by {delta:+d}p -- beyond "
                        "rounding, so the identifier agreeing is not sufficient"
                    ),
                    credit_paise=line.credit_paise,
                    candidate_entity_ids=_entity_ids(rows),
                )
            )
            continue

        matches.append(
            Match(
                bank_txn_id=line.txn_id,
                rule=rule,
                settlement_entity_ids=_entity_ids(rows),
                order_ids=_order_ids(rows),
                credit_paise=line.credit_paise,
                expected_paise=expected,
                delta_paise=delta,
                reason=reason,
            )
        )
        claimed.update(_entity_ids(rows))
        claimed_utrs[line.utr] = line.txn_id

    # --- pass 2: credits with nothing to join on --------------------------
    for line in unidentified:
        candidates = _candidates(line, dataset.settlements, claimed)
        exceptions.append(
            Exception_(
                bank_txn_id=line.txn_id,
                rule=RULE_NO_IDENTIFIER,
                reason=(
                    f"consolidated credit of {format_inr(line.credit_paise)} on "
                    f"{line.value_date.isoformat()} carries no bank reference; "
                    f"{len(candidates)} unclaimed settlement row(s) sit in the "
                    f"{DATE_WINDOW_DAYS}-day window. The deterministic layer will not "
                    "guess a combination -- escalated to the classifier"
                ),
                credit_paise=line.credit_paise,
                candidate_entity_ids=candidates,
            )
        )

    unclaimed = tuple(
        sorted(row.entity_id for row in dataset.settlements if row.entity_id not in claimed)
    )

    return Reconciliation(
        matches=tuple(matches),
        exceptions=tuple(sorted(exceptions, key=lambda e: e.bank_txn_id)),
        unclaimed_entity_ids=unclaimed,
    )

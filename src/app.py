"""The demo: three CSVs in, a reconciled ledger and an exception list out.

`make demo`. Deliberately thin -- this is a window onto the pipeline, not a
second implementation of it. Every number on this page comes from the same
`run_reconciliation` and `score` that `make metrics` and the API call, so a
figure on screen can always be reproduced at a terminal.

The page is laid out in the order the argument runs: the false-match rate first,
then the match rate, then the exception list with a reason on every row, then the
audit trail for whichever line you click. The adversarial case gets its own panel
because it is the most interesting thing here -- a credit the system could close
and refuses to.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from src.ingest import load_truth
from src.llm import CACHE_PATH, CacheMiss, ResponseCache
from src.matcher import RULE_NO_IDENTIFIER
from src.metrics import Report, score
from src.money import format_inr
from src.pipeline import RunResult, run_reconciliation, seed_dir

st.set_page_config(page_title="Ledger Sentinel", page_icon="=", layout="wide")

REQUIRED_FILES = ("orders.csv", "settlements.csv", "bank.csv")


def _cache_is_recorded() -> bool:
    return CACHE_PATH.exists() and len(ResponseCache()) > 0


@st.cache_data(show_spinner=False)
def _run(seed: str, use_ai: bool, workspace: str | None) -> tuple[RunResult, Report | None]:
    source = Path(workspace) if workspace else None
    db_path = Path(tempfile.gettempdir()) / f"ledger_demo_{seed}_{int(use_ai)}.db"
    result = run_reconciliation(seed, db_path=db_path, use_ai=use_ai, source=source)
    truth_path = (source or seed_dir(seed)) / "truth.json"
    report = score(result, load_truth(truth_path)) if truth_path.exists() else None
    return result, report


def _headline(report: Report | None, result: RunResult) -> None:
    outcome = result.outcome
    left, middle, right, far = st.columns(4)

    if report is not None:
        left.metric(
            "False-match rate",
            f"{report.false_match_rate * 100:.1f}%",
            help=(
                "Matches the system booked that were wrong. Quoted first because a "
                "wrong match closes a real discrepancy and nobody looks again, "
                "while an unresolved item costs ten minutes."
            ),
        )
    else:
        left.metric("False-match rate", "n/a", help="no ground truth for uploaded files")

    matched = len(outcome.matched_order_ids)
    middle.metric(
        "Match rate",
        f"{matched / len(result.dataset.orders) * 100:.1f}%",
        f"{matched} of {len(result.dataset.orders)} orders",
    )
    right.metric("Exceptions", len(outcome.exceptions), "each with a reason")
    if report is not None:
        far.metric(
            "Must-escalate refused",
            f"{report.escalation_achieved}/{report.escalation_required}",
            help="cases where no evidence can decide, and confidence would be the failure",
        )
    else:
        far.metric("Settlement rows", len(result.dataset.settlements))


def _matches_frame(result: RunResult) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "bank line": m.bank_txn_id,
                "layer": m.layer,
                "rule": m.rule,
                "credit": format_inr(m.credit_paise),
                "drift": f"{m.delta_paise:+d}p",
                "orders": len(m.order_ids),
                "rows": ", ".join(m.settlement_entity_ids),
            }
            for m in result.outcome.matches
        ]
    )


def _exceptions_frame(result: RunResult, report: Report | None) -> pd.DataFrame:
    planted = {u.bank_txn_id: u for u in (report.unresolved if report else ())}
    return pd.DataFrame(
        [
            {
                "bank line": e.bank_txn_id,
                "credit": format_inr(e.credit_paise),
                "layer": e.layer,
                "rule": e.rule,
                "candidates": len(e.candidate_entity_ids),
                "ground truth": (
                    f"case {planted[e.bank_txn_id].planted_case_id}"
                    if e.bank_txn_id in planted and planted[e.bank_txn_id].planted_case_id
                    else "-"
                ),
                "reason": e.reason,
            }
            for e in result.outcome.exceptions
        ]
    )


def _audit_frame(result: RunResult, subject_id: str) -> pd.DataFrame:
    from src import db
    from src.audit import decisions_for

    conn = db.connect(result.db_path)
    try:
        rows = decisions_for(conn, result.run_id, subject_id)
    finally:
        conn.close()
    return pd.DataFrame(
        [
            {
                "#": row["seq"],
                "layer": row["layer"],
                "rule": row["rule"],
                "decision": row["decision"],
                "reason": row["reason"],
            }
            for row in rows
        ]
    )


def main() -> None:
    st.title("Ledger Sentinel")
    st.caption(
        "Three-way settlement reconciliation. A deterministic matcher closes "
        "everything carrying a bank reference; a classifier reasoning through "
        "tools diagnoses the rest, and a gate decides what may be booked without "
        "a human."
    )

    with st.sidebar:
        st.header("Input")
        mode = st.radio("Data", ["Bundled seed", "Upload three CSVs"], index=0)
        workspace = None
        seed = "B"

        if mode == "Bundled seed":
            seed = st.radio(
                "Seed",
                ["B", "A"],
                index=0,
                help="A is what thresholds are tuned on. B is held out and is what gets reported.",
            )
        else:
            uploaded = st.file_uploader(
                "orders.csv, settlements.csv, bank.csv", accept_multiple_files=True
            )
            names = {f.name for f in uploaded or ()}
            if uploaded and set(REQUIRED_FILES) <= names:
                workspace = tempfile.mkdtemp()
                for file in uploaded:
                    (Path(workspace) / file.name).write_bytes(file.getvalue())
            elif uploaded:
                st.warning(f"still need: {', '.join(sorted(set(REQUIRED_FILES) - names))}")

        cached = _cache_is_recorded()
        use_ai = st.toggle(
            "Run the AI layer",
            value=cached,
            disabled=not cached,
            help=(
                "Replays the committed response cache -- no network, no key."
                if cached
                else "No recorded cache in this checkout. Run `make cache` with a key."
            ),
        )
        st.divider()
        st.caption(
            "Every number here comes from the same code path as `make metrics`. "
            "Nothing on this page is computed in the UI."
        )

    if mode == "Upload three CSVs" and workspace is None:
        st.info("Upload the three files, or switch to a bundled seed.")
        return

    try:
        result, report = _run(seed, use_ai, workspace)
    except CacheMiss as exc:
        st.error(f"The classifier has no recorded response for this input.\n\n{exc}")
        return

    _headline(report, result)

    if report is not None and report.false_matches:
        st.error(
            f"{len(report.false_matches)} false match(es) -- "
            + "; ".join(f.why for f in report.false_matches)
        )

    tabs = st.tabs(["Exceptions", "Matches", "The adversarial case", "Audit trail", "Per case"])

    with tabs[0]:
        st.subheader("What the system would not close")
        st.caption(
            "An exception is not a failure. It is the system declining to book "
            "something it cannot justify, and saying why."
        )
        st.dataframe(_exceptions_frame(result, report), width="stretch", hide_index=True)

    with tabs[1]:
        st.subheader("What it closed, and on what evidence")
        st.dataframe(_matches_frame(result), width="stretch", hide_index=True)

    with tabs[2]:
        st.subheader("The case designed to fool it")
        st.markdown(
            "Two disjoint sets of settlement rows explain this credit **exactly**. "
            "Only one is true. Nothing in the data decides which -- the decoy is "
            "not on hold, not late, not flagged, and settles in the same window. "
            "Confidence here is the failure; escalation is the only correct output."
        )
        adversarial = [
            u for u in (report.unresolved if report else ()) if u.planted_expectation == "escalate"
        ]
        if not adversarial:
            st.info("No adversarial case in this dataset.")
        for item in adversarial:
            with st.container(border=True):
                st.markdown(
                    f"**{item.bank_txn_id}** &nbsp; {format_inr(item.credit_paise)} &nbsp; "
                    f"`{item.rule}`"
                )
                st.write(item.reason)
                if use_ai:
                    diagnosis = next(
                        (d for d in result.diagnoses if d.bank_txn_id == item.bank_txn_id), None
                    )
                    if diagnosis:
                        st.caption(
                            f"classifier said case {diagnosis.case_id} at "
                            f"{diagnosis.confidence:.2f} confidence, using "
                            f"{', '.join(diagnosis.used_tools)}"
                        )
                        # When the gate escalates on the model's own verdict it
                        # adopts the model's words as the exception reason, so
                        # these are the same text. Show it once.
                        if diagnosis.reasoning.strip() != item.reason.strip():
                            st.write(diagnosis.reasoning)

    with tabs[3]:
        st.subheader("Zero unlogged decisions")
        lines = [e.bank_txn_id for e in result.outcome.exceptions] + [
            m.bank_txn_id for m in result.outcome.matches
        ]
        default = next(
            (e.bank_txn_id for e in result.outcome.exceptions if e.rule != RULE_NO_IDENTIFIER),
            lines[0] if lines else None,
        )
        if default:
            chosen = st.selectbox("Bank line", lines, index=lines.index(default))
            st.dataframe(_audit_frame(result, chosen), width="stretch", hide_index=True)
            if use_ai:
                diagnosis = next((d for d in result.diagnoses if d.bank_txn_id == chosen), None)
                if diagnosis and diagnosis.tool_calls:
                    with st.expander("Tool calls the classifier made"):
                        for call in diagnosis.tool_calls:
                            st.code(
                                f"{call.name}({json.dumps(call.arguments)})",
                                language="python",
                            )

    with tabs[4]:
        if report is None:
            st.info("Per-case scoring needs a labelled dataset.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "case": c.case_id,
                            "name": c.name,
                            "resolved by": c.resolvable_by,
                            "orders matched": f"{c.orders_matched}/{c.orders}",
                            "units correct": f"{c.units_resolved_correctly}/{c.units}",
                        }
                        for c in report.per_case
                    ]
                ),
                width="stretch",
                hide_index=True,
            )


main()

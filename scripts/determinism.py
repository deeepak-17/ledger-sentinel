"""Prove the pipeline gives the same answer three times in a row.

An LLM in the loop is the obvious place for a demo to become irreproducible, and
"it gave a different number the second time" is fatal to a reconciliation claim.
Temperature is zero and the responses are content-addressed and replayed, so the
whole run should be a pure function of the input files -- this asserts it rather
than assuming it.

What is compared is the *decisions*: every match with its rule and claimed rows,
every exception with its rule, and the diagnosis and confidence behind each one.
Timestamps and run ids are excluded on purpose; they are allowed to differ and
comparing them would only detect the clock.

    python scripts/determinism.py --seed B --runs 3 --ai
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import run_reconciliation  # noqa: E402


def fingerprint(seed: str, *, use_ai: bool, db_path: Path) -> str:
    result = run_reconciliation(seed, db_path=db_path, use_ai=use_ai)
    outcome = result.outcome
    payload = {
        "matches": [
            {
                "bank_txn_id": m.bank_txn_id,
                "layer": m.layer,
                "rule": m.rule,
                "rows": sorted(m.settlement_entity_ids),
                "orders": sorted(m.order_ids),
                "delta_paise": m.delta_paise,
            }
            for m in sorted(outcome.matches, key=lambda m: m.bank_txn_id)
        ],
        "exceptions": [
            {"bank_txn_id": e.bank_txn_id, "layer": e.layer, "rule": e.rule}
            for e in sorted(outcome.exceptions, key=lambda e: e.bank_txn_id)
        ],
        "diagnoses": [
            {
                "bank_txn_id": d.bank_txn_id,
                "case_id": d.case_id,
                "resolution": d.resolution,
                "confidence": d.confidence,
                "tools": list(d.used_tools),
            }
            for d in sorted(result.diagnoses, key=lambda d: d.bank_txn_id)
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default="B")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--ai", action="store_true", help="include the classifier and gate")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as workspace:
        prints = [
            fingerprint(args.seed, use_ai=args.ai, db_path=Path(workspace) / f"run{index}.db")
            for index in range(args.runs)
        ]

    layer = "deterministic + AI" if args.ai else "deterministic"
    unique = set(prints)
    if len(unique) == 1:
        payload = json.loads(prints[0])
        print(
            f"determinism OK: {args.runs}/{args.runs} identical runs on seed_{args.seed} "
            f"({layer}) -- {len(payload['matches'])} matches, "
            f"{len(payload['exceptions'])} exceptions"
        )
        return 0

    print(f"DETERMINISM FAILED on seed_{args.seed} ({layer}): {len(unique)} distinct outcomes")
    reference = json.loads(prints[0])
    for index, other in enumerate(prints[1:], start=1):
        candidate = json.loads(other)
        if candidate == reference:
            continue
        for key in ("matches", "exceptions", "diagnoses"):
            for left, right in zip(reference[key], candidate[key], strict=False):
                if left != right:
                    print(f"  run 0 {key}: {left}")
                    print(f"  run {index} {key}: {right}")
                    break
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

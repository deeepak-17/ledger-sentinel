"""Record the LLM response cache against the live API.

Run once, with a key, before the demo:

    python scripts/record_cache.py --seed A --seed B

Every request is content-addressed, so this is idempotent: re-running it costs
nothing for prompts already recorded and only pays for what has changed. The
resulting `cache/llm_responses.jsonl` is committed, which is what lets `make ai`,
`make demo` and CI run the full pipeline with no key and no network.

Both seeds are recorded. seed_A is what the threshold is tuned against; seed_B is
what gets reported. Recording both means the demo can be pointed at either one
without anybody discovering at the wrong moment that the cache only covers the
dataset we were not going to show.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import COST_INR_PER_MTOK_INPUT, COST_INR_PER_MTOK_OUTPUT, MODEL  # noqa: E402
from src.ingest import load_dataset  # noqa: E402
from src.llm import CACHE_PATH, LiveBackend, ResponseCache, load_env  # noqa: E402
from src.matcher import reconcile  # noqa: E402
from src.pipeline import run_reconciliation, seed_dir  # noqa: E402

# Measured, not guessed. The conversations were simulated offline against both
# seeds and the payloads counted: a three-turn investigation sends roughly
# 7.3k + 11.0k + 12.1k characters as tool results accumulate, and answers with
# a short tool call each time. Converted at ~3.6 chars per token.
#
# This is an ESTIMATE shown before spending, not a measurement of the run. The
# run reports its actual token counts when it finishes, and if the two disagree
# badly, these constants are what to correct.
EST_INPUT_TOKENS_PER_EXCEPTION = 8_500
EST_OUTPUT_TOKENS_PER_EXCEPTION = 750


def estimate(seeds: list[str]) -> tuple[int, int, int, float]:
    """How many exceptions, calls and tokens a recording would cost.

    Runs the deterministic layer only, which is free and offline, and counts
    what it hands to the classifier.
    """
    exceptions = 0
    for seed in seeds:
        dataset = load_dataset(seed_dir(seed), seed=seed, with_truth=False)
        exceptions += len(reconcile(dataset).exceptions)
    tokens_in = exceptions * EST_INPUT_TOKENS_PER_EXCEPTION
    tokens_out = exceptions * EST_OUTPUT_TOKENS_PER_EXCEPTION
    rupees = (
        tokens_in * COST_INR_PER_MTOK_INPUT + tokens_out * COST_INR_PER_MTOK_OUTPUT
    ) / 1_000_000
    return exceptions, tokens_in, tokens_out, rupees


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="append", dest="seeds", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what is already cached and make no API calls",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the spend confirmation (required when stdin is not a terminal)",
    )
    args = parser.parse_args()
    seeds = args.seeds or ["A", "B"]

    load_env()
    cache = ResponseCache()
    before = len(cache)
    print(f"cache at {CACHE_PATH} holds {before} response(s)")

    exceptions, tokens_in, tokens_out, rupees = estimate(seeds)
    print(
        f"\nwould classify {exceptions} exception(s) across seed(s) "
        f"{', '.join(seeds)} using {MODEL!r}\n"
        f"  estimated ~{exceptions * 3} API calls, "
        f"~{tokens_in:,} input + ~{tokens_out:,} output tokens\n"
        f"  estimated cost ~Rs {rupees:.2f}  "
        f"[assumed pricing -- see config.py; the run reports actuals]\n"
        "  content-addressed, so anything already cached is free\n"
    )

    if args.dry_run:
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is not set. Put it in .env (gitignored). Recording is "
            "the only step in this project that needs a key; everything else "
            "replays what it produced.",
            file=sys.stderr,
        )
        return 2

    if not args.yes:
        if not sys.stdin.isatty():
            print("stdin is not a terminal; pass --yes to record without asking.", file=sys.stderr)
            return 2
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing recorded")
            return 0

    total_in = total_out = 0
    with tempfile.TemporaryDirectory() as workspace:
        for seed in seeds:
            backend = LiveBackend(cache=cache, model=MODEL)
            result = run_reconciliation(
                seed,
                db_path=Path(workspace) / f"{seed}.db",
                use_ai=True,
                backend=backend,
            )
            booked = sum(1 for d in result.decisions if d.auto_resolved)
            total_in += result.input_tokens
            total_out += result.output_tokens
            print(
                f"  seed_{seed}: {len(result.decisions)} exceptions classified, "
                f"{booked} auto-resolved, {result.input_tokens} in / "
                f"{result.output_tokens} out tokens"
            )

    cost = (total_in * COST_INR_PER_MTOK_INPUT + total_out * COST_INR_PER_MTOK_OUTPUT) / 1_000_000
    print(
        f"cache now holds {len(cache)} response(s) "
        f"(+{len(cache) - before}); approx Rs {cost:.2f} spent"
    )
    print("commit cache/llm_responses.jsonl so the demo runs offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

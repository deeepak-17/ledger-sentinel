"""List the chat models this API key can actually reach.

`config.py` pins one model exactly, because "whatever is latest" is not a
reproducible experiment. Pinning a model that the key cannot see is a worse
failure than not pinning at all -- it fails at the first API call, which is
usually the worst possible moment -- so this prints what is actually available
and says whether the pinned choice is among them.

    python scripts/models.py

Prints model ids only. No key, no account details.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MODEL  # noqa: E402
from src.llm import load_env  # noqa: E402

FAMILIES = ("gpt-5", "gpt-4.1", "gpt-4o", "o4", "o3")

# Preference order for an automatic suggestion, cheapest-capable first. This
# project pins a mini-class model on purpose -- see the note in config.py.
# Anything matching an entry here is a chat model that supports tool calling.
PREFERRED = (
    "gpt-5-mini",
    "gpt-5.1-mini",
    "gpt-4.1-mini",
    "gpt-5-nano",
    "gpt-4o-mini",
    "gpt-5",
    "gpt-4.1",
)

# Never suggest these even if they match a family prefix -- they are not chat
# completion models and would fail at the first tool call.
EXCLUDE_MARKERS = ("audio", "realtime", "transcribe", "tts", "image", "search", "embedding")


def suggest(ids: list[str]) -> str | None:
    """Pick the cheapest capable model this key can see.

    Exact matches first, then dated variants (`gpt-5-mini-2026-01-01`), so a
    pin lands on the stable alias when one exists.
    """
    usable = [i for i in ids if not any(marker in i for marker in EXCLUDE_MARKERS)]
    for name in PREFERRED:
        if name in usable:
            return name
    for name in PREFERRED:
        dated = sorted(i for i in usable if i.startswith(f"{name}-"))
        if dated:
            return dated[-1]
    return None


def main() -> int:
    load_env()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set. Put it in .env (gitignored).", file=sys.stderr)
        return 2

    request = urllib.request.Request(
        "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 -- the message is the whole point here
        print(f"could not reach the API: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    ids = sorted(model["id"] for model in payload.get("data", []))
    print(f"{len(ids)} models visible to this key\n")
    for family in FAMILIES:
        matches = [i for i in ids if i.startswith(family)]
        if matches:
            print(f"  {family}*")
            for model_id in matches:
                print(f"      {model_id}")
    print()
    if MODEL in ids:
        print(f"config.py pins {MODEL!r} -- available. Nothing to change.")
        return 0

    print(f"config.py pins {MODEL!r}, which this key CANNOT see.")
    choice = suggest(ids)
    if choice is None:
        print("\nNo obviously suitable chat model in the list. Pick one yourself:\n")
        print("    echo 'LEDGER_SENTINEL_MODEL=<id>' >> .env\n")
        return 3
    print(f"\nCheapest capable model this key can reach: {choice}")
    print("Pin it without editing any code by running exactly this:\n")
    print(f"    echo 'LEDGER_SENTINEL_MODEL={choice}' >> .env\n")
    print("Then `make models` again to confirm, and `make cache` to record.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

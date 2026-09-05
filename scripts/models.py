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
    print(
        f"config.py pins {MODEL!r}, which this key CANNOT see.\n"
        "Pick one from the list above and set it either in config.py or, without\n"
        "editing anything, as an environment variable:\n\n"
        "    echo 'LEDGER_SENTINEL_MODEL=<id>' >> .env\n"
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

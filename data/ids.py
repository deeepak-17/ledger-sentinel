"""Deterministic identifier minting.

Every id is drawn from the run's seeded RNG, so regenerating a seed produces
byte-identical files. Prefixes follow Razorpay's public entity naming so the
CSVs read like the real reports.
"""

from __future__ import annotations

import random
import string

ALPHABET = string.ascii_letters + string.digits
DIGITS = string.digits


class IdMinter:
    """Mints unique, seeded identifiers. Collisions are retried, not tolerated --
    a duplicate id would silently corrupt ground truth."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._issued: set[str] = set()

    def _mint(self, prefix: str, length: int, alphabet: str = ALPHABET) -> str:
        for _ in range(100):
            body = "".join(self._rng.choice(alphabet) for _ in range(length))
            candidate = f"{prefix}{body}"
            if candidate not in self._issued:
                self._issued.add(candidate)
                return candidate
        raise RuntimeError(f"could not mint a unique id for prefix {prefix!r}")

    def order(self) -> str:
        return self._mint("order_", 14)

    def payment(self) -> str:
        return self._mint("pay_", 14)

    def refund(self) -> str:
        return self._mint("rfnd_", 14)

    def settlement(self) -> str:
        return self._mint("setl_", 14)

    def utr(self) -> str:
        return self._mint("UTR", 12, DIGITS)

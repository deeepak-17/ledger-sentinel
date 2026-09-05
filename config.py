"""Single source of truth for money rules, timing rules and decision thresholds.

Every assumption here is labelled [assumed] and sourced in README.md. Nothing in
`src/` may hardcode a rate, a window or a threshold -- it imports from here so a
judge can change one number and re-run `make metrics`.

Money is ALWAYS integer paise. Never float. 1 rupee == 100 paise.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

PAISE_PER_RUPEE: Final[int] = 100
CURRENCY: Final[str] = "INR"

# [assumed] Razorpay standard pricing: 2% per domestic transaction on cards,
# netbanking and wallets. UPI carries zero MDR per the NPCI mandate for
# person-to-merchant transactions. Basis points so the arithmetic stays integral.
FEE_BPS_BY_METHOD: Final[dict[str, int]] = {
    "card": 200,
    "netbanking": 200,
    "wallet": 200,
    "upi": 0,
}

# [assumed] GST at 18% is levied on the platform fee, not on the transaction.
GST_BPS: Final[int] = 1800

BPS_DENOMINATOR: Final[int] = 10_000

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

# [assumed] Domestic settlements land T+2 business days after capture.
SETTLEMENT_LAG_BUSINESS_DAYS: Final[int] = 2

# The deterministic matcher tolerates this much drift between the settlement's
# own settled_at and the bank value date before it stops calling it a date match.
DATE_WINDOW_DAYS: Final[int] = 3

# Bank holidays observed by the settlement calendar for the demo cycle.
# [assumed] a representative subset; real deployments read the RBI calendar.
BANK_HOLIDAYS: Final[frozenset[str]] = frozenset(
    {
        "2026-08-15",  # Independence Day
        "2026-10-02",  # Gandhi Jayanti
    }
)

# --------------------------------------------------------------------------
# Matching tolerances
# --------------------------------------------------------------------------

# Two systems rounding the same fee independently can disagree by a paise or
# two. Anything wider than this is a genuine exception, not rounding drift.
ROUNDING_TOLERANCE_PAISE: Final[int] = 2

# Upper bound on how many settlement rows the subset-sum tool will combine into
# one bank credit. Guards against combinatorial blow-up and against the tool
# "finding" a spurious 9-way coincidence.
MAX_SUBSET_SIZE: Final[int] = 6

# --------------------------------------------------------------------------
# Decision gate
# --------------------------------------------------------------------------

# Confidence at or above this auto-resolves; below escalates to a human with a
# reason. Tuned on seed_A only. Reported on seed_B only.
AUTO_RESOLVE_THRESHOLD: Final[float] = 0.90

# Cases that must NEVER auto-resolve regardless of stated confidence. These are
# the adversarial constructions: the classifier being confident about them is
# itself the failure we are guarding against.
ALWAYS_ESCALATE_CASES: Final[frozenset[int]] = frozenset({10, 11})

# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------

MODEL: Final[str] = "claude-sonnet-5"
TEMPERATURE: Final[float] = 0.0
MAX_TOKENS: Final[int] = 2048

# Rupees per million tokens, used only to print a cost line in `make metrics`.
# [assumed] list pricing at time of build.
COST_INR_PER_MTOK_INPUT: Final[float] = 250.0
COST_INR_PER_MTOK_OUTPUT: Final[float] = 1250.0

# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------

RECORDS_PER_SEED: Final[int] = 80

# Cases the generator emits today. The taxonomy in src/cases.py defines all 11;
# this is the switch that turns the remaining four on once Phase 3 is signed off.
ACTIVE_CASES: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 8, 10)

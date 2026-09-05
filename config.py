"""Single source of truth for money rules, timing rules and decision thresholds.

Every assumption here is labelled [assumed] and sourced in README.md. Nothing in
`src/` may hardcode a rate, a window or a threshold -- it imports from here so a
judge can change one number and re-run `make metrics`.

Money is ALWAYS integer paise. Never float. 1 rupee == 100 paise.
"""

from __future__ import annotations

import os
from typing import Final

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

PAISE_PER_RUPEE: Final[int] = 100
CURRENCY: Final[str] = "INR"

# Razorpay's published pricing is 2% + GST per domestic transaction across all
# modes -- cards, netbanking, wallets AND UPI -- because the 2% is Razorpay's own
# platform fee, not interchange:
#   https://razorpay.com/pricing/
# The NPCI zero-MDR mandate removes the *interchange* on person-to-merchant UPI
# and RuPay debit, not an aggregator's platform fee.
#
# [deviation, deliberate] We price UPI at zero anyway. The dataset needs one path
# where the bank credit equals the order amount exactly -- that is case 1, the
# control group, and without it every single case in the taxonomy involves fee
# arithmetic and we lose the ability to show a clean match at all. This is
# documented in the open in README.md rather than left for a reader to catch.
# Basis points so the arithmetic stays integral.
FEE_BPS_BY_METHOD: Final[dict[str, int]] = {
    "card": 200,
    "netbanking": 200,
    "wallet": 200,
    "upi": 0,
}

# [verified] GST at 18% is levied on the gateway's service fee, not on the
# transaction value. https://razorpay.com/blog/international-payment-gateway-cost-india
GST_BPS: Final[int] = 1800

BPS_DENOMINATOR: Final[int] = 10_000

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

# [verified] "T+2 working days, T being the date of transaction capture."
# https://razorpay.com/docs/payments/settlements/faqs/
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

# The plan named Claude here. We ship against OpenAI because that is the API key
# available to this build. It is a dependency swap, not a design change -- the
# classifier sits behind a backend interface and neither the tools, the gate nor
# the metrics know which vendor answered. Pinned exactly, because "the latest
# model" is not a reproducible experiment.
#
# A mini-class model is pinned deliberately, and not only for cost. The gate
# re-verifies the arithmetic and vetoes ambiguous evidence BEFORE confidence is
# consulted, so the false-match rate does not depend on how clever the model is.
# A weaker model degrades diagnosis accuracy, which is measured and reported --
# it cannot degrade safety, which is the number this project leads with. If the
# accuracy turns out poor, that is a finding worth publishing rather than a
# reason to spend more.
#
# Override without editing code:  echo 'LEDGER_SENTINEL_MODEL=<id>' >> .env
# `make models` lists what a key can actually reach and checks this pin.
MODEL: Final[str] = os.environ.get("LEDGER_SENTINEL_MODEL", "gpt-5-mini")

# None means "send no temperature and take the model's default". The gpt-5 family
# rejects any explicit temperature but the default (1), so pinning 0.0 here made
# every live call fail with a 400 -- see FAILURES.md #6.
#
# Determinism was never coming from this value. The demo, the API, CI and
# scripts/determinism.py all read the content-addressed cache, which replays
# byte-identically whatever the model was sampled at. What an explicit 0.0 would
# buy is reproducibility of a RE-RECORDING, and that is what is given up.
#
# This constant is part of the cache key (src/llm.py request_key). Changing it
# invalidates every recorded response and makes the offline path raise CacheMiss,
# so it must be settled before `make cache` and left alone afterwards.
TEMPERATURE: Final[float | None] = None

# Sent as `max_completion_tokens`, which on a reasoning model covers reasoning
# tokens as well as visible output. Measured on gpt-5-mini: reasoning came back
# at 0 tokens and a first turn cost 34 output tokens, so this is generous.
MAX_TOKENS: Final[int] = 2048

# How many tool-use turns one exception is allowed before the classifier gives up
# and escalates. A model still calling tools after this many rounds is not
# converging, and an escalation is the honest outcome.
MAX_TOOL_TURNS: Final[int] = 8

# Rupees per million tokens, used only to print a cost line in `make metrics`
# and a spend estimate before `make cache` runs.
# [assumed] mini-class list pricing; NOT verified against a live price list --
# the build environment had no network. Correct these against OpenAI's pricing
# page once a real run reports actual token counts.
COST_INR_PER_MTOK_INPUT: Final[float] = 22.0
COST_INR_PER_MTOK_OUTPUT: Final[float] = 176.0

# --------------------------------------------------------------------------
# Deterministic guardrail on the AI layer
# --------------------------------------------------------------------------

# If the tools prove a credit has two or more STRUCTURALLY VALID readings that
# share no rows -- each of them a complete settlement batch -- then no stated
# confidence may auto-resolve it. This is the backstop that does not depend on
# the model correctly recognising the adversarial case: it is a fact derived
# from the tool trace, not from the model's opinion of itself.
VETO_ON_AMBIGUOUS_EVIDENCE: Final[bool] = True

# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------

RECORDS_PER_SEED: Final[int] = 80

# Cases the generator emits today. The taxonomy in src/cases.py defines all 11;
# this is the switch that turns the remaining four on once Phase 3 is signed off.
ACTIVE_CASES: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 8, 10)

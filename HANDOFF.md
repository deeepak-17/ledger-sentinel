# Handoff — Ledger Sentinel

Last updated: **2026-09-05**. Written for whoever picks this up next, including
future-me on another machine or another tool.

---

## 0. Read this first

**The application deadline in the plan is today, 5 September 2026.** The plan's
own Day-0 action — *"confirm on the form whether repo/video are due at
submission"* — was never carried out. That answer decides everything below:

- **If a repo is due today (Track A):** M2/M3/M4 will not happen in the hours
  remaining. What exists is a rigorous, tested, honest dataset layer and no
  pipeline. Submitting it as-is means submitting a data generator, not a
  reconciliation system. Decide whether that clears the track bar or whether to
  skip this cycle.
- **If today is registration only (Track B):** the schedule is fine. M1 is done
  two days into a multi-week runway, and the remaining milestones stay as
  written in `02-ledger-sentinel-plan.md` §5.

Everything else in this document assumes the work continues.

---

## 1. Status at a glance

| Milestone | State | Notes |
|---|---|---|
| **M0 — Foundations** | ✅ done | schemas, config, money/date rules, CI, 86 tests |
| **M1 — Labelled dataset** | ✅ done | 2 seeds, published truth, byte-identical regeneration |
| **M2 — Deterministic matcher + audit** | ❌ not started | this is the next thing to build |
| **M3 — Classifier + gate** | ❌ not started | |
| **M4 — Submission package** | ❌ not started | |
| **M5 — Stretch (cases 6/7/9/11)** | ❌ not started | switch exists, builders do not |

**Not yet a git repository.** No commits exist. See §7 — this is the single
highest-priority action, and it is more urgent than any code.

---

## 2. What exists on disk

```
ledger-sentinel/
├── pyproject.toml           Python 3.11, pinned deps, ruff + pytest config
├── Makefile                 setup / data / test / lint / metrics / demo / determinism
├── config.py                EVERY constant: fee bps, GST, T+N, tolerances, threshold
├── HANDOFF.md               this file
├── src/
│   ├── cases.py             the 11-case taxonomy — the contract everything shares
│   ├── schema.py            pydantic models for orders / settlements / bank / truth
│   ├── money.py             fee + GST in integer paise, half-up rounding
│   └── dates.py             T+2 business days, weekends, bank holidays
├── data/
│   ├── generator.py         orchestration, CSV writing, self-generating README
│   ├── scenarios.py         one builder per planted case
│   ├── ids.py               seeded Razorpay-style id minting
│   ├── README.md            GENERATED — do not hand-edit, run `make data`
│   ├── seed_A/              tuning set: orders, settlements, bank, truth.json
│   └── seed_B/              held-out set — do not look at it while tuning
├── tests/
│   ├── test_unit_money.py   fee/GST arithmetic, rounding, formatting
│   ├── test_unit_dates.py   settlement calendar
│   └── test_unit_dataset.py ground-truth totality + adversarial-case integrity
└── .github/workflows/ci.yml lint, test, and prove the dataset regenerates
```

Missing but referenced by the `Makefile` and CI (they will fail until written):
`src/matcher.py`, `src/db.py`, `src/ingest.py`, `src/audit.py`, `src/metrics.py`,
`src/tools.py`, `src/classifier.py`, `src/gate.py`, `src/api.py`, `src/app.py`,
`scripts/determinism.py`, `docs/`, `cache/`, `README.md`, `FAILURES.md`.

---

## 3. Verified numbers

Both seeds, reproduced on every run:

- **80 orders**, 83 settlement rows, 65 bank lines, 65 truth records
- every order, settlement row and bank line claimed by **exactly one** truth record
- **56 of 65** bank lines carry a UTR; **9 do not** (4 batched + 3 netted refund + 2 collision)
- regenerating a seed is **byte-identical** (asserted in CI via `git diff --exit-code`)
- seed_A and seed_B share **no identifiers**
- **86 tests pass**, `ruff check` and `ruff format --check` clean

Case distribution (identical in both seeds):

| Case | Name | Layer | Units | Orders |
|---:|---|---|---:|---:|
| 1 | Clean match | deterministic | 18 | 18 |
| 2 | Fee and GST deducted | deterministic | 22 | 22 |
| 3 | Settlement timing gap | deterministic | 10 | 10 |
| 4 | Batched payout | tool | 4 | 12 |
| 5 | Refund netted into batch | ai | 3 | 6 |
| 8 | Paise rounding drift | deterministic | 6 | 6 |
| 10 | Amount collision (adversarial) | **escalate** | 2 | 6 |

Cases 6, 7, 9, 11 are defined in `src/cases.py` but dormant. Turning one on is a
builder plus an entry in `ACTIVE_CASES` — not a redesign.

---

## 4. Design decisions that will not survive in the code alone

These are the judgement calls. Anyone continuing needs them, and they are the
material for `docs/architecture.md` when M4 comes around.

### 4.1 The deterministic layer requires an identifier

**This is the thesis of the whole system.** Amount agreement alone never closes a
match, because amounts collide. Only a UTR closes a match; amount and date are
then used as *confirmation*, and a mismatch produces an exception rather than a
match.

Why this exists: every settlement row carries a UTR and so does every bank line,
which would have made the entire problem a SQL join with nothing left for the AI
layer to earn. Rather than crippling the data, the generator models a real
condition — single NEFT credits carry the UTR in the narration; consolidated
payouts arrive over a different rail with a generic narration and no reference at
all. This is documented in `data/README.md` and is the honest difficulty knob:
**no evidence is deleted, a statement format that never carried it is modelled.**

Consequence: cases 1/2/3/8 (56 orders, **70%**) are deterministic; cases 4/5/10
(24 orders) go to the classifier. That is the 65–70% baseline the plan predicted,
arrived at by construction rather than by tuning toward it.

### 4.2 UPI carries zero MDR

`FEE_BPS_BY_METHOD` prices cards/netbanking/wallets at 200bps and **UPI at zero**,
per the NPCI person-to-merchant mandate — rather than the flat 2% the plan
assumed. It is more correct, it costs one dict lookup, and it makes case 1
(clean match) economically real instead of artificial. Still marked `[assumed]`
and still needs a citation in the README against Razorpay's public pricing page.

### 4.3 The adversarial case had to be rebuilt once

The first version of case 10 marked the decoy settlement `on_hold`, which meant a
careful matcher could resolve it — making the "must escalate" claim theatre. It
is now genuinely undecidable: the decoy is **not** held, not late, not flagged,
sits in the same settlement window with the same `settled_at`, and its payout
simply falls outside this statement export (an ordinary partial download).

Four tests in `TestCollisionIsGenuinelyAmbiguous` enforce this: a single row
explains the credit exactly, a *disjoint* pair also explains it exactly, no field
distinguishes them, and the credit has no UTR to join on. **If a future change
makes case 10 solvable, those tests fail — that is intentional. Do not relax
them to make the matcher look better.**

### 4.4 Money is integer paise, and columns say so

Razorpay's settlement report denominates `amount`/`fee`/`tax` in rupees. We
deviate and suffix every money column `_paise`, holding integers. Rationale is in
`data/README.md`. Fee and GST round half-up independently, which is what
generates the one-paise disagreements case 8 models.

### 4.5 Generated docs, not typed ones

`data/README.md` is emitted by `python -m data.generator --readme`. CI fails if
the committed copy drifts. Same discipline must apply to `docs/metrics.md` when
M2 lands — the plan requires that no number in any document is hand-typed.

---

## 5. What to build next — M2

**Objective:** a complete, submittable Loop-1 baseline. If everything after M2
fails, this alone is a defensible submission.

Build in this order:

1. **`src/ingest.py`** — CSV → validated pydantic models. Must reject malformed
   rows loudly; a bad row silently becoming an exception would flatter the
   numbers.
2. **`src/db.py`** — SQLite tables: `orders`, `settlements`, `bank`, `matches`,
   `exceptions`, `audit`, `runs`. One file, ships in the repo, `sqlite3`-able live
   in front of a judge.
3. **`src/matcher.py`** — deterministic rules only, in this order:
   - `utr_exact` — UTR resolves to exactly one settlement row, net equals credit
     exactly, value date inside the settlement window → **match**
   - `utr_within_rounding` — as above but `|diff| <= ROUNDING_TOLERANCE_PAISE`
     → **match**, noting the drift (this is case 8)
   - `utr_amount_mismatch` — UTR matches, amount is off beyond tolerance
     → **exception**, not a match
   - `unknown_utr` — UTR present, resolves to nothing → **exception**
   - `no_identifier` — no UTR → **exception**, handed to M3 (cases 4/5/10)
   **The matcher must never run subset-sum.** Enumeration is deterministic and
   belongs in `src/tools.py` for the classifier to call; deciding what to do with
   multiple solutions is the judgement M3 is for.
4. **`src/audit.py`** — every match and every exception writes a row: rule name,
   inputs, decision, reason, timestamp. Zero unlogged decisions is an SLO.
5. **`src/metrics.py`** — `make metrics` prints false-match rate **first**, then
   match rate, then the unresolved list with a reason each.

**Acceptance:** false-match rate 0% on seed_B · match rate 68–72% (expect exactly
70.0%: 56/80) · every match has an audit row naming its rule · `tests/test_golden.py`
asserts the metrics floor · `tests/test_adversarial.py` asserts case 10 is **not**
matched by the deterministic layer.

Then M3 (classifier + 3 tools + gate + offline cache), M4 (docs, video, Streamlit).
Both are unchanged from `02-ledger-sentinel-plan.md` §6.

---

## 6. Decisions already taken (do not relitigate)

- **7 cases now, 11 later.** Deepak chose to ship cases 1/2/3/4/5/8/10 and add
  6/7/9/11 only if Phase 3 finishes with buffer. The enum and the `ACTIVE_CASES`
  switch already accommodate all eleven.
- **Offline-first LLM.** No `ANTHROPIC_API_KEY` in the build environment. The
  classifier goes behind an interface with a cached-replay backend committed to
  the repo; live Claude (temp 0, pinned model) is used when a key is present and
  populates the cache. The demo must never depend on the network.
- **Forecast layer is cut** under Track A, and the README must say so explicitly
  rather than quietly omitting it.

---

## 7. Immediate actions, highest priority first

1. **`git init` and commit.** There is no version control and no `.gitignore`.
   The working directory **already vanished once** during this build, when the
   parent folder was renamed `Razor-Pay` → `Razorpay` on 3 September; every file
   had to be reconstructed. There is no second recovery path. A `.gitignore`
   needs at minimum `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`,
   `.DS_Store`, `ledger.db` — but **not** `data/seed_*/`, which must be committed
   because CI diffs them.
2. **Answer the deadline question in §0** and record the answer in the README.
3. **Cite the fee/GST/T+2 assumptions** against Razorpay's public pricing and
   settlement docs, and paste the link into `config.py` and the README.
4. Then start M2.

---

## 8. Picking this up cold

```bash
cd /Users/deepak/Downloads/Razorpay/ledger-sentinel
make setup     # uv venv on Python 3.11 + install
make test      # expect 86 passing
make data      # regenerates both seeds + data/README.md; must produce no diff
make lint
```

`make metrics`, `make demo` and `make determinism` are wired but will fail until
M2 and M3 land — the targets exist so the interface is fixed before the code is.

Read in this order to understand the system: `data/README.md` (what the problem
looks like), `src/cases.py` (the taxonomy), §4 above (why it is built this way),
then `data/scenarios.py` (how each case is planted).

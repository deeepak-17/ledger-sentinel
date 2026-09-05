# Handoff — Ledger Sentinel

Last updated: **2026-09-05**, after M2. Written for whoever picks this up next,
including future-me on another machine or another tool.

---

## 0. Read this first

**The application deadline in the plan is today, 5 September 2026,** and the
plan's own Day-0 action — *"confirm on the form whether repo/video are due at
submission"* — has still not been carried out. Do that before anything else.

What changed since the last handoff is that the answer is no longer existential.
M2 landed, so there is a working Loop-1 baseline in the repo:

- **If a repo is due today (Track A):** what exists is submittable. Three CSVs go
  in, a reconciled ledger and a diagnosed exception list come out, every decision
  is logged, and the headline number is a **0.0% false-match rate at a 70.0%
  match rate on held-out data**. The AI layer, the video and the README are not
  built — the honest framing is "the deterministic half of the system, measured".
  That is a real submission rather than a data generator.
- **If today is registration only (Track B):** the schedule is comfortable. M0,
  M1 and M2 are done and the remaining milestones stand as written in
  `02-ledger-sentinel-plan.md` §6.

Either way, **write the answer into the README** rather than leaving it in
someone's head.

---

## 1. Status at a glance

| Milestone | State | Notes |
|---|---|---|
| **M0 — Foundations** | ✅ done | schemas, config, money/date rules, CI |
| **M1 — Labelled dataset** | ✅ done | 2 seeds, published truth, byte-identical regeneration |
| **M2 — Deterministic matcher + audit** | ✅ done | 0.0% false-match, 70.0% match rate, zero unlogged decisions |
| **M3 — Classifier + gate** | ❌ not started | this is the next thing to build |
| **M4 — Submission package** | ❌ not started | README does not exist yet |
| **M5 — Stretch (cases 6/7/9/11)** | ❌ not started | switch exists, builders do not |

**Now under version control.** Two commits: `M0+M1` and `M2`. The working
directory vanished once during this build (parent folder renamed on 3 September)
and every file had to be reconstructed; that risk is closed. Push to a remote
when there is one.

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
│   ├── dates.py             T+2 business days, weekends, bank holidays
│   ├── ingest.py            CSV → validated models; header drift and bad rows raise
│   ├── db.py                SQLite: orders settlements bank matches exceptions audit runs
│   ├── matcher.py           the deterministic layer — pure function, no I/O
│   ├── audit.py             one row per decision; zero unlogged decisions is asserted
│   ├── pipeline.py          the one orchestration shared by CLI, API and tests
│   └── metrics.py           scoring; the ONLY module allowed to read truth.json
├── data/
│   ├── generator.py         orchestration, CSV writing, self-generating README
│   ├── scenarios.py         one builder per planted case
│   ├── ids.py               seeded Razorpay-style id minting
│   ├── README.md            GENERATED — do not hand-edit, run `make data`
│   ├── seed_A/              tuning set: orders, settlements, bank, truth.json
│   └── seed_B/              held-out set — do not look at it while tuning
├── docs/
│   └── metrics.md           GENERATED — do not hand-edit, run `make metrics`
├── tests/
│   ├── conftest.py          session-scoped pipeline runs per seed
│   ├── test_unit_money.py   fee/GST arithmetic, rounding, formatting
│   ├── test_unit_dates.py   settlement calendar
│   ├── test_unit_dataset.py ground-truth totality + adversarial-case integrity
│   ├── test_unit_ingest.py  malformed input must raise, naming file and line
│   ├── test_golden.py       the metrics floor and the audit-trail SLO
│   └── test_adversarial.py  case 10 is not matched by the deterministic layer
└── .github/workflows/ci.yml lint, test, and prove data/ and docs/ regenerate
```

Missing but referenced by the `Makefile` and CI (they will fail until written):
`src/tools.py`, `src/classifier.py`, `src/gate.py`, `src/api.py`, `src/app.py`,
`scripts/determinism.py`, `cache/llm_responses.jsonl`, `README.md`,
`FAILURES.md`, `docs/architecture.md`, `docs/exceptions.md`, `docs/runbook.md`.

`ledger.db` is a build artefact of `make metrics` and is gitignored.

---

## 3. Verified numbers

### Dataset (both seeds, reproduced on every run)

- **80 orders**, 83 settlement rows, 65 bank lines, 65 truth records
- every order, settlement row and bank line claimed by **exactly one** truth record
- **56 of 65** bank lines carry a UTR; **9 do not** (4 batched + 3 netted refund + 2 collision)
- regenerating a seed is **byte-identical** (asserted in CI via `git diff --exit-code`)
- seed_A and seed_B share **no identifiers**

| Case | Name | Layer | Units | Orders |
|---:|---|---|---:|---:|
| 1 | Clean match | deterministic | 18 | 18 |
| 2 | Fee and GST deducted | deterministic | 22 | 22 |
| 3 | Settlement timing gap | deterministic | 10 | 10 |
| 4 | Batched payout | tool | 4 | 12 |
| 5 | Refund netted into batch | ai | 3 | 6 |
| 8 | Paise rounding drift | deterministic | 6 | 6 |
| 10 | Amount collision (adversarial) | **escalate** | 2 | 6 |

### Deterministic layer, held-out seed_B (`make metrics`)

| Metric | Value |
|---|---|
| **False-match rate** | **0.0%** (0 of 56 matches) |
| Match rate | **70.0%** (56 of 80 orders) |
| Must-escalate cases correctly escalated | 2/2 |
| Exceptions raised | 9, each with a written reason |
| Unlogged decisions | 0 |

Identical on seed_A, which is the point: the 70% is a property of the data (56
orders sit behind a bank credit carrying a UTR), not a tuned result.

Match rules that fired: `utr_exact` 50, `utr_within_rounding` 6 (case 8).
Exception rules that fired: `no_identifier` 9.

**144 tests pass**, `ruff check` and `ruff format --check` clean.

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

`data/README.md` is emitted by `python -m data.generator --readme` and
`docs/metrics.md` by `python -m src.metrics --seed B`. CI fails if either
committed copy drifts. The same discipline must apply to every number that
reaches the README and the video script: if a document and the code disagree,
the document is wrong.

### 4.6 The matcher will not run subset-sum — and that is a feature

A bank credit with no reference *could* be explained by some combination of
settlement rows, and enumerating those combinations is deterministic arithmetic.
It belongs in `src/tools.py` for the classifier to call. But **deciding what to
do when two disjoint combinations both explain a credit exactly is a
judgement**, and pushing it into the matcher would mean the matcher silently
picking one. It would raise the match rate and it would be the single most
expensive bug the system could ship — case 10 is the proof that the situation is
not hypothetical. Everything without an identifier leaves the matcher as an
exception carrying its candidate rows, and M3 decides.

### 4.7 A match is scored on what it claims, not on where it lands

`src/metrics.py` counts a match as correct only if it claims *exactly* the right
orders and the right settlement rows. Getting the bank line into the "matched"
bucket while attributing it to the wrong orders is still a false match: it is
the answer that puts a controller on the wrong trail. Scoring it as a win is how
a system flatters itself.

### 4.8 Two passes, so the classifier's search space is honest

`reconcile()` resolves every identified credit first, then offers each
unidentified credit only the settlement rows no identifier has claimed. A single
pass would hand the classifier candidate rows that a UTR was about to take —
known-wrong answers inside its search space.

### 4.9 The pipeline never sees truth.json

`src/pipeline.py` loads exactly the three files a merchant would hand over.
`src/metrics.py` is the only module that reads ground truth, and
`tests/test_golden.py` asserts the database contains no truth table. A scorer
that can see the answers is not a scorer.

### 4.10 Exceptions are keyed on the bank line

Money moving is the event worth explaining, so `matches` and `exceptions` are
both keyed on `bank_txn_id`. Settlement rows that no credit claims are reported
separately as a count (27 on each seed) rather than as exceptions of their own —
in this dataset every one of them is already a candidate for an unresolved
credit, and raising both would be the same problem counted twice. If a future
dataset has genuinely orphaned settlements, that count is where it will show up
and it deserves its own rule then.

---

## 5. What to build next — M3

**Objective:** the AI layer resolves the residual with calibrated confidence, and
refuses the case it cannot know.

The exception list the classifier receives is nine bank credits per seed: four
batched payouts (case 4), three netted refunds (case 5), two amount collisions
(case 10). Each arrives with its candidate settlement rows already scoped to the
date window and stripped of anything an identifier claimed.

Build in this order:

1. **`src/tools.py`** — three tools, all pure arithmetic, all unit-tested
   independently of the model:
   - `query_candidates(order_id | amount | window)` — read-only over SQLite
   - `expected_fee(amount, method)` — a thin wrapper over `src.money`, so the
     model and the deterministic layer can never disagree about arithmetic
   - `subset_sum(candidates, target, max_k)` — bounded by `MAX_SUBSET_SIZE`;
     **must return all solutions, not the first**, because returning one is
     precisely how case 10 gets auto-resolved
2. **`src/classifier.py`** — Claude behind an interface, temp 0, pinned model,
   tool-use loop. Output: `case_id`, resolution, confidence, reasoning, tool-call
   trace. The offline replay backend is not optional — see §6.
3. **`src/gate.py`** — `confidence >= AUTO_RESOLVE_THRESHOLD` auto-resolves,
   below escalates with the reason. `ALWAYS_ESCALATE_CASES` (10, 11) never
   auto-resolve **regardless of stated confidence**; a confident wrong answer is
   the failure being guarded against, not an edge case.
4. **`cache/llm_responses.jsonl`** — committed, keyed on a hash of the prompt.
   Populated when a key is present, replayed when it is not.
5. **`scripts/determinism.py`** — three runs, assert identical.
6. Extend `src/metrics.py`: diagnosis accuracy per case, auto-resolve precision,
   a calibration table, cost and token counts into the `runs` table (the columns
   are already there).

**Acceptance:** diagnosis accuracy ≥ 85% on seed_B · auto-resolve precision ≥ 95%
· case 10 escalated 100% · false-match rate still 0.0% · 3/3 determinism · cost
logged and under ₹10 per 80 records.

**Tune on seed_A only.** Every published number comes from seed_B.

Then M4: README (headline metric in the first ten lines), `docs/architecture.md`
with the four rubric headings, `FAILURES.md` with a real entry, Streamlit demo,
five-minute video ending on what could not be solved.

---

## 6. Decisions already taken (do not relitigate)

- **7 cases now, 11 later.** Deepak chose to ship cases 1/2/3/4/5/8/10 and add
  6/7/9/11 only if Phase 3 finishes with buffer. The enum and the `ACTIVE_CASES`
  switch already accommodate all eleven. `RULE_UTR_ALREADY_CLAIMED` in the
  matcher is the seat reserved for case 11.
- **Offline-first LLM.** No `ANTHROPIC_API_KEY` in the build environment. The
  classifier goes behind an interface with a cached-replay backend committed to
  the repo; live Claude (temp 0, pinned model) is used when a key is present and
  populates the cache. The demo must never depend on the network.
- **Forecast layer is cut** under Track A, and the README must say so explicitly
  rather than quietly omitting it.

---

## 7. Immediate actions, highest priority first

1. **Answer the deadline question in §0** and record the answer in the README.
2. **Write `README.md`.** It is the one artefact a judge is guaranteed to read
   and it does not exist. First ten lines: false-match rate, then match rate,
   then unresolved count, all copied from `docs/metrics.md`.
3. **Cite the fee/GST/T+2 assumptions** against Razorpay's public pricing and
   settlement docs, and paste the link into `config.py` and the README.
4. **Push to a remote.** Two local commits protect against a rename; they do not
   protect against a disk.
5. Then start M3, in the order in §5.

---

## 8. Picking this up cold

```bash
cd /Users/deepak/Downloads/Razorpay/ledger-sentinel
make setup       # uv venv on Python 3.11 + install
make test        # expect 144 passing
make lint
make data        # regenerates both seeds + data/README.md; must produce no diff
make metrics     # runs the pipeline on seed_B, prints the report, writes docs/metrics.md
```

`make demo` and `make determinism` are wired but will fail until M3 lands — the
targets exist so the interface is fixed before the code is.

To interrogate a number rather than trust it:

```bash
sqlite3 ledger.db "select rule, count(*) from matches group by rule"
sqlite3 ledger.db "select rule, reason from exceptions"
sqlite3 ledger.db "select seq, rule, decision, reason from audit where subject_id='BNK00031'"
```

Read in this order to understand the system: `data/README.md` (what the problem
looks like), `src/cases.py` (the taxonomy), §4 above (why it is built this way),
`src/matcher.py` (the rules), then `data/scenarios.py` (how each case is planted).

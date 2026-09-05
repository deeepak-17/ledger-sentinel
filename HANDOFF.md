# Handoff — Ledger Sentinel

Last updated: **2026-09-05, 10:00 IST**, mid-build. Written for whoever picks
this up next — immediately, that is a Claude Code CLI session running on
Deepak's Mac.

---

## 0. Read this first

**Submission deadline is tonight, 5 September 2026, 23:59 IST.** Registration and
the repo are both due then — Track A confirmed, so the repository must be
submittable as it stands. It currently is.

**Repo:** https://github.com/deeepak-17/ledger-sentinel — 18 commits, CI green.

**The one thing blocking real work:** the LLM response cache has never been
recorded, because the sandboxed environment the previous session ran in cannot
reach `api.openai.com` (403 from its egress proxy; the cloud container could not
either). **You are running locally and almost certainly can.** That single step
is §5.1 and everything else in M3 waits on it.

Both keys are already in `.env` (gitignored): `OPENAI_API_KEY` and a
`GITHUB_TOKEN` used for pushes. **Revoke the GitHub PAT once tonight is over** —
github.com/settings/tokens.

---

## 1. Status at a glance

| Milestone | State | Notes |
|---|---|---|
| **M0 — Foundations** | ✅ | schemas, config, money/date rules, CI |
| **M1 — Labelled dataset** | ✅ | 2 seeds, published truth, byte-identical regeneration |
| **M2 — Deterministic matcher + audit** | ✅ | 0.0% false-match, 70.0% match rate, zero unlogged decisions |
| **M3 — Classifier + gate** | ⚠️ code done, **unmeasured** | every part built and tested offline. The cache is empty, so no number in this milestone has been produced by an actual model |
| **M4 — Submission package** | ✅ except the video | README, architecture, FAILURES, runbook, exceptions, demo, video script |
| **M5 — Stretch (cases 6/7/9/11)** | ❌ | switch exists, builders do not |

**Be precise about M3 when you talk about it.** The end-to-end test drives a
scripted stand-in that plays a competent analyst deterministically. It proves the
plumbing — that a correct diagnosis gets booked, that the gate refuses what it
should, that a false match cannot pass the arithmetic re-check. It proves nothing
about the model. Until §5.1 runs, "diagnosis accuracy" and "auto-resolve
precision" have no measured values and must not be quoted.

---

## 2. What exists on disk

```
ledger-sentinel/
├── config.py                EVERY rate, window and threshold; nothing in src/ hardcodes one
├── .env                     gitignored — OPENAI_API_KEY, GITHUB_TOKEN
├── .env.example             committed template
├── Makefile                 setup data test lint fmt metrics ai models cache demo api determinism
├── src/
│   ├── cases.py             the 11-case taxonomy — the contract everything shares
│   ├── schema.py            pydantic models for the three sources and ground truth
│   ├── money.py  dates.py   integer-paise fee/GST, T+2 business-day calendar
│   ├── ingest.py            CSV → validated models; header drift and bad rows raise
│   ├── db.py                SQLite: orders settlements bank matches exceptions audit runs
│   ├── matcher.py           the deterministic layer — pure function, no I/O
│   ├── audit.py             one row per decision; zero unlogged decisions is asserted
│   ├── tools.py             the three tools; subset_sum returns EVERY solution
│   ├── llm.py               one call, two backends, content-addressed cache
│   ├── classifier.py        the tool-use loop; prompt carries no answer key
│   ├── gate.py              what may be booked — confidence is checked LAST
│   ├── pipeline.py          the one orchestration shared by CLI, API and tests
│   ├── metrics.py           scoring; the ONLY module allowed to read truth.json
│   ├── api.py               FastAPI; /docs is interactive evidence
│   └── app.py               Streamlit demo
├── data/                    generator, scenarios, ids, seed_A/, seed_B/, GENERATED README.md
├── scripts/                 determinism.py, record_cache.py, models.py
├── docs/                    architecture.md, runbook.md, video-script.md,
│                            metrics.md + exceptions.md (both GENERATED)
├── tests/                   207 tests — unit, golden, adversarial, tools, ai_layer,
│                            api, app, no_secrets
├── cache/llm_responses.jsonl   **DOES NOT EXIST YET** — this is §5.1
└── .github/workflows/ci.yml
```

Never written: `FAILURES.md` covers this in "Still open".

---

## 3. Verified numbers

### Deterministic layer, held-out seed_B (`make metrics`)

| Metric | Value |
|---|---|
| **False-match rate** | **0.0%** (0 of 56 matches) |
| Match rate | **70.0%** (56 of 80 orders) |
| Must-escalate cases correctly escalated | 2/2 |
| Exceptions raised | 9, each with a written reason |
| Unlogged decisions | 0 |

Identical on seed_A, which is the point: the 70% is a property of the data (56
orders sit behind a credit carrying a UTR), not a tuned result. Rules that fired:
`utr_exact` 50, `utr_within_rounding` 6 (case 8), `no_identifier` 9.

### Dataset (both seeds)

80 orders · 83 settlement rows · 65 bank lines · 65 truth records, every row
claimed exactly once · 9 of 65 bank lines carry no UTR · regeneration is
byte-identical (CI asserts it) · seeds share no identifiers.

| Case | Name | Layer | Units | Orders |
|---:|---|---|---:|---:|
| 1 | Clean match | deterministic | 18 | 18 |
| 2 | Fee and GST deducted | deterministic | 22 | 22 |
| 3 | Settlement timing gap | deterministic | 10 | 10 |
| 4 | Batched payout | tool | 4 | 12 |
| 5 | Refund netted into batch | ai | 3 | 6 |
| 8 | Paise rounding drift | deterministic | 6 | 6 |
| 10 | Amount collision (adversarial) | **escalate** | 2 | 6 |

### AI layer

**No measured numbers exist.** See §1.

**207 tests pass**, ruff clean, CI green on every step including determinism and
an offline pipeline run with `OPENAI_API_KEY` blanked.

---

## 4. Design decisions that will not survive in the code alone

These are the judgement calls. Anyone continuing needs them. They are also the
material behind `docs/architecture.md`, which is written -- if you change a
decision here, change it there too.

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

---

## 5. What is left, in order

### 5.1 Record the response cache — BLOCKING, and the only step that needs a key

```bash
cd ~/Downloads/Razorpay/ledger-sentinel
make setup      # .venv on disk predates the OpenAI dependency; recreate it
make models     # what this key can see, and whether the pin is valid
```

**`config.py` pins `MODEL = "gpt-5.1"`, which is an unverified guess.** The
previous session had no network and could not check it. `make models` prints the
chat models the key can actually reach and says whether the pin is among them. If
it is not, override without touching code:

```bash
echo 'LEDGER_SENTINEL_MODEL=<id>' >> .env
```

Then:

```bash
make cache      # records seed_A and seed_B; content-addressed, so idempotent
make ai         # the whole system, replaying what you just recorded
```

Commit `cache/llm_responses.jsonl`. That file is what lets the demo, CI and a
fresh clone run the full pipeline with no key and no network.

**Expect the first run to surface something.** The classifier has never spoken to
a real model. Likely failure modes: the model answering in prose instead of
calling `submit_diagnosis` (handled — one nudge, then escalation), not calling
`subset_sum` at all (it will then have no evidence and should escalate), or
inflating confidence. All three fail safe, toward escalation. Read
`docs/metrics.md` after `make ai` and put whatever you find into `FAILURES.md`.

### 5.2 Tune the gate threshold — on seed_A only

```bash
python -m src.metrics --seed A --ai --threshold 0.85 --no-markdown
python -m src.metrics --seed A --ai --threshold 0.95 --no-markdown
```

Pick a value, set `AUTO_RESOLVE_THRESHOLD` in `config.py`, then report **once**
on seed_B and do not iterate on what you see. Looking at seed_B while tuning
destroys the only thing that makes its numbers worth quoting.

### 5.3 Regenerate every published number

```bash
make metrics && python -m src.metrics --seed B --ai
```

`docs/metrics.md` and `docs/exceptions.md` are generated; CI fails if a committed
copy has drifted. Then update the README's headline table and status rows by hand
— that is the one table that is not auto-generated, and it currently says the AI
layer is awaiting a cache.

### 5.4 Record the video

`docs/video-script.md` is written with timings and reads off the screen. Under
five minutes, ends on what could not be solved. Read the numbers off
`docs/metrics.md` at record time rather than off the script.

### 5.5 Submit

Repo URL, and whatever else the form asks for.

---

## 5a. Environment notes for a local session

- **`make setup` first.** The `.venv` in the working tree was created 3 September
  and predates the OpenAI dependency swap. `make lint` and `make test` will
  behave oddly against it.
- **ruff is pinned exactly** (`ruff==0.16.6`). `make lint` is byte-for-byte what
  CI runs; `make fmt` applies the formatter. A green local lint means a green
  build — do not skip it, a formatting miss already broke CI once.
- **No test may reach the network.** `tests/conftest.py` blanks
  `OPENAI_API_KEY` for the whole session, so the suite always uses the replay
  backend even though your `.env` has a real key. `TestTheSuiteIsOffline` asserts
  it. Do not remove that fixture to "test the live path" — record the cache
  instead.
- **Pushing** uses the PAT in `.env`, passed inline so it never lands in
  `.git/config`:
  ```bash
  TOKEN=$(grep -m1 '^GITHUB_TOKEN=' .env | cut -d= -f2-)
  git push "https://x-access-token:${TOKEN}@github.com/deeepak-17/ledger-sentinel.git" main:main
  ```
  From a normal local shell `git push` may just work if you have credentials
  configured; prefer that.
- **Secrets are guarded by a test, not by `.gitignore`.**
  `tests/test_no_secrets.py` scans every tracked file for credential-shaped
  strings and runs as its own CI step before lint. History was audited: nothing
  has ever leaked.

---

## 6. Decisions already taken (do not relitigate)

- **7 cases now, 11 later.** Deepak chose to ship cases 1/2/3/4/5/8/10 and add
  6/7/9/11 only if Phase 3 finishes with buffer. The enum and the `ACTIVE_CASES`
  switch already accommodate all eleven. `RULE_UTR_ALREADY_CLAIMED` in the
  matcher is the seat reserved for case 11.
- **Offline-first LLM.** The classifier sits behind a backend interface with a
  cached-replay backend committed to the repo. The live backend (temperature 0,
  model pinned exactly) is used only when a key is present, and populates the
  cache. **The demo must never depend on the network** -- CI proves this with a
  step that runs the pipeline with `OPENAI_API_KEY` blanked.
- **OpenAI, not Claude.** The plan named Claude for the classifier; the build
  ships against OpenAI because that was the key available. A dependency swap,
  not a design change -- neither the tools, the gate nor the metrics know which
  vendor answered. Stated in the README and `docs/architecture.md` rather than
  left for a reader to notice.
- **The gate does not trust stated confidence.** Two deterministic checks run
  before the threshold: the claimed rows are re-summed against the credit, and
  two disjoint complete settlement batches veto any confidence. This makes "the
  collision is never auto-resolved" a property of the system rather than a hope
  about the model -- and it means a rule, not the AI, is what catches the
  adversarial case. That trade was made deliberately; `docs/video-script.md`
  says so out loud in the closing minute rather than hiding it.
- **Forecast layer is cut** under Track A, and the README must say so explicitly
  rather than quietly omitting it.

---

## 7. Immediate actions, highest priority first

1. **§5.1 — record the cache.** Everything in M3 is unmeasured until this runs,
   and this session is the first one able to do it.
2. **§5.2 — tune on seed_A**, report once on seed_B.
3. **§5.4 — record the video.**
4. Revoke the GitHub PAT.

Resolved earlier today: the deadline question (Track A, repo due tonight), the
README, the assumption citations, version control and a remote, CI green, and
the secret-handling guard.

---

## 8. Picking this up cold

```bash
cd ~/Downloads/Razorpay/ledger-sentinel
make setup       # required — see 5a
make test        # expect 207 passing, no key needed
make lint
make data        # regenerates both seeds + data/README.md; must produce no diff
make metrics     # deterministic layer on held-out seed_B
make demo        # Streamlit
```

To interrogate a number rather than trust it:

```bash
sqlite3 ledger.db "select rule, count(*) from matches group by rule"
sqlite3 ledger.db "select bank_txn_id, rule, reason from exceptions"
sqlite3 ledger.db "select seq, layer, rule, decision, reason from audit
                   where subject_id='BNK00031' order by seq"
```

Read in this order to understand the system: `README.md`, `data/README.md` (what
the problem looks like), `src/cases.py` (the taxonomy), §4 above (why it is built
this way), `src/matcher.py` then `src/gate.py` (the two sets of rules), then
`data/scenarios.py` (how each case is planted). `FAILURES.md` is the shortest
route to understanding what is fragile.

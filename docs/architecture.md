# Architecture

Ledger Sentinel closes one finance-ops loop: three disagreeing sources in, a
reconciled ledger, a diagnosed exception list and an audit trail out.

This document is organised around the four things the track is judged on.

---

## Problem taste

**The hard part of reconciliation is not the arithmetic.**

Fees, GST, T+2 gaps, netted refunds and batched payouts are all arithmetically
closed. Any competent engineer can subtract 2% and add 18% of that. What makes
reconciliation a real problem is that *some of the residual is genuinely
undecidable*, and a tool that cannot tell "hard" from "impossible" will resolve
the impossible ones confidently — which is worse than not resolving them at all.

So the thing this system is built to get right is not the match rate. It is
knowing where to stop.

Three consequences follow, and they shape everything else:

**1. A match requires an identifier.** Amount agreement never closes a match,
because amounts collide. Only a shared UTR opens one; amount and value date are
then *confirmation*, and a disagreement in either raises an exception rather than
a match. This is why the false-match rate is quoted first and can be quoted at
all.

**2. The dataset had to be hard for an honest reason.** In a naive synthetic
dataset every settlement row carries a UTR and so does every bank line, which
makes the whole problem a SQL join. Rather than delete references to manufacture
difficulty, the generator models a real condition: single NEFT credits carry the
UTR in the narration, consolidated payouts arrive over a different rail with a
generic narration and no reference at all. No evidence is removed — a statement
format that never carried it is modelled. That single choice is what splits the
problem 70/30 between the two layers, by construction rather than by tuning.

**3. One case is built to be unresolvable.** Case 10 is a bank credit that two
disjoint sets of settlement rows explain *exactly*. Only one is true. Nothing in
the data decides it. The correct output is escalation, and a system confident
about it has failed in the expensive direction. Four tests assert the trap is
genuinely set rather than merely labelled.

**Costs are asymmetric, and the metrics say so.** An unresolved exception costs a
controller ten minutes. A false match closes a discrepancy that was real and
nobody looks at it again. The report prints false-match rate *before* match rate
for that reason, and the API returns it as the first key in the payload.

---

## Build quality

**Layout.** `config.py` holds every rate, window and threshold; nothing in `src/`
hardcodes one, so a judge can change a number and re-run `make metrics`.
`src/cases.py` is the 11-case taxonomy, the single contract shared by the
generator, the matcher, the classifier and the scorer — nothing else may invent a
case id.

**Money is integer paise, and the column names say so.** Razorpay's settlement
report denominates in rupees; every money column here carries a `_paise` suffix
and holds an integer. Float rupees are the most common source of silent
reconciliation error, and a column name that states its unit is cheaper than a
comment nobody reads. Fee and GST round half-up independently, which is what
produces the one-paise disagreements case 8 models.

**Ground truth is structurally out of reach.** `src/pipeline.py` loads exactly the
three files a merchant would hand over. `src/metrics.py` is the only module that
reads `truth.json`, and a test asserts the database contains no truth table. A
scorer that can see the answers is not a scorer.

**Two seeds, and the reported one is never looked at.** `seed_A` tunes, `seed_B`
reports. Different RNG streams, no shared identifiers, identical shape.
Regenerating either is byte-identical and CI asserts it with
`git diff --exit-code`.

**No number in this repository is typed by hand.** `data/README.md` is emitted by
the generator and `docs/metrics.md` by the metrics module. CI regenerates both and
fails the build if a committed document has drifted. A document that disagrees
with the code is not documentation; it is a claim nobody re-checked.

**Testing.** 194 tests. The dataset is tested harder than the pipeline, because
the dataset is the quality ceiling: ground truth must be total (every row claimed
by exactly one record) and the adversarial case must be genuinely adversarial.
`tests/test_golden.py` asserts the metrics floor **two-sided** — a match rate that
rises unexpectedly is as alarming as one that drops, because the deterministic
layer is supposed to close exactly the cases carrying an identifier, and closing
more than that means it started guessing.

### Architecture decisions

| Decision | Why | Cost accepted |
|---|---|---|
| Deterministic layer first, identifier-only | makes 0% false-match a property, not a hope | match rate caps at 70% before AI |
| SQLite over Postgres | one file, ships in the repo, `sqlite3`-able live in front of a judge | not the answer at 1M rows |
| Tools over prompting | the model reasons over enumerated facts instead of recalling arithmetic | more turns, more tokens |
| Integer paise everywhere | removes a whole class of silent error | conversions at the edges |
| Content-addressed response cache | the demo never depends on the network | a changed prompt invalidates the cache, loudly |
| Both layers speak one `Match` type | the AI gets no softer accounting than the rules | AI matches carry a synthetic rule name |

### Scale

Not built; stated. At 10k records the subset-sum tool needs candidate windowing
(by date ±3 days and method) and the matcher moves to indexed SQL — the indices
are already in the schema. At 1M, batch the classifier and move to Postgres. The
matcher is already a pure function of its input, so it parallelises by settlement
window without restructuring.

---

## AI judgment

### Where AI is deliberately not used

Ingestion. Fee and GST arithmetic. The settlement calendar. Deterministic
matching. Subset-sum enumeration. Threshold gating. **The model classifies and
explains; it never computes money.** Every figure it sees came from the same
functions the deterministic layer uses, so the two halves cannot disagree about a
fee, a net or a sum.

### What the model is and is not told

It gets the taxonomy, because a trained controller would know it — but written
fresh in `src/classifier.py` as neutral descriptions of each phenomenon. The
`resolvable_by` field in `src/cases.py`, which records which layer is *supposed*
to close each case, is never sent; a test asserts the prompt carries no verdict
language.

It **is** told the risk posture — an unresolved item is cheap, a wrong
attribution is expensive, two readings with nothing between them should be
escalated. That is policy, applies to every exception equally, and tells it
nothing about which one is the trap.

### The tools

| Tool | What it does | Why it is a tool and not a prompt |
|---|---|---|
| `query_candidates` | unclaimed settlement rows, filtered by window, amount, order, method | the model cannot reach a row an identifier already claimed |
| `expected_fee` | fee and GST for an amount and method | one implementation of the arithmetic in the whole repository |
| `subset_sum` | **every** combination summing to the credit, refunds negative | see below |

`subset_sum` returning *all* solutions rather than the first is the single most
important line in the tool layer. Returning the first is precisely how a system
auto-resolves an amount collision: the construction is that one row explains the
credit and a disjoint pair also does, so a tool that stops at the first hit hands
the model one answer and erases the finding.

It also reports which settlement batches each solution draws from and whether it
takes one whole — a fact, not a verdict. A real consolidated credit is one payout
and every row in a payout shares a `settlement_id`. That distinguishes an ordinary
batch from a coincidental sum, and **it does not resolve case 10**, where both
readings are complete batches on both seeds.

### The gate does not trust confidence

Stated confidence is a self-report, and self-reports fail exactly where this
system must not: an amount collision *looks easy*, because the arithmetic closes
perfectly. So confidence is the last check, not the first.

1. **The model escalated** → escalate.
2. **No rows named** → nothing to book.
3. **The arithmetic is re-verified.** The claimed rows are re-summed against the
   credit using the matcher's own money functions. A right case with wrong rows
   is not booked.
4. **Ambiguity vetoes any confidence.** If the tool trace holds two complete
   settlement batches that each explain the credit exactly and share no rows,
   nothing auto-resolves it. Derived from evidence, not from the model's opinion
   of itself — so it holds when the model misdiagnoses the case entirely, which
   the tests drive directly.
5. **Named adversarial cases never auto-resolve**, whatever the confidence.
6. Only then, the threshold — tuned on `seed_A`, reported on `seed_B`.

### Reproducibility

Temperature 0, model pinned exactly, every request content-addressed and its
response committed to `cache/llm_responses.jsonl`. With a key the live backend
answers and records; without one the replay backend serves the same bytes and the
whole pipeline runs offline. `scripts/determinism.py` asserts three identical
runs. A live API call in front of a judge is a coin flip, and "it worked this
morning" is not a result.

*The plan named Claude for this layer; the build ships against OpenAI because
that was the key available. A dependency swap, not a design change — the
classifier sits behind a backend interface and neither the tools, the gate nor
the metrics know which vendor answered.*

---

## Failure recovery

`FAILURES.md` carries the full log. The pattern worth naming here is that **every
one of them was caught by something written to catch it**, not by inspection:

- the adversarial case was not adversarial — caught by the test written to prove
  that it was;
- UPI was priced against a rule that does not apply — caught by doing the Day-0
  sourcing action three days late;
- the held-out set contains an unplanted ambiguity — caught by running the tools
  over both seeds *before* writing the classifier;
- confidence was nearly treated as a defence — caught by asking what happens when
  the model misdiagnoses the case the backstop keys on.

### In the system itself

**Fails toward escalation, never toward a match.** A classifier that does not
converge in eight turns, that answers in prose, that names unknown rows, or whose
arithmetic does not re-check, all produce an escalation with a reason. The
dangerous direction is booking something wrong; the safe direction costs ten
minutes.

**A malformed tool call costs a turn, not the run.** Tool errors come back to the
model as results.

**Ingestion fails loudly.** Header drift is rejected rather than guessed, and a
bad row raises with the file and the line number. A row that quietly became an
exception would inflate the exception list and flatter the match rate.

**Zero unlogged decisions is an SLO**, asserted in the test suite. Every match and
every exception writes an audit row naming the rule, the inputs, the decision and
the reason; the AI layer additionally logs every tool call, which is the answer to
the obvious "this is just an LLM lookup table" critique.

**If the live demo breaks**, switch to the replay backend, say so out loud, and
show the audit log. The offline path is the default, not the fallback.

---

## What is not built

The forecast layer is **cut**, not deferred quietly. The original plan paired this
loop with a payout-timing forecaster beating a naive T+2 baseline. Under a
72-hour track, one measured loop is worth more than two gestured-at ones.

Cases 6, 7, 9 and 11 are defined in the taxonomy and dormant.
`RULE_UTR_ALREADY_CLAIMED` sits in the matcher as the seat reserved for case 11
and has never fired against real data.

`docs/exceptions.md` and `docs/runbook.md` are in the plan and are not written.

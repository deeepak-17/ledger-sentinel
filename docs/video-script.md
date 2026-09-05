# Five-minute video script

Read it in your own words — the timings matter more than the wording. Numbers in
**bold** must match `docs/metrics.md` at the time you record; regenerate with
`make metrics` first and read them off the screen rather than off this page.

Record with two windows: a terminal and the Streamlit demo. Start on the terminal.

---

## 0:00 – 0:35 · The problem

> A merchant's order book, Razorpay's settlement report, and their bank
> statement never agree. Fees, GST on those fees, T+2 gaps stretched by a
> weekend, refunds netted inside a payout, several settlements leaving as one
> credit.
>
> Most tools report a match rate and drop the rest on a human. But the hard part
> isn't the arithmetic — it's that **some of the residual is genuinely
> undecidable**, and a tool that can't tell "hard" from "impossible" will resolve
> the impossible ones confidently. That's the expensive mistake, and it's the one
> this is built to avoid.

> Three CSVs go in. A reconciled ledger, a diagnosed exception list and an audit
> log come out — and there's a case in here designed to fool it.

## 0:35 – 1:20 · The data, and why it's hard honestly

Show `data/README.md`.

> The dataset is synthetic and the generator is committed, so you can audit how
> hard it actually is instead of taking my match rate on trust. Two seeds:
> **seed_A** tunes, **seed_B** reports and is never looked at while tuning.
> Regenerating either is byte-identical and CI checks it.
>
> Here's the honest part. In a naive synthetic dataset every settlement row has a
> UTR and so does every bank line, which makes the whole problem a SQL join. I
> didn't delete references to make it harder. I modelled a real thing: single
> NEFT credits carry the reference in the narration, consolidated payouts arrive
> over a different rail with a generic narration and no reference at all.
>
> That's why **9 of 65** bank lines have nothing to join on, and it's what splits
> the problem between the two layers — by construction, not by tuning.

## 1:20 – 2:15 · The deterministic layer

Run `make metrics`. Let the report fill the screen.

> False-match rate first, before the match rate. That ordering is the whole
> argument. Any tool can hit whatever match rate you ask by lowering its standard
> of evidence — but an unresolved exception costs a controller ten minutes, and a
> false match closes a real discrepancy that nobody ever looks at again.
>
> **0.0% false matches. 70.0% match rate** — 56 of 80 orders.
>
> The rule that earns that: **a match requires an identifier**. Amount agreement
> never closes anything, because amounts collide. A shared UTR opens the match,
> and then the amount and date only confirm it — if either disagrees, that's an
> exception, not a match.

Run one `sqlite3` query against `ledger.db`.

> And it's all in SQLite, in the repo. You don't have to believe the number —
> count the rows yourself.

## 2:15 – 3:15 · Where the AI earns its place

Switch to the demo, Exceptions tab.

> Everything without a reference comes here. The classifier has three tools, and
> it never computes money — every fee, net and sum comes back from the same
> functions the deterministic matcher uses, so the two halves can't disagree
> about arithmetic.
>
> The important one is `subset_sum`, and the important thing about it is that it
> returns **every** combination that sums to the credit, not the first one.
> Returning the first is exactly how a system auto-resolves a collision — it
> hands the model one answer and erases the ambiguity that was the whole finding.

Open the audit trail for a resolved credit; show the tool calls.

> Every tool call is logged, which is the answer to "this is just an LLM lookup
> table". You can watch it enumerate.

## 3:15 – 4:15 · The case designed to fool it

Adversarial case tab. Slow down here — this is the minute that matters.

> Two disjoint sets of settlement rows explain this credit **exactly**. Only one
> is true. And nothing in the data decides it: the decoy isn't on hold, isn't
> late, isn't flagged, and settles in the same window with the same date.
>
> I got this wrong the first time. The original version marked the decoy
> on-hold — which meant a careful matcher could rule it out, and the "must
> escalate" claim was theatre. The test I'd written to prove the trap was set is
> what caught it. It's entry 1 in `FAILURES.md`.

> The system refuses this one. And it doesn't refuse it because the model was
> smart enough — it refuses because the gate doesn't trust confidence. Before the
> threshold is even consulted, the claimed rows get re-summed against the credit,
> and if the tools found two complete settlement batches that each explain it
> exactly and share no rows, **no stated confidence can book it**. That's derived
> from evidence, not from the model's opinion of itself — so it holds even when
> the classifier misdiagnoses the case entirely. There's a test that drives
> exactly that: a deliberately wrong diagnosis at confidence 1.0, still refused.

## 4:15 – 5:00 · What I couldn't solve

Do not soften this. It is the strongest minute in the video.

> Four things I'd want you to know.
>
> **One.** I zero-rated UPI on the strength of the NPCI zero-MDR mandate. That
> mandate removes interchange — Razorpay's 2% is a platform fee and applies to
> UPI too. The assumption was plausible, cited a real rule, and applied it to the
> wrong thing. I kept the deviation, because the dataset needs one clean-match
> control group, but it's stated in the README rather than buried.
>
> **Two.** Testing the tools turned up an ambiguity in the held-out set that
> nobody planted — an ordinary batched payout with a second exact reading the RNG
> produced by accident. I didn't reseed it away. Regenerating until the
> collisions disappear would be tuning the data to flatter the result.
>
> **Three.** The forecast layer is cut. The plan paired this with payout-timing
> forecasting; under 72 hours, one measured loop beats two gestured-at ones.
> Cases 6, 7, 9 and 11 are defined and dormant.
>
> **Four.** The honest limit: the deterministic guardrail is doing the work of
> catching the collision, not the model's judgement. That's the right
> engineering — but it means I've proved the system is safe, not that the model
> is wise. With two more weeks, that's what I'd go after.

---

## Before you hit record

- [ ] `make data` then `make metrics` — read the numbers off the screen
- [ ] `git status` clean, everything pushed
- [ ] `make demo` already running, seed_B loaded, tabs pre-clicked once
- [ ] one `sqlite3` query ready to paste
- [ ] under 5:00, and it ends on what you couldn't solve

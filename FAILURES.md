# Failures

Things that went wrong, in the order they happened. Each entry says what broke,
how it was caught, and what changed — including the ones nobody would have found
if they were not written down.

A build log with no failures in it is a build log that was edited.

---

## 1. The adversarial case was not adversarial

**What happened.** Case 10 is supposed to be genuinely undecidable: a bank credit
that two different sets of settlement rows explain exactly, with nothing in the
data to choose between them. The first version marked the decoy settlement
`on_hold`.

**Why that was wrong.** A held settlement has not been paid out, so a careful
matcher could rule the decoy out and resolve the credit. The case would still
have been *labelled* "must escalate" and the system would still have escalated
it — but only because it was told to, not because the data was ambiguous. The
headline claim would have been theatre.

**How it was caught.** Writing the test that was supposed to prove the trap was
set. `TestCollisionIsGenuinelyAmbiguous` asserts four things: one row explains
the credit exactly, a *disjoint* pair also explains it exactly, no field
distinguishes them, and the credit has no reference to join on. The third
assertion failed.

**What changed.** The decoy is now not held, not late, not flagged, and settles
in the same window with the same `settled_at`. Its payout simply falls outside
this statement export — an ordinary partial download. The four tests now pass and
are load-bearing: if a future change makes case 10 solvable, they fail on
purpose. **Do not relax them to make the matcher look better.**

---

## 2. The working directory vanished, and there was no version control

**What happened.** On 3 September the parent folder was renamed `Razor-Pay` →
`Razorpay`. The working tree was lost and every file had to be reconstructed from
scratch.

**Why it was possible.** Two milestones of work existed with no `git init`. There
was no second recovery path, and no way to tell what had been lost versus what
had never been written.

**What changed.** The repository was initialised before any further code was
written, with `.gitignore` covering `.venv/`, the caches and `ledger.db` but
deliberately **not** `data/seed_*/`, which CI diffs. The `.env` ignore rule was
committed *before* any key existed, so there is no window in which an
absent-minded `git add -A` could commit a secret.

**What it cost.** Roughly a day of rebuild, and the reason the M0+M1 history is a
single squashed commit rather than the sequence it should have been.

---

## 3. UPI was priced against a rule that does not apply

**What happened.** `config.py` zero-rated UPI on the grounds that the NPCI
mandate sets zero MDR on person-to-merchant UPI. That was carried as `[assumed]`
for three days.

**Why it was wrong.** Razorpay's published pricing is 2% + GST across *all*
modes, UPI included, because the 2% is Razorpay's platform fee and not
interchange. The zero-MDR mandate removes the interchange, not an aggregator's
own fee. The assumption was plausible, cited a real rule, and applied it to the
wrong thing — which is the most dangerous kind of assumption, because it survives
a casual review.

**How it was caught.** Doing the Day-0 action three days late: actually opening
[razorpay.com/pricing](https://razorpay.com/pricing/) and reading it, rather than
reasoning from what the mandate says.

**What changed.** Not the number. Regenerating both seeds against real pricing
would collapse case 1 ("clean match", where the credit equals the order exactly)
into case 2, and the dataset would lose its control group — every case in the
taxonomy would then involve fee arithmetic and the system could never demonstrate
a clean match at all. The deviation is kept **and stated in the open** in the
README and in `config.py`, with both sources cited, rather than left for a reader
to discover.

The other two assumptions were checked at the same time and were correct: T+2
*working* days from capture, and 18% GST levied on the fee rather than the
transaction value. Both are now cited rather than assumed.

---

## 4. The held-out set contains an ambiguity nobody planted

**What happened.** `BNK00015` in seed_B is an ordinary batched payout (case 4)
whose ground truth says "matched". Testing `subset_sum` against it turned up a
*second* combination that sums to the credit exactly — four rows drawn from
cases 4, 10 and 5.

**Why it matters.** It is real ambiguity in the data the published numbers come
from, and it was produced by the RNG rather than by any builder. It caps what an
honest classifier can score: a system reasoning purely from amounts either
escalates it (marked wrong against ground truth) or picks one reading for no
reason.

**How it was caught.** Running the tools over both seeds before writing any
classifier, specifically to see what the model would be shown.

**What changed.** Two things.

First, it is **not** regenerated away. Reseeding until the collisions disappear
would be tuning the dataset to flatter the result, and the accidental collision
is a more honest test than the planted one precisely because nobody designed it.

Second, it motivated the discriminator the classifier now reasons with: a real
consolidated credit is *one payout*, and every row in a payout carries the same
`settlement_id`. The intended reading of `BNK00015` is one complete settlement
batch; the accidental one is stitched from three unrelated batches.

The important part is what that discriminator does **not** do. On case 10, both
readings are complete batches — `{a, b}` share a batch id and `c` is its own
payout — so structure does not resolve the adversarial case, on either seed.
`TestBatchStructureDoesNotSolveTheCollision` asserts exactly this, so that the
day someone breaks the property in pursuit of a higher match rate, the build says
so instead of the demo.

---

## 5. The classifier's confidence is not a defence, and was nearly treated as one

**What happened.** The gate's first design was the obvious one: auto-resolve
above a threshold, escalate below, with the adversarial case ids on a
never-auto-resolve list as a backstop.

**Why that was not enough.** The backstop keys on the case id the *model*
reports. A model that misdiagnoses an amount collision as an ordinary batched
payout and states 0.97 confidence sails straight through it — and that is the
likely failure, not an exotic one, because a collision looks *easy*. The
arithmetic closes perfectly. High confidence there is not a prompt bug; it is
the honest output of a system that cannot see what it is missing.

**What changed.** Two deterministic checks now run *before* confidence is
consulted at all:

- the rows the model claims are re-summed against the credit, using the same
  money functions the deterministic matcher uses. A diagnosis that does not
  survive re-checking is not booked whatever it claims.
- if the tool trace contains two complete settlement batches that each explain
  the credit exactly and share no rows, **no confidence auto-resolves it**. This
  is derived from evidence the model saw, not from the model's opinion of itself,
  so it holds even when the model fails to notice the ambiguity.

`tests/test_ai_layer.py` drives exactly that scenario — a deliberately
misdiagnosed collision at confidence 1.0 — and asserts the gate refuses it.

---

## 6. Still open

- **The deadline question went unanswered for three days.** The plan's own Day-0
  action was to confirm whether the repo was due at application time. It was
  answered on the morning of the deadline. Everything downstream was scheduled
  against a date nobody had checked.
- **`docs/exceptions.md` and `docs/runbook.md` are not written.** They are in the
  plan's M4 deliverables and they did not fit.
- **Cases 6, 7, 9 and 11 are defined and dormant.** `RULE_UTR_ALREADY_CLAIMED`
  exists in the matcher as the seat reserved for case 11, and has never fired
  against real data.
- **The forecast layer is cut**, not deferred quietly — see the README.

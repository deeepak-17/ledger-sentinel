# Runbook

What to do when something needs doing. Written for whoever is holding this at
11pm, not for whoever wrote it.

## Run it from nothing

```bash
git clone <repo> && cd ledger-sentinel
make setup     # uv venv on Python 3.11 + install
make test      # 200 tests, no key and no network needed
make metrics   # deterministic layer on the held-out seed
make ai        # the whole system, replaying the committed cache
make demo      # Streamlit, the one command a judge runs
```

No step above requires an API key. Recording the cache does; nothing else.

## Regenerate the data

```bash
make data
git diff --exit-code -- data/     # must be empty
```

If that diff is not empty, the generator changed and the committed seeds are
stale. Either the change was intended — commit the regenerated CSVs *and*
re-run `make metrics`, because every published number just moved — or it was
not, in which case find out what touched a builder or a seed constant.

**Never hand-edit anything under `data/`.** `data/README.md` is emitted by
`python -m data.generator --readme` and CI diffs it.

## Re-record the LLM cache

Needed when the system prompt, the tool schemas or the pinned model change. The
cache key is a hash of the whole request, so a changed prompt does not silently
serve a stale answer — it raises `CacheMiss`.

```bash
printf 'OPENAI_API_KEY=sk-...\n' > .env      # gitignored; never commit it
python scripts/record_cache.py --dry-run     # what is already covered
make cache                                   # records seed_A and seed_B
git add cache/llm_responses.jsonl && git commit
```

Content-addressed, so re-running costs nothing for prompts already recorded.
Record **both** seeds — discovering mid-demo that only the seed you were not
going to show is covered is an avoidable way to lose.

## Handle a key

```bash
cp .env.example .env      # then fill in OPENAI_API_KEY
```

`.env` and every `.env.*` variant are gitignored, along with `*.key`, `*.pem`
and `credentials.json`. But **the ignore rules are not the guard** — they only
help when the file is named the way you expected, and they do nothing about a key
pasted into `config.py`. `tests/test_no_secrets.py` scans every git-tracked file
for credential-shaped strings and runs as its own CI step, before lint, so a leak
is the first line of the log.

Exactly one file in `src/` reads the key, and a test asserts that stays true. The
committed response cache holds only the model, the messages and the token counts
— never a header or a client config — and there is a test for that too.

**If a key ever does reach the remote: rotate it.** Do not revert and assume it
is fine. Anything pushed to a public repository is compromised from the moment it
lands, and a force-push does not un-fetch it.

## Reproduce a number a judge is questioning

Every figure in `docs/metrics.md` and the README comes from `make metrics`.
Nothing is typed by hand, and CI fails if a committed document has drifted.

To interrogate rather than trust:

```bash
sqlite3 ledger.db "select rule, count(*) from matches group by rule"
sqlite3 ledger.db "select bank_txn_id, rule, reason from exceptions"
sqlite3 ledger.db "select seq, layer, rule, decision, reason from audit
                   where subject_id='BNK00031' order by seq"
```

That last query is the one to run when someone asks why a specific credit was
not closed.

## Tune the gate threshold

**On `seed_A` only.** `seed_B` is the held-out set and looking at it while tuning
destroys the only thing that makes its numbers worth quoting.

```bash
python -m src.metrics --seed A --ai --threshold 0.85 --no-markdown
python -m src.metrics --seed A --ai --threshold 0.95 --no-markdown
```

Pick the value, put it in `config.py` as `AUTO_RESOLVE_THRESHOLD`, then report
once on seed_B and do not iterate on what you see.

## Change an assumption

Every rate, window and threshold is in `config.py`; nothing in `src/` hardcodes
one. Change the number, re-run `make metrics`, and update the citation in the
README table in the same commit. An assumption whose source is not written down
becomes folklore within a week — see `FAILURES.md` entry 3 for what that costs.

## The live demo is failing

1. The offline path is the **default**, not the fallback: `make demo` and
   `make ai` never touch the network. If something is reaching for it, a cache
   entry is missing — the error names the request hash.
2. If the classifier is misbehaving, `make metrics` (deterministic layer only)
   still stands entirely on its own and reports a real number.
3. Say out loud which one is running. Showing the audit log while explaining that
   the AI layer is on replay is a better minute than pretending.

## Incidents seen so far

| Symptom | Cause | Fix |
|---|---|---|
| `CacheMiss` on every exception | prompt or tool schema changed since recording | `make cache` with a key; do not hand-edit the cache |
| `git diff` non-empty after `make data` | a builder or seed constant moved | intended → commit and re-run metrics; not → revert |
| Match rate *rose* unexpectedly | the deterministic layer started guessing | `tests/test_golden.py` fails two-sided by design; find what loosened |
| `TestBatchStructureDoesNotSolveTheCollision` fails | something made case 10 resolvable | fix the change, do not relax the test |
| Working tree gone after a folder rename | no version control | already fixed; push, and keep pushing |

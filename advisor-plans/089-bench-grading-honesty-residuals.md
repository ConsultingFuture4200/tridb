# Plan 089: Close two benchmark-grading honesty residuals (SM-4 empty-oracle recall, live_report manifest seed)

> **Executor instructions**: Host-Python only; both fixes are small and must land with tests that
> fail pre-fix. Do not regenerate committed benchmark reports. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- bench/wikidata_sm4_seedless.py bench/live_report.py tools/real_corpus.py tools/bench_corpus.py tests/`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: measurement integrity / tests
- **Planned at**: commit `a780b46`, 2026-07-16

## Why this matters

Two grading seams can silently inflate or mis-derive published numbers. (a) The SM-4 seedless
bench's local `recall_at_k` scores an empty oracle as a perfect 1.0 even when the engine returned
junk rows — the shared implementation deliberately scores that 0.0. (b) `live_report` rebuilds the
grading corpus from a `--seed` flag defaulting to 42 instead of the seed recorded in the manifest,
so grading a corpus generated with any other seed silently produces a wrong oracle and wrong
SM-1/SM-4 numbers.

## Current state (verified)

- `bench/wikidata_sm4_seedless.py:76-80` (local copy):
  ```python
  def recall_at_k(ids: list[int], oracle_ids: list[int]) -> float:
      o = set(oracle_ids)
      if not o:
          return 1.0
      return len(o & set(ids)) / len(o)
  ```
  vs the shared semantics in `tools/real_corpus.py:370-373`:
  ```python
  if not truth:
      return 1.0 if not got else 0.0  # empty truth: perfect only if nothing returned
  ```
  Plan 069 unified another SM-4 recall seam; this local copy survived it.
- `bench/live_report.py:361` — `report = build_report(text, manifest, args.seed)` where `--seed`
  defaults to 42 (`live_report.py:344-346`); `build_report` (line 219) calls
  `rebuild_corpus(manifest, seed)`. The manifest already records the generating seed:
  `tools/bench_corpus.py:112` writes `"seed": args.seed` into the manifest JSON.
- Context, NOT in scope: `live_report.py:317-320` sets SM-2 `passed = True` with an explicit
  documented rationale ("not a fail: it is simply not measured head-to-head here") — that is a
  recorded decision; do not change its semantics in this plan.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/test_live_report.py tests/ -q -k 'seed or recall'` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `bench/wikidata_sm4_seedless.py`
- `bench/live_report.py`
- The existing test homes for each (extend; create a focused test module only if none exists for
  the touched behavior)

**Out of scope**:
- The SM-2 gated/passed representation (documented decision).
- `tools/real_corpus.py`, `tools/bench_corpus.py` (reference semantics; read-only).
- Regenerating any committed report/manifest artifact.

## Git workflow

Use assigned `dustin/dev-NNNN`. Suggested commit: `fix(bench): close grading honesty residuals`.

## Steps

### Step 1: SM-4 empty-oracle semantics

Make the seedless bench's recall use the shared empty-truth rule — preferably by importing/reusing
the shared function (check how plan 069's seam did it for the other module and mirror that
pattern); a local copy is acceptable only if imports are structurally blocked, and then it must
replicate `1.0 if not got else 0.0`. Add a test: empty oracle + nonempty ids → 0.0; empty oracle +
empty ids → 1.0.

**Verify (negative control)**: the empty-oracle+nonempty-ids test fails pre-fix.

### Step 2: Manifest seed is authoritative in live_report

In `main`, use `manifest["seed"]` when present; keep `--seed` only as an explicit override and
ERROR (do not warn-and-continue) if both are given and disagree. A manifest without a `seed` key
falls back to the flag with a warning. Add tests for: manifest seed used by default; conflicting
explicit flag errors; legacy manifest without seed uses flag.

**Verify (negative control)**: a test grading a manifest with `"seed": 7` while defaulting the
flag must fail pre-fix (wrong corpus) or, if corpus rebuild is too heavy for a unit test, assert on
the seed value passed to `rebuild_corpus` via a seam/monkeypatch.

### Step 3: Full verification

**Verify**: `make test && make lint && git diff --check` exit 0; only in-scope files changed.

## Test plan

Empty-oracle boundary both ways, seed-authority matrix (manifest-only, flag-only, both-agree,
both-conflict, legacy), plus the existing suites untouched.

## Done criteria

- [ ] SM-4 seedless recall scores empty-oracle+junk as 0.0, matching the shared rule.
- [ ] `live_report` grades with the manifest's recorded seed by default and refuses a conflicting
      flag.
- [ ] Negative controls demonstrably failed pre-fix; host tests/lint green.

## STOP conditions

- The manifest schema no longer records `seed` (re-check `tools/bench_corpus.py`).
- Reusing the shared recall requires a circular import — report the structure, don't hack it.
- Any committed report would need regeneration to keep tests green.

## Maintenance notes

Grading inputs (seed, corpus identity) should always come from the run's own manifest; flags are
for override with conflict detection, never silent defaults. Any new recall copy is a bug — reuse
the shared reducer.

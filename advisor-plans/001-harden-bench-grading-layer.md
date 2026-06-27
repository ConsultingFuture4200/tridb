# Plan 001: Harden + test the benchmark grading layer

> **Executor instructions**: Follow this plan step by step. Run every verification command and confirm
> the expected result before moving on. If a STOP condition occurs, stop and report — do not improvise.
> When done, update the status row for this plan in `advisor-plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 7bf3dca..HEAD -- tools/real_corpus.py tools/sweep_corpus.py tools/bench_corpus.py tools/bench_corpus_shared.py`
> If any of those changed since this plan was written, compare the "Current state" excerpts below
> against the live code before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug + tests
- **Planned at**: commit `7bf3dca`, 2026-06-26

## Why this matters

The Python layer (`tools/*.py`, `bench/*.py`) computes the recall@k / SM-4 numbers that go into the
public GTM benchmark writeup. Two real inconsistencies live in the two recall oracles, and the newest
grading module (`tools/sweep_corpus.py`) has **zero unit tests** — so a grading regression would ship
silently into a launch number. This plan makes the two oracles agree, fixes the empty-oracle bug, and
gives `sweep_corpus` the same test coverage `real_corpus` already has. All verifiable with `make test`.

## Current state

- `tools/real_corpus.py` — real-dataset corpus + exact numpy recall oracle.
  - `exact_oracle` tiebreak is CORRECT (line 340): `order = np.lexsort((cand, d2))` — matches the SQL
    oracle's `ORDER BY d2, id`. Do not change this.
  - `recall_at_k` (lines 349–361) has the bug: an empty oracle returns 1.0 **regardless of what the
    engine returned**:
    ```python
    truth = oracle if k is None else oracle[:k]
    if not truth:
        return 1.0                      # BUG: ignores `returned`; a false-positive scores 1.0
    got = set(returned if k is None else returned[:k])
    return len(got & set(truth)) / len(truth)
    ```
- `tools/sweep_corpus.py` — index-quality × term_cond sweep; numpy oracle + transcript grader.
  - Oracle tiebreak DIVERGES (lines 82–84): `order = cd[np.argsort(d2, kind="stable")]` — `argsort`
    breaks ties by candidate-list position, not by id, so it disagrees with the SQL oracle and with
    `real_corpus`'s `lexsort`. (Practical impact is small — recall here is set-based and float64 ties
    are measure-zero — but it should match for correctness/consistency, esp. for integer/quantized
    datasets.)
  - `_recall` (lines 186–189) has the CORRECT empty-oracle semantics — use it as the reference:
    ```python
    def _recall(returned, oracle):
        if not oracle:
            return 1.0 if not returned else 0.0
        return len(set(returned) & set(oracle)) / len(oracle)
    ```
  - There is **no `tests/test_sweep_corpus.py`**.
- `tools/bench_corpus.py` — `build_sql()` (around line 125) is "THE SINGLE SOURCE OF TRUTH" for the
  `#BENCH` SQL, shared by `real_corpus` and `bench_corpus_shared`. No test asserts the two emit
  byte-identical SQL.
- Window-bound math is unchecked in three generators — `tools/bench_corpus.py:91`,
  `tools/bench_corpus_shared.py:77`, `tools/sweep_corpus.py:75`: `int(rng.integers(args.time_min, args.time_max - args.window + 2))`
  raises a cryptic numpy `ValueError("low >= high")` when `window > time_max - time_min + 1`.
- Test conventions: pytest, numpy-only, no network/Docker, deterministic via a seed. **Model new tests
  on `tests/test_real_corpus.py`** (it covers loader, oracle-vs-brute-force, recall perfect/degrade,
  SQL markers, determinism). Style: WHY-first docstrings, no emojis (`tools/bench_corpus.py` is the voice).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `make test` | all pass (currently 110; this plan adds tests) |
| Lint | `make lint` | `All checks passed!` + formatted |
| Run one test file | `. .venv/bin/activate && pytest tests/test_sweep_corpus.py -q` | new tests pass |

(If `.venv` is missing: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`.)

## Scope

**In scope:**
- `tools/real_corpus.py` (recall_at_k fix + window validation)
- `tools/sweep_corpus.py` (oracle lexsort fix + window validation)
- `tools/bench_corpus.py`, `tools/bench_corpus_shared.py` (window validation only)
- `tests/test_sweep_corpus.py` (create)
- `tests/test_real_corpus.py` (add empty-oracle regression test)
- `tests/test_bench_corpus_shared.py` (add build_sql byte-identity test)

**Out of scope (do NOT touch):**
- `real_corpus.exact_oracle`'s `lexsort` (line 340) — already correct.
- `bench/*.py`, `bench/live_report.py` — separate concern.
- Anything under `vendor/`, `src/`, `scripts/`.
- The `#BENCH` SQL format / `build_sql` body — changing emitted SQL would break the live harness.

## Git workflow

- Branch: `advisor/001-bench-grading` (or the repo's `dustin/dev-NNNN` convention if you have an issue).
- Commit style (conventional, from `git log`): `fix(bench): ...` / `test(bench): ...`. Do NOT push or open a PR unless told to.

## Steps

### Step 1: Fix `real_corpus.recall_at_k` empty-oracle semantics

Compute `got` before the empty-oracle branch and mirror `sweep_corpus._recall`:
```python
truth = oracle if k is None else oracle[:k]
got = set(returned if k is None else returned[:k])
if not truth:
    return 1.0 if not got else 0.0   # empty truth: perfect only if nothing was returned
return len(got & set(truth)) / len(truth)
```
Update the docstring line that claims an empty oracle is "recall 1.0" to state the conditional.

**Verify**: `. .venv/bin/activate && pytest tests/test_real_corpus.py -q` → still passes.

### Step 2: Make `sweep_corpus` oracle tiebreak match the SQL oracle

In `tools/sweep_corpus.py` (~line 83) replace `np.argsort(d2, kind="stable")` with a lexicographic
sort by `(d2, id)`, matching `real_corpus.exact_oracle` and the SQL `ORDER BY d2, id`:
```python
cd = np.array(cand)
d2 = np.sum((emb[cd] - qv) ** 2, axis=1)
order = cd[np.lexsort((cd, d2))]      # ties broken by id, like ORDER BY d2, id
oracle = [int(x) for x in order[: args.k]]
```

**Verify**: `. .venv/bin/activate && python3 -m tools.sweep_corpus --entities 200 --dim 16 --hubs 4 --fanout 30 --queries 3 --k 5 --index-configs "16:200" --term-conds "50" --seed 42 --sql-out /tmp/s.sql --manifest-out /tmp/s.json` → exits 0, prints the `[sweep_corpus] wrote` line.

### Step 3: Add window-bound validation to the three generators

In each of `tools/bench_corpus.py`, `tools/bench_corpus_shared.py`, `tools/sweep_corpus.py`, immediately
before the first `rng.integers(args.time_min, args.time_max - args.window + 2)`, add:
```python
if args.window > args.time_max - args.time_min + 1:
    raise ValueError(
        f"window ({args.window}) must fit in the time range "
        f"({args.time_max - args.time_min + 1} = time_max - time_min + 1)"
    )
```

**Verify**: `make lint` → clean.

### Step 4: Create `tests/test_sweep_corpus.py`

Mirror `tests/test_real_corpus.py`. Cover, at minimum:
- `test_oracle_is_exact_top_k`: build a tiny corpus; for each query recompute the brute-force top-k
  (reachable dst in the ts window, nearest by true L2, ties by id) independently and assert equality
  with the manifest `queries[i]["oracle"]`.
- `test_oracle_tiebreak_matches_id_order`: construct a case with an exact distance tie and assert the
  lower id wins (guards Step 2).
- `test_recall_perfect_and_degrades`: feed `report()` a synthetic transcript with perfect ids
  (recall 1.0) and with one missing id (recall < 1.0).
- `test_recall_empty_oracle`: a query whose ts window excludes all reachable dst → oracle `[]`; assert
  `_recall([], []) == 1.0` and `_recall([5], []) == 0.0`.
- `test_report_parses_build_examined_latency`: a synthetic transcript with `#SWEEP BUILD_BEGIN`/`Time:`,
  `#SWEEP RESULT`, `#SWEEP EXAMINED`, `#SWEEP EXPLAIN_BEGIN`/`Execution Time:` → assert `build_ms`,
  `mean_examined`, `median_latency_ms`, `mean_recall@k` populate correctly.
- `test_determinism_same_seed`: two `build()` calls with the same args produce identical SQL + manifest.

Use the synthetic-transcript construction already proven in this repo's history (see the inline parser
self-test pattern); keep it numpy-only, no Docker.

**Verify**: `. .venv/bin/activate && pytest tests/test_sweep_corpus.py -q` → all new tests pass.

### Step 5: Add `build_sql` byte-identity + real_corpus empty-oracle regression tests

- In `tests/test_bench_corpus_shared.py`, add a test that emits SQL from `bench_corpus.build()` and from
  the shared path, normalizes ONLY the `-- AUTO-GENERATED by <source>` header line, and asserts the rest
  is byte-identical (guards the "single source of truth" contract).
- In `tests/test_real_corpus.py`, add `test_recall_empty_oracle_returns_zero_on_false_positive`
  asserting `recall_at_k([1,2], []) == 0.0` and `recall_at_k([], []) == 1.0` (guards Step 1).

**Verify**: `make test` → all pass.

## Test plan

New/added tests: `tests/test_sweep_corpus.py` (6 cases above), plus one case each in
`tests/test_bench_corpus_shared.py` and `tests/test_real_corpus.py`. Pattern source:
`tests/test_real_corpus.py`. Final gate: `make test` → all pass (110 existing + the new ones), `make lint` clean.

## Done criteria

- [ ] `make test` exits 0; `tests/test_sweep_corpus.py` exists and its tests pass.
- [ ] `make lint` exits 0.
- [ ] `grep -n "argsort" tools/sweep_corpus.py` returns nothing in the oracle block (lexsort used).
- [ ] `grep -n "if not truth" tools/real_corpus.py` shows the conditional now also checks `got`.
- [ ] No files outside the in-scope list modified (`git status`).
- [ ] `advisor-plans/README.md` status row updated.

## STOP conditions

- An existing `test_real_corpus.py` test fails after Step 1/Step 2 — it may encode the old behavior;
  report rather than weakening the test.
- The byte-identity test in Step 5 reveals a real divergence between the two SQL emitters (not just the
  header line) — that is a separate finding; report it.
- The drift check shows any in-scope file changed since `7bf3dca` and the excerpts no longer match.

## Maintenance notes

- `sweep_corpus._recall` and `real_corpus.recall_at_k` should stay semantically identical; if a third
  grader appears, factor a shared helper.
- If the `#BENCH`/`#SWEEP` SQL format ever changes, the parser regexes in `sweep_corpus.report` and
  `bench/live_report.py` must change in lockstep — the new tests will catch a drift.
- Reviewer: confirm Step 2 did not change recall on the committed `bench/results/sweep_manifest.json`
  (float64 corpus has no ties, so the manifest oracle should be unchanged).

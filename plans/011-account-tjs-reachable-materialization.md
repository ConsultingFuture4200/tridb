# Plan 011: Account for TJS reachable-set materialization in SM-1

> **Executor instructions**: Follow this plan step by step. Run every verification
> command and confirm the expected result before moving on. If a STOP condition
> occurs, stop and report instead of improvising. When done, update this plan's
> status row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat fb3f08b..HEAD -- bench/live_report.py tests/test_bench_live_report.py scripts/patches/tridb_tjs_operator.patch docs/benchmark_results_v0.1.0.md`
> If any in-scope file changed since this plan was written, compare the excerpts
> below with the live code before proceeding. A mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: correctness / perf / benchmark
- **Planned at**: commit `fb3f08b`, 2026-06-26

## Why this matters

TriDB's public SM-1 metric currently says the TJS plan holds only the bounded
top-k heap (`k`) as its peak intermediate. The TJS patch also precomputes and
caches the full reachable destination set for the source vertex at operator
Open. That set is a real in-process intermediate and can be larger than `k`, so
the benchmark and docs overstate intermediate-result reduction for high-degree
sources. This plan corrects the measurement and documentation first; it does
not redesign TJS.

## Current state

- `scripts/patches/tridb_tjs_operator.patch` contains the TJS implementation
  patch applied to the MSVBASE fork. It materializes a reachable set once:

  ```text
  scripts/patches/tridb_tjs_operator.patch:332
  graphReachableT(src) probes graph_store.neighbors(src) ... and returns the
  set of reachable destination ids. We resolve this ONCE at init (TJS Open)
  and cache it

  scripts/patches/tridb_tjs_operator.patch:348
  static std::unordered_set<int64> graphReachableT(int64 src){
      std::unordered_set<int64> reachable;
  ```

- `bench/live_report.py` currently records TriDB peak intermediate as exactly
  `k`, excluding the reachable set:

  ```python
  bench/live_report.py:231
  # Peak in-flight intermediate ... keeps ONLY the bounded top-k heap.
  bench/live_report.py:240
  peak = k
  ```

- The generated benchmark docs repeat the same claim:

  ```text
  docs/benchmark_results_v0.1.0.md:59
  never materializing the reachable/filtered set.
  docs/benchmark_results_v0.1.0.md:60
  TriDB holds a bounded top-k heap (5)
  ```

- Repo conventions: Python tests use `pytest`; lint uses `ruff`. Fast gates are
  `make test` and `make lint`. Engine verification is Docker/GX10-gated.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Fast tests | `make test` | `92 passed` or all current tests pass |
| Lint | `make lint` | `All checks passed` and format check clean |
| Focused tests | `pytest tests/test_bench_live_report.py -q` | all tests pass |
| Engine report regeneration | `make bench-live` | exits 0, updates `bench/results/*` and `docs/benchmark_results_v0.1.0.md` |

## Scope

**In scope**:
- `bench/live_report.py`
- `tests/test_bench_live_report.py`
- `docs/benchmark_results_v0.1.0.md`
- `bench/results/bench_live_metrics.json`
- `bench/results/report_live.html`
- `bench/results/bench_live_raw.txt`
- Optional: `scripts/patches/tridb_tjs_operator.patch` comments only, if needed to expose a count.

**Out of scope**:
- Rewriting TJS to stream the graph predicate.
- Changing the native graph store.
- Changing SM-2 or `term_cond`; that is plan 012.

## Git workflow

- Branch: `advisor/011-tjs-reachable-accounting`
- Commit message style: `fix(bench): account for tjs reachable-set materialization`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Make TriDB peak include reachable-set size

In `bench/live_report.py`, use the parsed oracle count `o["reached"]` as the
reachable-set size for the same pinned source. Set TriDB peak intermediate to at
least `max(k, reached)` when `reached` is present. If `reached` is missing, keep
a conservative fallback of `k` but make the comment explicit that this is only a
fallback for incomplete transcripts.

The target shape is:

```python
reachable_peak = o["reached"] if o.get("reached") is not None else k
peak = max(k, reachable_peak)
```

Update the nearby comment so it says TJS holds a bounded top-k heap plus the
precomputed reachable-id set.

**Verify**: `pytest tests/test_bench_live_report.py -q` initially may fail until
Step 2 updates the expected SM-1 behavior.

### Step 2: Add focused regression coverage

In `tests/test_bench_live_report.py`, update `test_build_report_live_smoke` so
the transcript's `#BENCH ORACLE_COUNTS ... reached=4` produces
`report.tridb_samples[0].peak_intermediate_rows == 4` when `k == 3`.

Add a second test for missing `ORACLE_COUNTS` proving the fallback remains `k`
for old/incomplete transcripts.

**Verify**: `pytest tests/test_bench_live_report.py -q` exits 0.

### Step 3: Regenerate live benchmark artifacts if the engine image exists

Run `make bench-live` only if `tridb/msvbase:dev` is already built and Docker is
available. This command is expected to update:

- `bench/results/bench_live_metrics.json`
- `bench/results/report_live.html`
- `bench/results/bench_live_raw.txt`
- `docs/benchmark_results_v0.1.0.md`

If the image is not available, do not fake outputs. Leave the code/tests change
and note in the final response that artifact regeneration is pending.

**Verify**: `make bench-live` exits 0, or the command fails immediately with the
existing "image not built" message and no benchmark artifacts are changed.

### Step 4: Update docs wording

In `docs/benchmark_results_v0.1.0.md`, replace claims that TriDB "never
materializes the reachable set" or holds only heap size `k`. The corrected
wording should say the current SRF TJS implementation precomputes one source's
reachable-id set at Open and keeps the bounded top-k heap while streaming the
ANN candidates.

Do not overclaim TR-1. Say explicitly that this is still in-process and avoids
cross-system transfer, but it is not a pure no-materialization graph predicate.

**Verify**: `rg -n "never materializing the reachable|bounded top-k heap \\(5\\)" docs/benchmark_results_v0.1.0.md bench/live_report.py` returns no stale claim.

## Test plan

- Unit coverage in `tests/test_bench_live_report.py` for:
  - reachable count included in TriDB peak.
  - missing reachable count falls back to `k`.
- Full fast suite: `make test`.
- Lint/format check: `make lint`.
- Engine report regeneration with `make bench-live` when the image is available.

## Done criteria

- [ ] `bench/live_report.py` includes reachable-set size in TriDB SM-1 peak.
- [ ] Tests cover both counted and fallback cases.
- [ ] Stale "only top-k heap" SM-1 claims are gone from current benchmark docs.
- [ ] `make test` and `make lint` pass.
- [ ] If `make bench-live` could run, regenerated artifacts are committed; if not, the final note says it was not run.
- [ ] `plans/README.md` row for plan 011 is updated.

## STOP conditions

- The live transcript no longer emits `#BENCH ORACLE_COUNTS` per query.
- Correct accounting requires changing the TJS C patch rather than the report layer.
- `make bench-live` produces a new SM-1 below target and the requested change was only accounting, not redesign.

## Maintenance notes

This plan corrects measurement honesty. A future performance plan can redesign
TJS so the graph predicate streams or probes incrementally instead of caching
all reachable ids, but that is a separate architecture change with SPI and TR-1
risks.

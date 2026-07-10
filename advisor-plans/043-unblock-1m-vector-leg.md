# Plan 043: Unblock 1M fused operating point (vector / HNSW leg)

> **Executor instructions**: Fork HNSW + planner work (patch chain). Author here; measure on GX10/Spark.
> Do **not** fabricate 1M latency numbers. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- scripts/patches docs/benchmark_wiki_fusion_v0.1.0.md bench/wiki_h2h.py bench/wiki_fusion.py`

## Status
- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: none for investigation; publication of 1M fusion requires this green
- **Category**: performance / direction (publication gate)
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Wiki fusion **speed is proven at N=200k** (11.5× hop-1). At **N=1M** the fused h2h did not execute:
`tjs_open` and even plain `ORDER BY embedding <-> q LIMIT 10` hang with **`examined=0`**. EXPLAIN shows
a blocking **Sort over ~1M rows** on top of `articles_hnsw` — the planner no longer trusts index order.
Graph load at 1M is healthy (38.99M edges ~35s via `gph_insert_edges`). Without this fix there is no
credible-scale fusion claim.

## Current state (measured, documented)

From `docs/benchmark_wiki_fusion_v0.1.0.md:40-84`:

- `tjs_open` @ 1M: examined=0 after 600s timeout
- Plain ANN LIMIT 10 also blocks; Sort over full corpus
- 0/2 fresh HNSW builds usable on blocked runs; publication gate requires ≥3/3 healthy
- Harness: `bench/wiki_h2h.py` `publication_gate()` hard-fails `examined==0` and unlucky single builds

Relevant patches (order in `scripts/lib/msvbase_patches.sh`):
- `tridb_hnsw_costestimate_no_orderby.patch`, `tridb_hnsw_scan_no_orderby.patch`
- `tridb_relaxed_order_executor_guard.patch`, `tridb_hnsw_am_entry_guards.patch`
- `tridb_tjs_open_workbound.patch`, `tridb_hnsw_scan_workbound.patch` (do **not** fix examined=0 hang)

Golden rules: TR-1 early termination must remain; no materialize-all “fix”.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patch chain | `bash scripts/ci_check_patches.sh` | exit 0 |
| Host tests | `make test && make lint` | exit 0 |
| Engine | `make graph-test` / image rebuild after patch | ALL PASS |
| Live 1M | GX10: fusion/h2h with `--n 1000000` | publication_gate PASS; examined>0 |

## Scope

**In scope:**
- Root-cause + fix so HNSW ordered scan / `tjs_open` vector drain emits candidates at N=1M without full Sort
- New or extended fork patch(es) under `scripts/patches/` + wire in `msvbase_patches.sh` + `verify_patches` sentinels
- Engine-side regression SQL if feasible (smaller N that still triggers Sort if reproducible)
- Doc update: fusion 1M section when unblocked (honest numbers only)
- Optional: multi-build health gate already in harness — keep/strengthen, do not weaken

**Out of scope:** fabricating latency; changing multi-store baseline; CSR-lite; graph SI; GPU query path.

## Git workflow
- Branch: `advisor/043-1m-vector-leg`
- Commits: `fix(hnsw): …` / `test(engine): …` / `docs(bench): …`

## Steps

### Step 1: Reproduce and capture EXPLAIN on engine

On engine image with a ≥100k or 1M corpus (or synthetic float8[] + hnsw):

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT id FROM articles ORDER BY embedding <-> $q LIMIT 10;
```

Record: Index Scan vs Seq Scan, presence of Sort, whether `amcanorderbyop` / cost estimate paths fire.

**Verify**: written notes under `docs/` or `bench/results/` as a short investigation addendum (not a win claim).

### Step 2: Root-cause the Sort injection

Likely classes (check which is true; do not assume):

1. Cost model prefers full scan + Sort at N=1M
2. Relaxed-order index fails `amcanorderbyop` / pathkeys so planner requires Sort
3. Iterator never returns rows (hang inside LoadIndex / beam) → examined stays 0
4. Opclass / `<->` not bound to `articles_hnsw`

Trace with `tridb_hnsw_*` patches + `relaxed_order` guard. Prefer minimal fix that restores
**LIMIT-aware early stop** without claiming strict total order if the AM is approximate.

**Verify**: at standin scale (e.g. 20k–100k) EXPLAIN shows no blocking Sort over full N for LIMIT k; query finishes with examined>0.

### Step 3: Patch + sentinels

Land fix as a new ordered patch (LAST or as required by chain). Register:

- `apply_tridb_fork_patches` entry
- `verify_patches` greps (unique strings)
- `ci_check_patches.sh` still exit 0

**Verify**: `bash scripts/ci_check_patches.sh` exit 0; `make test && make lint`.

### Step 4: Engine regression

Add or extend an SQL test that fails if Sort-over-full-corpus returns for LIMIT k on a corpus large
enough to hit the bug **if** that is reproducible on CI-sized data; otherwise document GX10-only gate
and keep `publication_gate` as the 1M gate.

**Verify**: `make graph-test` on engine image.

### Step 5: GX10 1M re-run (gated)

Rebuild image; load 1M slice; run `bench/wiki_fusion.py` / h2h with publication gates:

- median examined > 0
- ≥3/3 healthy fresh HNSW builds **or** document remaining build flake separately without quoting luck
- Only then emit 1M latency@recall

**Verify**: gate script exit 0; results JSON committed under `bench/results/` + doc update.

## Test plan
- Patch apply CI
- Engine suites non-regression
- Live publication_gate matrix (existing `wiki_h2h` tests if any host-side; extend pure unit tests for gate if missing — can share plan 050)

## Done criteria
- [ ] Root cause documented with EXPLAIN evidence
- [ ] Plain ANN LIMIT k and `tjs_open` at 1M produce examined>0 without statement timeout (on GX10)
- [ ] `ci_check_patches.sh` green; host `make test`/`lint` green
- [ ] No fabricated 1M numbers; fusion doc updated honestly
- [ ] Index DONE or BLOCKED with precise remaining wall

## STOP conditions
- Fix would require disabling TR-1 or materializing full ANN result — STOP
- Only one lucky HNSW build works — do not publish 1M fusion; fix build reproducibility first
- Drift: costestimate patches already fixed Sort on HEAD — re-measure before rewriting

## Maintenance notes
- Companion: plan 052 (HNSW cache invalidation) and PERF-08 GPU index load are **not** substitutes for this hang.
- Reviewer: reject any PR that weakens `publication_gate` to green-wash examined=0.

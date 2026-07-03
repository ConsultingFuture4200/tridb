# Plan 029: Hot-path performance batch — O(D) graph loads, SIMD distance in the filter-first drain, hash-join membership

> **Executor instructions**: Follow step by step; verify each step (each perf claim needs a
> before/after number, not vibes). On any STOP condition, stop and report. Update your row in
> `advisor-plans/README.md` when done (unless a reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- tools/ scripts/patches vendor/MSVBASE/src/tjs_operator.cpp`
> Plans 024/025 land first and change the same files — EXPECTED drift; re-read the live code at each
> excerpt site before editing. If plan 025 landed, the corpus emitters already changed shape (v1
> `gph_insert_edge` path) — apply Step 1's batching to THAT shape.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (touches the flagship filter-first body; answers must stay byte-identical)
- **Depends on**: advisor-plans/024-operator-arg-hardening.md (patch-chain tail ordering); rebase over 025 if it has landed
- **Category**: perf
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

Three measured-shape inefficiencies on the paths the headlines run: (1) the v0 `add_edge` upsert
(`nbrs = adjacency.nbrs || EXCLUDED.nbrs`) copies the whole immutable array per appended edge —
O(D²) bytes + WAL per degree-D vertex, paid by EVERY benchmark corpus load including the 1M
headline (24 hubs × fanout 2000 → 2000 array rewrites of up to 16KB each per hub). (2) The
filter-first drain ranks with a hand-rolled scalar `double` loop while the fork ships a
NEON/SIMD-accelerated L2 kernel measured 3.6-7.8× faster on the identical math — the flagship 4.7ms
path forfeits the project's own kernel. (3) The drain's membership predicate `(id) = ANY($1)` on
PG 13 has no hashed ScalarArrayOp: as a seq-scan filter it is O(rows×|set|), and plan choice is
fragile — exactly in the large-reachable-set regime FR-6 routes here.

## Current state

- `src/graph_store_ext/graph_store--0.1.0.sql:15-23` — `add_edge` upsert with array concat (the
  O(D²) mechanism). Callers emit one `SELECT graph_store.add_edge(s,d);` per edge:
  `tools/bench_sm2_corpus.py` (`build_sql`, "native adjacency graph: hub -> dst" section),
  `tools/bench_corpus.py`, `tools/sweep_corpus.py`.
- `vendor/MSVBASE/src/tjs_operator.cpp` `beginFilterFirstT` (from `tridb_tjs_filter_first.patch`,
  post-024 state): ranking loop
  ```c
  double* vals = (double*) ARR_DATA_PTR(arr);
  double acc = 0.0;
  for (int c = 0; c < dim; c++) { double diff = vals[c] - qvec[c]; acc += diff * diff; }
  ```
  and drain SQL `"select %s, (%s) from %s where (%s) = any($1) [and (%s)]"` with the reachable ids
  as one `int8[]` `$1`.
- The SIMD kernel: `scripts/patches/tridb_neon_l2_distance.patch` adds a NEON L2Sqr to hnswlib's
  `space_l2.h` (`__ARM_NEON`-gated; x86 uses hnswlib's SSE/AVX paths). It operates on `float*`;
  the drain has `double*` — the shared helper below must handle f64 (either an f64 SIMD variant or
  convert; MEASURE both, pick by numbers, keep exact ordering semantics).
- Engine-change workflow + patch-chain conventions: identical to plan 024 "Current state" (vendor
  edit → new last patch `tridb_ff_drain_perf.patch` → register + sentinel). The drain SQL text and
  kernel change ride ONE new patch.
- Perf harness precedent: `tools/neon_l2_bench.c` (kernel micro-bench), `test/tjs_filter_first_test.sql`
  (answers must not change), `docs/benchmark_sm2_1m_v0.2.0.md` (the 1M recipe if a headline re-check
  is wanted — OPTIONAL here, x86-scale evidence suffices).

## Commands you will need

Plan 024's table (incremental compile, x86build, graph-test, single-file test, make test/lint), plus:

| Purpose | Command | Expected |
|---|---|---|
| Load-path timing | `time bash scripts/graph_test.sh tridb/msvbase:dev test/<scratch load test>.sql` or a psql `\timing` block in the test | before/after numbers captured |
| Emitter unit tests | `.venv/bin/python -m pytest tests/ -q -k corpus` | pass |

## Scope

**In scope:** the 3 corpus emitters' edge-SQL sections; a batched-load path in
`src/graph_store_ext/graph_store--0.1.0.sql` (new `add_edges(src bigint, dsts bigint[])` or grouped
INSERT emission — choose emission-side grouping first, it needs NO extension change);
`vendor/MSVBASE/src/tjs_operator.cpp` drain (kernel + membership SQL) → new
`scripts/patches/tridb_ff_drain_perf.patch` + registration; a small shared-header change inside the
SAME patch if the kernel is factored (e.g. expose `tridb_l2sqr_f64` from a fork header);
`test/tjs_filter_first_test.sql` (answers-unchanged assertions already exist — extend only if the
>256 block from plan 027 isn't there yet); README row.

**Out of scope:** v1 native-AM load path internals (025 owns them); hnswlib's own kernels beyond
factoring a callable; the vector-first body; any FR-6 decision logic (plan 031).

## Git workflow
Branch `advisor/029-perf-batch`; `perf(...)` commits with the before/after numbers in the body; do NOT push.

## Steps

### Step 1: Emitter-side O(D) loads
Change the three emitters to group edges by source and emit ONE statement per vertex:
`INSERT INTO graph_store.adjacency (vid, nbrs) VALUES (s, ARRAY[d1,d2,...]) ON CONFLICT (vid) DO UPDATE SET nbrs = adjacency.nbrs || EXCLUDED.nbrs;`
(one concat per vertex per load, not per edge). If plan 025 landed, apply the same grouping to the
v1 emission (vertex materialization + batched `gph_insert_edge` calls stay per-edge C — group only
what the store supports; note the residual in the commit).
**Verify**: `make test` (emitter unit tests) green; a scratch 1-hub/2000-edge load in psql shows
wall-clock improvement (record before/after ms in the commit message); `bash scripts/graph_test.sh
tridb/msvbase:dev test/parse_canonical.sql` still ALL PASS (its 4-edge seeding must still work).

### Step 2: Kernel in the drain
Snapshot vendor file; factor a callable exact-L2 (f64) that uses SIMD where available (NEON on ARM,
SSE2/AVX on x86 — or measure that a `float`-converted call into the existing kernel preserves
ordering: STOP if any test answer changes) and call it from the drain loop. Keep the scalar loop as
the fallback `#else`.
**Verify**: incremental compile; `test/tjs_filter_first_test.sql` ALL PASS byte-identical answers;
micro-number: extend `tools/neon_l2_bench.c` (or a scratch variant) to time f64 scalar vs new path
at dim 128/768 — record the ratio.

### Step 3: Membership as a hash join
Change the drain SQL to `select %s, (%s) from %s t join unnest($1::int8[]) AS r(id) on (t.<idcol>) = r.id [where (%s)]`
so PG13 plans a hash/merge join instead of a per-row SAOP array scan. `EXPLAIN` the drain shape at
|set|=2000 in a scratch psql block and capture the plan (Hash Join expected).
**Verify**: all filter-first tests byte-identical answers incl. duplicates semantics (unnest join
can duplicate if the id set has dupes — the reachable set is a `std::unordered_set`, so it cannot;
assert that reasoning in a code comment); captured EXPLAIN in the commit body.

### Step 4: Patch generation + full validation
Generate `tridb_ff_drain_perf.patch` from the snapshot diff; register (after 024's patch; sentinel
on a load-bearing token e.g. the join-on-unnest SQL fragment); `bash scripts/x86build.sh --docker`;
`make graph-test`; `make test && make lint`.
**Verify**: all green; reverse-apply check clean.

## Test plan
Answer-invariance is the test (existing filter-first + canonical suites); perf evidence = recorded
before/after numbers per step. No new test files required beyond what 027 added.

## Done criteria
- [ ] Emitters grouped; before/after load numbers recorded in commit
- [ ] Drain uses the factored kernel + unnest join; EXPLAIN captured; answers byte-identical
- [ ] New patch registered + sentinel + reverse-apply clean; `make graph-test` green
- [ ] `make test && make lint` green; README row updated

## STOP conditions
- ANY answer change in ANY suite (ordering ties may differ only if a test never pinned them — if a
  test fails on order, report; do not re-pin the test).
- The f32-conversion route changes any ranking at dim 8/128 test scale — use the f64 SIMD variant or
  keep scalar and report the measured cost of keeping it.
- Plan-chain reverse-apply breaks (025 landed a conflicting drain change) — rebase the vendor edit
  onto the live state, regenerate, note it.

## Maintenance notes
When 025's Stage B replaces the v0 loader entirely, Step 1's grouped v0 path becomes legacy — fine,
it still serves any v0-comparative bench. The unnest-join drain shape is also the right substrate if
plan 031 later feeds a cardinality hint. Reviewer focus: exact-ordering preservation in Step 2 and
the no-dupes argument in Step 3.

---

## Status addendum 2026-07-03 — DEFERRED (post-025 re-scope)

Not executed this batch. Re-scoped after plan 025 (v0→v1 native AM rewire) merged:

- **Step 1 (O(D) emitter loads) is now largely MOOT.** The hot path no longer uses the v0
  `add_edge` array-concat upsert — v1 routes edges through the native `gph_insert_edge`
  (page append, not O(D²) array rewrite). The v0 emitter grouping would only help any
  remaining v0-comparative bench, which is low value.
- **Steps 2 (SIMD drain distance) + 3 (hash-join membership) remain valid** — they live in
  the store-independent filter-first `tjs()` body — but each requires a **full engine image
  rebuild** (a new fork patch to `tjs_operator.cpp`) plus a **1M re-measurement**, which risks
  the just-verified 025 filter-first path (recall 1.0 / 6.66 ms) for a P2 latency win on a path
  already well inside its SM-2 margin (13.4×).

**Recommendation:** execute Steps 2+3 as a focused engine-patch cycle in a future session when
an engine rebuild is already planned (e.g. alongside DEV-1259 Phase B or the 031 boundary sweep),
so the rebuild cost is shared. Re-verify the 025 filter-first answers are byte-identical after.
Deferred deliberately, not skipped — the plan body above is ready to execute as written (apply
Step 1's batching only to any surviving v0 path).

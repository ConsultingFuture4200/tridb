# MSVBASE fork findings (from building the real composition)

Constraints discovered by actually running queries against the fork. Each one
directly shapes the TJS operator (DEV-1169) and the relaxed-monotonicity top-k
operator (DEV-1168). Surfaced 2026-06-23.

## 1. FROM-clause SRFs are materialized — they cannot early-terminate
PostgreSQL runs a set-returning function in a FROM clause to exhaustion via
`ExecMakeTableFunctionResult` (builds a tuplestore) before any LIMIT applies.
Only the target-list / `ProjectSet` form is pulled lazily.
**Implication:** the production graph traversal that composes into TJS with early
termination must be a **custom-scan node or index-AM `amgettuple`** path — a
userland SRF in FROM would block. (The v0 `graph_store.neighbors` SRF is fine as
a per-source LATERAL leg, because the *outer* driver early-terminates; see below.)

## 2. Scalar vector distance returned 0 — FIXED (plan 005)
**Status: FIXED 2026-06-24** by `scripts/patches/l2_distance_scalar.patch`.

**Was observed:** `l2_distance(float8[], float8[])` and the `<->` operator returned a
**constant 0 for every input** when evaluated as a scalar (outside an HNSW index scan) —
for both fractional and integer vectors, with explicit `ARRAY[...]::float8[]` casts.

**Root cause:** the static `hnswlib::L2Space L2Distance(0)` in `src/operator.cpp` was
constructed with **dim=0**, so `get_dist_func()` selected `L2SqrSIMD16Ext`, which sums only
full 16-float blocks (`qty16 = qty >> 4`). For any dimension `< 16` (or not a multiple of 16)
the residual is dropped and the result is 0. The index path was never affected — it builds its
own correctly-dimensioned `L2Space` in the HNSW scan.

**Fix:** `l2_distance` now computes the Euclidean distance directly from the two arrays
(`sqrt(Σ (a_i − b_i)²)`), independent of any index-scan state. Verified by
`test/fork_distance_probe.sql` (now a regression test: 4 distinct distances {10,9,5,0} to
`[10,0,0]`, plus correct re-rank order). Only the scalar `l2_distance` was touched; the HNSW
index path and `range_l2_distance`/inner-product operators are unchanged.

**Downstream unblock:**
- Exact top-k **can** now be checked by a SQL over-fetch + scalar re-rank, and exact ground
  truth computed directly — useful for correctness tests across the project.
- DEV-1168's relaxed-monotonicity finalize no longer *must* read index-internal distances to
  get a usable scalar; a scalar re-rank is now a viable ground-truth oracle (the production
  iterator design choice is unchanged — see finding #1).

## What IS demonstrable today (and is, in `test/trimodal_early_term.sql`)
The early-terminating tri-modal pipeline, driven by the ANN index scan:
```
Limit(5)
  -> NestLoop -> NestLoop
       -> Index Scan using entities_hnsw (VECTOR; actual rows=8 of 2000)
       -> Function Scan on graph_store.neighbors (GRAPH; per source)
       -> Index Scan on entities d + ts filter (RELATIONAL)
```
Verified: all three legs engage, the relational filter is load-bearing, and the
ANN scan examines 8 of 2000 sources (0.4% << SM-3's 25%). Exact top-k *ranking*
is deferred to DEV-1168 per finding #2.

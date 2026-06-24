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

## 2. No working scalar vector distance — distances live only in the index scan
**Observed:** `l2_distance(float8[], float8[])` and the `<->` operator return a
**constant (0) for every input** when evaluated as a scalar (outside an HNSW index
scan) — seen for both fractional and integer vectors, with and without an index,
and with explicit `ARRAY[...]::float8[]` casts (ruling out literal coercion). Real,
correctly-ordered distances are produced ONLY inside the HNSW index scan.
Reproducible probe: `test/fork_distance_probe.sql` (asserts the index path is
correct and reports the scalar behavior, so it stays informative if the fork is
fixed). Treat as a confirmed-by-probe fork bug pending a root-cause patch in
`l2_distance`'s C implementation.
**Implications:**
- Exact top-k **cannot** be done by a SQL over-fetch + re-rank, and exact ground
  truth **cannot** be computed by a seq-scan — there is no usable distance scalar.
- The relaxed-monotonicity finalize (DEV-1168) must be a **C operator that reads
  the index's internal distances**; it cannot lean on a scalar `<->`.
- Fixing `l2_distance` to compute a real scalar is a candidate fork patch that
  would unlock SQL-level re-rank and exact-correctness tests. Worth scoping.

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

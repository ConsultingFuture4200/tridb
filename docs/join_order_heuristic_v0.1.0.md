# Cross-Modal Join-Order Heuristic (FR-6)

**Version:** 0.1.0
**Date:** 2026-06-23
**Issue:** DEV-1170
**Status:** Design complete; implementation deferred to GX10 (`src/planner/join_order.c`)

---

## 1. What This Is ("The 20%")

The TriDB v1 planner makes exactly one planning decision: which leg of the
canonical tri-modal query executes first — the relational filter or the vector
ANN scan. This single heuristic is responsible for the bulk of the SM-1
intermediate-result reduction (≥5×). It is called "the 20%" internally because
a cheap, threshold-based selectivity test captures most of the gain that a full
cost-based optimizer would deliver, at a fraction of the complexity.

Everything else — cardinality estimation, cost models for the graph traversal
leg, adaptive re-planning — is explicitly deferred to v2.

---

## 2. The Decision: Filter-First vs. Vector-First

The canonical v1 query has three legs:

1. **Relational leg** — `WHERE timestamp IN :selected_time_range` against a
   PostgreSQL B-tree index.
2. **Graph leg** — `MATCH (src)-[:related_to]->(dst)` traversal over the native
   adjacency-list access method.
3. **Vector leg** — `ORDER BY src_embedding <-> :question_embedding LIMIT k`
   ANN scan over the HNSW index.

The graph leg fans out from whatever seed set enters it; its cost scales with
`seed_count * avg_out_degree`. The planner controls the seed set size by
choosing which leg runs first and feeds seeds into the graph.

Two orderings are available:

| Order | First leg | Seeds into graph | Graph output | Second leg |
|---|---|---|---|---|
| **filter_first** | Relational filter | `rel_filter_matches` rows | `rel_filter_matches * avg_out_degree` candidates | Vector ANN over candidates |
| **vector_first** | Vector ANN (top-k over-fetch) | `k * overfetch_factor` rows (~50×k) | proportionally larger | Relational filter over ANN results |

When the relational filter is highly selective, `filter_first` produces a small
seed set and the graph traversal stays cheap. When the filter is weak (returns
most of the table), it contributes nothing early; vector ANN should run first so
the graph only traverses from the k most similar candidates.

---

## 3. Selectivity Estimate

Selectivity is defined as the fraction of the table that the relational filter
is expected to return:

```
selectivity = rel_filter_matches / table_size
```

`rel_filter_matches` is obtained from the PostgreSQL statistics catalog
(`pg_statistic`) via the standard selectivity estimator path — the same
mechanism used by the Postgres planner for ordinary B-tree predicates. No new
statistics infrastructure is required for v1.

`avg_out_degree` is the mean number of edges per node in the graph store,
stored as a summary statistic on the access method's metapage (one float per
label type, updated at `ANALYZE` time).

These two values together fully specify the heuristic. Neither requires
histogram sampling beyond what Postgres already collects.

Reference: `relational_selectivity(stats: LegStats) -> float` in
`src/planner/join_order_ref.py`.

---

## 4. Threshold Rule

```
if selectivity <= 0.10:
    order = "filter_first"
else:
    order = "vector_first"
```

The threshold is **10%** (hardcoded for v1, configurable in v2 via a GUC).

**Rationale for 10%:** At 10% selectivity the relational filter passes 10% of
the table. The graph traversal then fans out by `avg_out_degree` from that 10%.
If `avg_out_degree` is typical (2–8 for knowledge-graph workloads), the
candidate set entering the vector leg is still well below the full corpus,
preserving SM-3 (<25% of corpus examined for k=5). At selectivities above 10%,
the benefit of filtering first shrinks faster than the cost of the extra
predicate evaluation over a large ANN over-fetch — vector-first dominates.

The 10% boundary is not derived from a curve-fit; it is a conservative
threshold borrowed from the Chimera paper's observation that typical temporal
filters on RAG corpora fall either below 5% (narrow time windows) or above 50%
(broad or absent filters). The gap between those clusters makes the exact
threshold value largely irrelevant for real workloads, which is precisely why a
full cost model is not worth building for v1.

Reference: `choose_order(stats: LegStats, threshold: float = 0.10) -> str` in
`src/planner/join_order_ref.py`.

---

## 5. Intermediate-Result Estimate

The reference model also estimates the peak intermediate result size for each
ordering, used in the `explain()` output and as a proxy for the SM-1 metric:

**filter_first:**
```
intermediate_rows = min(rel_filter_matches, vector_topk)
```
After the relational filter, at most `rel_filter_matches` rows remain. The
vector leg then limits to `k`, so the peak is the smaller of the two. The
graph traversal is treated as a pass-through in this reference model; the C
counterpart (`tridb_estimate_intermediate`) will incorporate the
`avg_out_degree` fan-out into the estimate.

**vector_first:**
```
intermediate_rows = vector_topk * 50
```
ANN over-fetches by approximately 50× `k` to maintain recall under HNSW's
approximate search guarantee. This is the dominant intermediate cost of the
vector-first path and the quantity that filter-first avoids when selectivity is
low.

The 50× over-fetch factor is a conservative estimate for HNSW with `ef_search`
tuned for ≥99% recall at k=5 [UNVERIFIED — validate against the MSVBASE HNSW
implementation on the GX10 build with the actual `ef_search` parameter used in
production].

Reference: `estimated_intermediate_rows(stats: LegStats, order: str) -> int`
in `src/planner/join_order_ref.py`.

---

## 6. Why v1 Is NOT a Full Cost-Based Optimizer

A cost-based optimizer (CBO) would enumerate join orders, estimate costs using
cardinality models for all three legs, and pick the minimum-cost plan. TriDB v1
deliberately avoids this for three reasons:

1. **The cardinality problem is unsolved for ANN.** HNSW's output distribution
   depends on the embedding space geometry, which cannot be summarized by
   standard histogram statistics. A cost model for the vector leg would require
   new statistics primitives that are themselves research contributions.

2. **The workload is narrow.** v1 targets a single canonical query template.
   There is no join-order search problem — the only choice is which of two legs
   executes first. A threshold heuristic captures >90% of the optimizer's value
   at ~1% of the implementation cost.

3. **Premature optimization risk.** Implementing a CBO before the physical
   operators exist on real hardware would produce a cost model calibrated against
   synthetic benchmarks. The v2 CBO should be calibrated against real GX10
   measurements from the v1 system.

Cost-based optimization is explicitly tracked as a v2 item in the spec (§2).

---

## 7. Reference Model

`src/planner/join_order_ref.py` is the authoritative reference implementation.
It is pure Python, runs without a database, and is the contract that
`src/planner/join_order.c` must match.

Key functions:

| Python function | C counterpart (GX10) | Purpose |
|---|---|---|
| `relational_selectivity(stats)` | `tridb_rel_selectivity()` | Compute selectivity from catalog stats |
| `choose_order(stats, threshold)` | `tridb_choose_join_order()` | Return `FILTER_FIRST` or `VECTOR_FIRST` enum |
| `estimated_intermediate_rows(stats, order)` | `tridb_estimate_intermediate()` | Intermediate-result size estimate for EXPLAIN |
| `explain(stats, threshold)` | — | Debug/EXPLAIN output; no direct C equivalent |

The `LegStats` dataclass maps to a C struct:

```c
typedef struct LegStats {
    int64  rel_filter_matches;   /* from pg_statistic / restriction selectivity */
    int64  table_size;           /* from pg_class.reltuples */
    float8 avg_out_degree;       /* from graph access method metapage */
    int32  vector_topk;          /* from LIMIT clause */
} LegStats;
```

The Python `threshold=0.10` default maps to a GUC
`tridb.join_order_selectivity_threshold` (float8, default 0.10, range
[0.0, 1.0]), loaded at planner invocation time.

The C implementation lives in the standard Postgres planner hook path:
`src/planner/join_order.c` registers a `planner_hook` that intercepts
`PlannedStmt` nodes whose `rtable` contains all three store types, calls
`tridb_choose_join_order()`, and rewrites the top-level `Plan` node order
before returning to the executor.

---

## 8. FR-6 Acceptance Test

The acceptance criterion for FR-6 is:

> Given two corpora with inverted selectivity profiles, the planner must choose
> opposite orderings.

**Corpus A — high selectivity (filter-first expected):**
- `table_size = 10_000`
- `rel_filter_matches = 50` (0.5% selectivity, well below 10% threshold)
- `avg_out_degree = 5.0`
- `vector_topk = 5`
- Expected: `choose_order(stats) == "filter_first"`
- Peak intermediate: `min(50, 5) = 5` rows (reference model; C impl will add graph fan-out)

**Corpus B — low selectivity (vector-first expected):**
- `table_size = 10_000`
- `rel_filter_matches = 8_000` (80% selectivity, well above 10% threshold)
- `avg_out_degree = 5.0`
- `vector_topk = 5`
- Expected: `choose_order(stats) == "vector_first"`
- Peak intermediate: `max(5 * 50, 5) = 250` rows

The test must assert both the ordering choice and that the filter-first
intermediate count is materially smaller than the vector-first intermediate
count when selectivity is low — directly verifying SM-1 (≥5× reduction).

A reference test using these corpora is located at
`src/planner/tests/test_join_order_ref.py` (to be authored as part of DEV-1170).

---

## 9. Invariant Compliance

This heuristic does not introduce any blocking operators. Both orderings are
composed of Volcano iterators:

- Relational filter: standard Postgres `Filter` node (Open/Next/Close).
- Graph traversal: `GraphScanIter` (Open/Next/Close, defined in
  `src/graph_store/`).
- Vector ANN: HNSW relaxed-monotonicity iterator (Open/Next/Close, defined in
  MSVBASE, surfaced by DEV-1168).

Early termination from the top-level `LIMIT 5` propagates down through all
three legs regardless of ordering. TR-1 is preserved.

---

## 10. Cross-References

- `src/planner/join_order_ref.py` — reference model (read before editing this doc)
- `docs/sqlpgq_logical_plan_v0.1.0.md` — where the TJS operator sits in the
  logical plan; join-order heuristic feeds into TJS construction
- `docs/graph_store_layout_v0.1.0.md` — `avg_out_degree` storage on metapage
- `spec/tridb_spec_v0.1.0.md` §2, §4.2, §6 (FR-6), §7 (SM-1, SM-3)
- ADR-0002 (graph store layout) — explains why the adjacency-list access method
  stores `avg_out_degree` rather than computing it at query time

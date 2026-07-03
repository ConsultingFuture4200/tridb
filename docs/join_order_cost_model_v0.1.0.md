# FR-6 cost-based join-order decision v0.1.0 (advisor plan 031)

> **Status:** decision core + GUCs + SQL surface LANDED and unit-verified; the lowering wire-up
> and the empirical boundary-sweep validation are DEFERRED (below). Default mode is `threshold`,
> so this is ZERO behavior change until deliberately enabled. Frozen core (`join_order.c`) untouched.
> **Motivated by:** `docs/landscape_review_v0.1.0.md` F4 — the frozen threshold is blind to the
> graph leg and misprices the landed DEV-1290 filter-first body.

## The problem the threshold has

The FROZEN decision (`tridb_choose_join_order`) picks `filter_first` iff **relational** selectivity
(`rel_matches / table_size`) ≤ a GUC threshold (default 0.10). It never sees the graph leg. Two
failure modes follow:

1. **Broad relational filter + tiny reachable set → wrong.** The 1M GX10 point: a ts window passing
   ~60% (`rel_sel ≈ 0.6`) but only 2000 of 1M rows reachable from `src`. The joint predicate is
   ~0.12% selective and filter-first is 36× faster — yet `0.6 > 0.10`, so the threshold picks
   `vector_first`. (The v0.2.0/v0.3.0 headline runs *forced* filter-first to sidestep this.)
2. **Selective relational filter + mega-hub `src` → wrong the other way.** A narrow ts window over a
   `src` reachable to most of the corpus would pick `filter_first` and drain an enormous set.

Both are the same blind spot: the decision must price the **filter-first drain size** (which is a
graph quantity, `deg(src) · rel_sel`) against the **vector-first examined stream**.

## The model

Two physical bodies, priced per row/candidate (both pay the same `dim`-distance cost, which cancels):

- **filter-first** drains `reachable(src) ∩ filter ≈ deg · rel_sel` rows, each an exact `dim`-distance.
- **vector-first** examines `~ k / joint_sel` candidates to fill the top-k, each a `dim`-distance
  **plus** an HNSW step + graph-membership probe — costlier per candidate by a ratio `R = a_vf/a_ff`.
  `joint_sel = rel_sel · (deg / table_size)`; `examined` is clamped to `table_size` (can't examine
  more than the corpus; also the `joint_sel → 0` case).

**Decision:** `filter_first` iff `drain < R · examined`.

`R` is the single empirical constant — GUC `tridb.join_order_cost_ratio`, default **4.0**, calibrated
from the 1M GX10 point (vector-first ≈ 17 µs/candidate ÷ filter-first ≈ 3.9 µs/drained-row ≈ 4.4).
`table_size ≤ 0` or `deg ≤ 0` (no graph leg / unknown) → `vector_first`, the same safe default the
frozen core uses.

## Calibration check (all reproduced in `test/join_order_cost_test.sql`)

| Point | deg | rel_sel | N | k | drain | examined | cost decision | threshold decision |
|---|---:|---:|---:|---:|---:|---:|---|---|
| 1M GX10 | 2000 | 0.6 | 1e6 | 5 | 1200 | 4167 | **filter_first** ✓ | vector_first ✗ (F4 bug) |
| mega-hub | 500000 | 0.6 | 1e6 | 5 | 300000 | 17 | **vector_first** ✓ | vector_first |
| 2k selective | 4 | 0.01 | 2000 | 1 | 0.04 | 2000 (clamped) | **filter_first** ✓ | filter_first |
| 2k broad | 4 | 0.8 | 2000 | 2 | 3.2 | 1250 | **filter_first** ✓* | vector_first |

\* On the 2k broad case the cost model *diverges* from threshold-mode and is correct to: `deg = 4`
makes the drain trivial regardless of window breadth. The threshold, blind to `deg`, over-picks
vector-first there. This divergence is the feature.

## Scope shipped vs deferred

**Landed (this doc):** `join_order_cost.c` (the cost function), GUCs `tridb.join_order_mode`
(`threshold`|`cost`, default `threshold`) + `tridb.join_order_cost_ratio` (default 4.0), the
`tridb_choose_join_order_cost(...)` SQL surface, and `test/join_order_cost_test.sql`. This mirrors how
the frozen core itself shipped (`join_order.c`: "decision core + GUC + SQL surface; the hook
integration is DEFERRED").

**Deferred (the binding + the empirical proof), and why:**

1. **Lowering wire-up.** Making the lowering call the cost path when `mode = 'cost'` needs a cheap
   `deg(src)` at decision time. v1 exposes only the store-wide `gph_edge_count()`, not a per-vertex
   out-degree, so `deg(src)` would be a `SELECT count(*) FROM gph_neighbors_ext(src)` — an O(deg)
   probe (bounded, single-hop, but real). The right form is a per-vertex degree accessor on the v1
   metapage/vertex record; that is graph-store C, tracked with ADR-0013's rider work. Until then the
   decision core is callable but not auto-bound.
2. **Empirical boundary sweep.** `R = 4.0` is calibrated from ONE operating point. Before the default
   flips from `threshold` to `cost`, a sweep across the crossover (fanout × window, both bodies forced
   via 8-arg `tjs()`, measured vs predicted) must show cost-mode's regret ≤ threshold-mode's at the
   phase boundary (the ~290× plan-regret concentration the literature warns of). That needs an engine
   run and is the gating evidence for the default flip.

**Do NOT flip `tridb.join_order_mode = cost` as the default** until both land. The value shipped now
is the correct, tested, graph-leg-aware decision function + the knobs — the F4 blind spot is fixable
the moment the accessor + sweep are done.

## v2 (explicitly out of scope)

In-filter traversal as a *third* physical body (predicate bitmaps, not per-node qual re-eval),
adaptive mid-query switching, and learned selectivities — all deferred (landscape review C4).

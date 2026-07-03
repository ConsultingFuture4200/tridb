# ADR-0011: Wiring the join-order decision into TJS — pass the order as a parameter, not a CustomScan rewrite

**Status:** Accepted — ALL stages (0-4) landed; Stage 3/4 (filter-first body + binding, DEV-1290)
implemented 2026-07-02 as `tridb_tjs_filter_first.patch`, x86-validated; GX10 validation + the
three-way 1M rerun are the DEV-1290 close-out evidence. See Addenda 2026-07-01 / 2026-07-02.
**Issue:** DEV-1285 (FR-6 — make the join-order decision actually change execution)
**Related:** DEV-1170 / `docs/join_order_heuristic_v0.1.0.md` (FROZEN decision core, shipped),
ADR-0007 / DEV-1169 (TJS operator — SRF now, CustomScan later), ADR-0006 (relaxed-monotonicity
vector iterator), ADR-0002 (adjacency-list graph store layout), CLAUDE.md golden rules 1 (TR-1) & 3.

> **GX10-gated.** No C in this ADR's scope is built or run on the x86 standin. The decision core
> (`join_order.c`) is shipped and GX10-green; everything DEV-1285 adds on top — the `LegStats`
> catalog builder (`join_order_legstats.c`, drafted here as UNBUILT-HERE) and any change to how
> `tjs()` is driven — compiles only inside the MSVBASE fork on the GX10. This ADR does NOT claim any
> of it builds or passes. The Python reference (`join_order_ref.py`) remains the executable spec.

## Context

DEV-1170 shipped the FROZEN decision core: `tridb_rel_selectivity` / `tridb_choose_join_order` /
`tridb_estimate_intermediate` + the `tridb.join_order_selectivity_threshold` GUC, bit-identical to
the Python reference and green on the GX10 (`test/join_order_test.sql`). What it did **not** do is
make that decision change a single byte of execution. `join_order_heuristic_v0.1.0.md` §10.5 sketched
the integration as a `planner_hook` that "sets the TJS node's driving-child slot" — but that sketch
assumed TJS is a plan node with a driving child to rewrite. **It is not.**

### The core problem: TJS is an SRF, and it is hardwired vector-first

Per ADR-0007, `tjs(...)` ships as a C **set-returning function**, not a CustomScan plan node. There
is no `Plan` tree for a `planner_hook` to intercept and no "driving child slot" to flip. More
decisively, the operator's *internal shape* already commits to one order (`tjs_operator.cpp`, via
`scripts/patches/tridb_tjs_operator.patch`):

- The **vector leg is the SOLE ordered stream** — a child HNSW `IndexScan` built via SPI, ranked by
  `xs_orderbyvals[0]`. That is the only authoritative distance (ADR-0006: scalar `<->` returns 0
  outside an index scan), so re-ranking in any other order is impossible by construction.
- The **relational filter is pushed into that index scan's `WHERE`** (`select ... where <filter>
  order by <orderby>`), so it is already a predicate on the vector stream, not a separate first leg.
- The **graph leg is a precomputed reachability predicate** resolved once at Open.

In other words: **`tjs()` IS the vector-first plan.** It has no filter-first execution path at all.
"Choosing filter_first" is not a slot to flip inside today's operator — it is a *different physical
strategy* (drain the selective relational predicate first, seed the graph from that small set, and
probe the vector index per seed) that the operator does not implement. §10.5's "rewrite the driving
child" does not map onto an SRF that has exactly one child and one order.

This is why DEV-1285 is flagged as "a real operator-architecture change with SPI / TR-1 risk, not a
wiring task." The decision core is done; making it *bind* requires giving TJS a second physical path.

## Decision

Adopt **Option B — pass the chosen order into `tjs()` as a parameter; the operator owns both
physical paths internally** — over Option A (CustomScan + `set_join_pathlist_hook`). Stage it so the
risky second-path work is isolated and the wiring is observable before it is trusted.

This ADR lands only the SAFE, additive pieces (the `LegStats` catalog builder + this design); the
operator's filter-first path is GX10-gated follow-on work, scoped below but not started.

### The two options the issue names

**Option A — CustomScan form + planner hook.**
Give TJS a CustomScan node (the "later" half of ADR-0007's "SRF now, CustomScan later"), then a
`set_join_pathlist_hook` (or `planner_hook`) builds `LegStats` and selects the driving leg by
choosing among candidate paths.

**Option B — order-as-parameter, operator-internal drive.**
Add a `join_order` argument (or reuse the GUC + a `LegStats` computed at the call site) to `tjs()`.
The DEV-1167 lowering computes `LegStats` from the catalog, calls `tridb_choose_join_order`, and
passes `'filter_first'` / `'vector_first'` into `tjs()`. The operator branches internally: the
existing vector-first body, or a new filter-first body (relational predicate drained first to seed
the graph and bound the vector probes).

### Comparison

| Axis | Option A (CustomScan + hook) | Option B (order-as-parameter) |
|---|---|---|
| **TR-1 risk** | High. CustomScan must re-implement Open/Next/Close + early termination as a node; the validated `execFagins`/`consecutive_drops` bound lives in the SRF body and would have to be lifted into the node API correctly. Two ways to get TR-1 wrong (node lifecycle + the new path). | Lower. The proven vector-first body and its `consecutive_drops` stop are **untouched**. Only the new filter-first body must be shown to preserve TR-1, and it reuses the same global top-k PQ + graph iterator's own early termination. One new path to audit, in isolation. |
| **Code surface** | Large. New CustomScan node, path generation, `set_join_pathlist_hook` registration, cost stubs to make the planner pick the custom path, plus the second physical path anyway. Couples to the unfinished SQL/PGQ parser (ADR-0007's stated reason to defer CustomScan). | Small-to-medium. One new SRF arg + one new branch in `tjs()`. No planner-node machinery, no parser coupling. The catalog→`LegStats`→decision logic lives in plain C (this ADR's `join_order_legstats.c`) called from the lowering, reusing the FROZEN functions verbatim. |
| **EXPLAIN visibility** | Native: a CustomScan shows up in `EXPLAIN` with its own node label, so "selected driver" is visible for free in the plan tree. | The SRF is opaque to `EXPLAIN` (it is one function-scan row). Visibility must be added explicitly: emit the chosen order via a companion introspection function (e.g. extend `tjs_candidates_examined()` with `tjs_last_join_order()`), and/or surface it through the lowering's debug output. DEV-1285's "EXPLAIN shows selected driver" criterion is met by asserting on that companion, not on a plan-node label. |
| **Effort / risk-adjusted** | Higher effort, higher blast radius, pulls in the deferred parser. Buys clean EXPLAIN but at the cost of re-deriving a TR-1-correct node. | Lower effort, contained blast radius, no parser dependency. Pays a small tax to make the decision observable. Matches ADR-0007's explicit "CustomScan upgrade is cheap *later*; do not do it for v1." |

### Recommendation: Option B

Option B is the right v1 choice. It keeps the validated vector-first path and its early-termination
bound untouched (the single biggest TR-1 risk in this whole task), avoids the CustomScan/parser
coupling ADR-0007 deliberately deferred, and isolates the genuinely hard part — a *new* filter-first
physical path — behind one branch we can build and prove on the GX10 in isolation. The only thing
Option A gives that B does not is free EXPLAIN visibility, and that is recoverable cheaply with a
companion introspection function. Option A is the v2 form once the SQL/PGQ parser lands and a
CustomScan is justified for reasons beyond join order.

The EXPLAIN-visibility cost is real and must not be hand-waved: DEV-1285's done-criterion is
"EXPLAIN shows selected driver." Under B that means shipping `tjs_last_join_order()` (or equivalent)
and asserting on it — see the Test Plan.

## Populating LegStats from the catalog

`LegStats` (FROZEN, `join_order_heuristic_v0.1.0.md` §10.1) has four fields. Sources:

| Field | Source | Status |
|---|---|---|
| `table_size` | `pg_class.reltuples` for the relational+vector table (the `tjs()` `table_name`, resolved via `RangeVarGetRelid`). `reltuples` is a float estimate maintained by `ANALYZE`/autovacuum; cast to `int64`. If `relpages == 0` (never analyzed) `reltuples` is 0 → selectivity falls to the `table_size == 0 → 1.0` FROZEN branch (safe vector-first default). | **Available.** Standard catalog. |
| `rel_filter_matches` | `clauselist_selectivity(root, filter_clauses, 0, JOIN_INNER, NULL)` × `reltuples`, the standard restriction-selectivity estimator — the same path the Postgres planner uses for ordinary B-tree predicates (matches `join_order_heuristic_v0.1.0.md` §3). Computed from the lowered `WHERE` clause of the canonical query. | **Available**, but needs a `PlannerInfo *root` (or the simpler single-clause `restriction_selectivity`/`scalarltsel` path) at the call site. The draft helper takes the estimated selectivity as an input so the estimator wiring is a separate, explicit step (see "Drafted here"). |
| `avg_out_degree` | The graph store's per-label mean out-degree. `join_order_heuristic_v0.1.0.md` §3 and §10.1 specify this lives "on the access method's metapage, updated at ANALYZE time." | **MISSING — must be added. See gap below.** Not an input to the v1 driver decision (FROZEN §10.1: `avg_out_degree` is carried for `tridb_estimate_intermediate`'s EXPLAIN fan-out only), so its absence does NOT block the FR-6 ordering decision — but it DOES block a faithful filter-first intermediate estimate and the "graph fan-out in EXPLAIN" the C `tridb_estimate_intermediate` was reserved for. |
| `vector_topk` | The `k` argument already passed to `tjs()`. | **Available.** Direct. |

### Gap: the graph metapage does NOT store avg_out_degree (must be added)

I checked the graph store (`src/graph_store/gph_page.h`, `graph_am.c`, `graphstore.h`,
`docs/graph_store_layout_v0.1.0.md`). The summary statistic the heuristic doc assumes **does not
exist**:

- The metapage payload `GphMeta` (`gph_page.h`) carries `gm_vertex_count` but **no store-wide edge
  count** and **no `avg_out_degree`**.
- `gph_edge_count` exists only **per adjacency page** (count of `EdgeSlot`s on that one page,
  `graph_store_layout_v0.1.0.md` §2.2) — it is not aggregated anywhere.
- There is **no `amanalyze` hook** in `graph_am.c`; nothing updates a degree statistic at `ANALYZE`
  time. `gm_vertex_count` is incremented on vertex insert; edges are appended with no running total.

So `avg_out_degree` cannot be read today. To honor §3/§10.1 faithfully, the graph AM must gain:

1. A store-wide edge counter on the metapage — add `uint64 gm_edge_count` to `GphMeta`, incremented
   in `gph_insert_edge` alongside the existing append (it already touches the metapage region). This
   reuses `gm_reserved`/keeps the struct in the existing page; it is a graph-store change, **out of
   scope for this ADR** and owned by the graph-store track (a DEV-116x follow-on), flagged here.
2. `avg_out_degree = gm_edge_count / NULLIF(gm_vertex_count, 0)`, computed on read (no need to store
   the float — derive it from the two counts at `LegStats`-build time).

**Until that lands**, `join_order_legstats.c` sets `avg_out_degree = 0.0` and documents it as a known
placeholder. This is honest and safe for v1 because, per the FROZEN contract, `avg_out_degree` is NOT
an input to `tridb_choose_join_order` — the FR-6 ordering decision is fully determined by
`rel_filter_matches`, `table_size`, and the threshold. Only the *intermediate-row EXPLAIN estimate*
degrades (it omits graph fan-out, exactly as the simplified Python reference already does, §5).

### Drafted here (SAFE, additive, GX10-gated)

`src/planner/join_order_legstats.c` (+ `join_order_legstats.h`): a standalone helper
`tridb_build_legstats(Relation rel, float8 est_filter_selectivity, int32 vector_topk, LegStats *out)`
that reads `reltuples` from the relation's `pg_class` cache entry, multiplies by the caller-supplied
restriction selectivity to get `rel_filter_matches`, sets `vector_topk` from `k`, and sets
`avg_out_degree = 0.0` with the placeholder comment above. It does **NOT** touch `tjs()`, the planner,
the TJS patch, or `join_order.c`. It reuses the FROZEN `LegStats` struct and is the exact input that
the Option-B lowering will feed to `tridb_choose_join_order`. Marked UNBUILT-HERE (GX10-gated).

The selectivity estimate is taken as an argument rather than computed inside the helper on purpose:
the `clauselist_selectivity` call needs planner context (`PlannerInfo`/`RelOptInfo`) that exists at
the lowering site, not in a leaf helper. Keeping the estimator call at the call site and the helper
pure makes the helper trivially correct and unit-testable, and keeps the FROZEN-function reuse clean.

## Staged implementation plan

**Stage 0 — graph stat prerequisite (graph-store track, blocks faithful estimate only).**
Add `gm_edge_count` to `GphMeta`, increment it in `gph_insert_edge`, expose
`graph_store.avg_out_degree('<relname>')`. GX10-buildable graph-store C. Not on the FR-6 critical
path (see gap note) but required before `tridb_estimate_intermediate` can report graph fan-out.

**Stage 1 — LegStats catalog builder (this ADR, SAFE).**
`join_order_legstats.c/.h`. Pure catalog read + arithmetic, reuses FROZEN struct/functions. No
operator or planner change. GX10-gated; drafted here for review.

**Stage 2 — call-site decision (lowering, GX10).**
In the DEV-1167 lowering of the canonical query: resolve the table, run the restriction-selectivity
estimator on the `WHERE` clause, call `tridb_build_legstats`, then `tridb_choose_join_order` (GUC
threshold). Produces a `'filter_first'`/`'vector_first'` decision. Still inert until Stage 3/4.

**Stage 3 — TJS filter-first physical path (GX10, the hard part, NOT started).**
Add the second physical body to `tjs()` behind a new `join_order` arg: drain the relational
predicate first (it is already expressible as the index scan's `WHERE`, but here it drives), seed the
graph traversal from the (small) qualifying set, and probe the vector index per seed under the SAME
global top-k PQ + `consecutive_drops` bound. This is the SPI/TR-1-risky change ADR-0007 anticipated;
it MUST be built and proven on the GX10. **This ADR does not start it** — it specifies it.

**Stage 4 — observability (GX10).**
Add `tjs_last_join_order()` companion so EXPLAIN-equivalent assertions can see the selected driver
(Option B's EXPLAIN-visibility tax). Wire the lowering's decision into `tjs()`'s `join_order` arg.

## Test plan (satisfies DEV-1285 done-criteria)

1. **Decision-core parity (exists, GX10-green).** `test/join_order_test.sql` already pins
   `tridb_choose_join_order` against the FROZEN matrix — unchanged.
2. **LegStats builder unit (GX10).** A SQL/regress wrapper over `tridb_build_legstats`: a table with
   known `reltuples` (set via `ANALYZE` then asserted), a known selectivity, asserts the produced
   `LegStats` fields. Asserts the `reltuples == 0 → table_size 0 → selectivity 1.0` safe path.
3. **Decision changes execution — the FR-6 end-to-end (GX10).** The done-criterion. Two corpora with
   **inverted selectivity** (heuristic doc §8: Corpus A 0.5% → filter_first; Corpus B 80% →
   vector_first) run through the FULL lowering→`tjs()` path. Assert: (a) `tjs_last_join_order()`
   reports opposite orders for A vs B (the "EXPLAIN shows selected driver" criterion under Option B);
   (b) the **peak intermediate (SM-1) differs materially** — filter_first on Corpus A examines far
   fewer candidates than vector_first would, verified via `tjs_candidates_examined()` (SM-3 probe).
   This is the test that proves the decision is no longer inert.
4. **TR-1 preservation (GX10).** On the larger corpus, assert `tjs_candidates_examined() << corpus`
   for BOTH physical paths (no blocking) and that a top-`LIMIT k` still early-terminates each path —
   the same SM-3 evidence ADR-0007 uses for the vector-first body, now also required for filter-first.

## TR-1 preservation argument (the non-negotiable)

Option B preserves TR-1 (CLAUDE.md golden rule 1) by construction:

- The **vector-first body is unchanged** — its bounded size-`k` priority queue and
  `consecutive_drops >= term_cond` stop (ADR-0007 §3, corrected for past-frontier counting in
  `tridb_tjs_predicate_termination.patch`) are untouched. Whatever TR-1 guarantee it has today, it
  keeps.
- The **new filter-first body reuses the same global top-k PQ** and drives the graph traversal
  through the native `gs_getnext` iterator, which is itself a Volcano Open/Next/Close iterator with
  per-call early termination (`graphstore.h`: "reads at most one adjacency page per call"). The
  vector probes run under the same bounded PQ. No step materializes a full intermediate: the
  relational predicate is drained as an iterator, the graph fans out one edge per Next, and the
  vector leg is bounded by `k`. There is no blocking operator and no full-intermediate
  materialization — which is exactly the design TriDB's golden rule 1 demands and golden rule 3
  requires (graph stays a native traversal, never a relational join).
- The decision logic (`tridb_choose_join_order`) is pure arithmetic over four scalars — it cannot
  introduce a blocking operator; it only selects which already-TR-1-safe body runs.

Option A's TR-1 argument is weaker precisely because it would re-implement the iterator lifecycle in
a new node — that is the risk this ADR routes around.

## Consequences

- DEV-1285's SAFE, design-complete portion lands now (this ADR + `join_order_legstats.c`). The
  decision-changes-execution portion is honestly GX10-gated and explicitly NOT claimed done.
- A graph-store prerequisite (`gm_edge_count` on the metapage) is surfaced as a real, previously
  undocumented gap; until it lands, `avg_out_degree` is a documented `0.0` placeholder that does NOT
  affect the FR-6 ordering decision (only the EXPLAIN intermediate estimate's graph fan-out).
- Choosing Option B keeps the CustomScan upgrade as a clean v2 item, consistent with ADR-0007, and
  avoids pulling the unfinished SQL/PGQ parser into the v1 join-order work.
- EXPLAIN visibility of the chosen driver is a deliberate, small added cost (a companion
  introspection function), not free as it would be under a CustomScan.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| Option A (CustomScan + `set_join_pathlist_hook`) now | Re-derives a TR-1-correct executor node (highest risk in the task), couples to the deferred SQL/PGQ parser, large surface — for only the EXPLAIN-visibility win, which Option B recovers cheaply. Correct as v2. |
| `planner_hook` "rewrite the driving child" (heuristic doc §10.5 as written) | Assumes a Plan node with a driving child. TJS is an SRF with one ordered stream; there is no child slot to flip. The sketch predates the operator's final SRF shape. |
| Store `avg_out_degree` as a float on the metapage | Redundant — derive it from `gm_edge_count / gm_vertex_count` on read; storing a float invites staleness between the two counts. (And the whole stat is out of the FR-6 decision path anyway.) |
| Model the filter-first path as relational JOINs feeding the graph | Violates golden rule 3 (graph is native traversal, never relational joins). The filter-first body must drive the native `gs_getnext` iterator. |
| Implement the filter-first body on the x86 standin and call it done | GX10-gated: the operator is MSVBASE-fork C with SPI-driven executor lifecycle; it builds and the TR-1/SM-3 evidence is measurable ONLY on the GX10 (CLAUDE.md hardware reality). |

## Addendum 2026-07-01 — Stage 0 landed (advisor plan 006, engine-validated)

The metapage degree-stat gap this ADR flagged as an open follow-up is now closed. Advisor
plan 006 added `uint64 gm_edge_count` to `src/graph_store/gph_page.h` (store-wide directed-edge
count, the FR-6 `avg_out_degree` source), and `src/planner/join_order_legstats.c` now reads it
to derive `avg_out_degree` instead of the earlier `avg_out_degree = 0.0` placeholder. This was
built + passed the full native-AM engine suite on the `tridb/msvbase:dev` x86 image (including
crash-recovery abort-safety), so Stage 0 is landed, not just designed. The body above is
unchanged; this addendum carries the delta. Remaining open work: Stage 3, the filter-first
operator body (DEV-1290), which is still GX10-gated and deliberately not started.

## Addendum 2026-07-02 — Stage 2 landed (call-site decision in the lowering)

The Stage-2 decision is now made and recorded on every `graph_store.graph_query()` call
(`src/graph_store_ext/graph_store--0.1.0.sql`), with one deliberate deviation from the body's
sketch: the lowering is **plpgsql**, so instead of exposing `clauselist_selectivity` /
`tridb_build_legstats` through new C plumbing, `rel_filter_matches` is taken from the
planner's own row estimate via `EXPLAIN (FORMAT JSON)` on the canonical `WHERE` — the same
`clauselist_selectivity × reltuples` product, reached through the planner's front door.
`table_size` comes from `pg_class.reltuples` (0 when never ANALYZEd → the FROZEN
"selectivity 1.0 → vector_first" safe default), and the FROZEN `tridb_choose_join_order`
(SQL surface, GUC threshold) makes the call. `join_order_legstats.c` remains the builder for
the future C call sites (Stage 3/4, DEV-1290).

Observability: `graph_store.last_join_order()` reports the decision for the most recent
call (session GUC `graph_store.last_join_order`) — the lowering-level half of the Option-B
EXPLAIN-visibility tax; the operator-level `tjs_last_join_order()` still arrives with
DEV-1290. Soft dependency: if the `join_order` extension is not installed the lowering
records `vector_first` (today's only physical path) and proceeds unchanged.

Verified on the `tridb/msvbase:dev` x86 image (`scripts/join_order_lowering_test.sh`,
wired into `AM_TESTS`): inverted-selectivity windows (~1% vs ~80%) pick opposite orders
through the FULL lowering; both windows return the pre-Stage-2 vector-first answers (the
decision is inert on execution, exactly as scoped); the no-decision-core fallback holds.
GX10 re-validation rides the next `make graph-test` on-target. The decision binds to
execution only when DEV-1290 lands Stage 3/4.

## Addendum 2026-07-02 (later) — Stages 3+4 landed (DEV-1290): the filter-first body

Motivated the same day by the 1M measurement (`docs/benchmark_sm2_1m_v0.1.0.md`): at ~0.12%
joint selectivity the vector-first body loses 2× to the correctly-configured multi-store
baseline, and the FROZEN heuristic (via the Stage-2 lowering) already selects `filter_first`
there. Stage 3/4 land as fork patch `scripts/patches/tridb_tjs_filter_first.patch` (applied
LAST in the chain, after the termination fix and `tjs_open`):

- **`join_order` as an OPTIONAL 8th `tjs()` argument** (Option B as decided): 7-arg callers
  are byte-identical (vector-first); `'filter_first'` selects the new body; unknown values
  ERROR. `filter_first` with `src < 0` ERRORs — without a graph source the "drain" would be
  the blocking full scan TR-1 forbids.
- **The filter-first body** (`beginFilterFirstT`): drain the qualifying set —
  `reachable(src) ∩ relational filter` — through ONE bounded-batch SPI cursor (the reachable
  ids travel as a single `int8[]` parameter, never interpolated SQL; golden rule 3 holds: the
  ids come from the same native `graph_store.neighbors` probe both bodies share), ranking each
  row by **exact squared L2 computed in C** into the SAME bounded top-k PQ. Peak memory is
  O(batch + k) (per-batch detoast scratch is reset every fetch); the drain length is the
  predicate's true cardinality — the quantity FR-6 chose this path FOR, reported via
  `tjs_candidates_examined()`.
- **Deliberate deviation from the body's sketch:** the sketch said "probe the vector index per
  seed"; the landed body ranks the drained set EXACTLY instead — a set cheap enough to
  enumerate does not need an approximate index probe, so filter-first recall w.r.t. the
  predicate set is 1.0 by construction.
- **The vector-first body is UNTOUCHED** (the single biggest TR-1 risk, per the Decision):
  filter-first pre-completes the merge (`finish=true`, `result_stack` filled) and `execTJS`
  serves the pops unchanged.
- **Stage 4 observability + binding:** operator-level `tjs_last_join_order()` reports which
  body RAN (lowering-level `graph_store.last_join_order()` reports what was DECIDED); the
  lowering now passes its decision into the 8-arg `tjs()` when present, falling back to the
  7-arg form on older engines.

Tests: `test/tjs_filter_first_test.sql` (ENGINE_TESTS — parity, companion transitions,
examined divergence, error guards, alternating-body SPI lifecycle) and
`test/join_order_integration_test.sql` + harness (AM_TESTS — the FULL
lowering→operator FR-6 end-to-end, superseding the retired `join_order_integration_stub.sql`).
First live x86 run: identical answers both bodies; on the ~1%-selective window vector-first
examined 1999 of 2000 candidates, filter-first examined 3. GX10 validation + the three-way 1M
rerun (TriDB-ff vs TriDB-vf vs correct baseline) are the DEV-1290 close-out evidence.

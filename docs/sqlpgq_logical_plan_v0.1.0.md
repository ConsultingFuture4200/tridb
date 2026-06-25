# SQL/PGQ Surface → Logical Plan v0.1.0

> **Date:** 2026-06-23
> **Status:** Implemented (surface) — DEV-1167 landed on `dustin/dev-1167` (2026-06-25)
> **Gating:** surface is hardware-independent (plpgsql); `tjs()` it lowers to is in the
>   `tridb/msvbase:dev` image (DEV-1169)
> **Depends on:** `spec/tridb_spec_v0.1.0.md §5`, `docs/graph_store_layout_v0.1.0.md`,
>   DEV-1169 (TJS operator), DEV-1170 (join-order heuristic)
> **Implementation decision:** see §11 below + ADR-0008.

---

## 0. Implementation note (DEV-1167, 2026-06-25)

§§3–4 below describe the *logical* parse→plan model (analyzer expanding `GraphTableExpr`,
a `GraphTableScan` rtable node). The **v1 surface does not build that querytree machinery** —
it does not need to, because `tjs()` (DEV-1169) already IS the one logical plan. The shipped
front door is `graph_store.graph_query(canonical_sql text)`: a plpgsql set-returning function
that takes the FULL canonical statement as text, validates it against the single canonical
template (the scope guard), and lowers it to exactly one `tjs(...)` call. The §3 "RangeFunction
in the FROM list" approach was **disproven empirically** (stock PG 13.4 raises a syntax error on
the unquoted MATCH payload — see §11 and ADR-0008); §§3–4 are retained as the target logical
model for the future CustomScan upgrade. The actual mapping landed is in §11.

## 1. The One Canonical Query

```sql
SELECT chunk
FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
  COLUMNS ( src.embedding AS src_embedding,
            dst.chunk     AS chunk,
            dst.timestamp AS timestamp ) )
WHERE timestamp IN :selected_time_range
ORDER BY src_embedding <-> :question_embedding
LIMIT 5;
```

Every design choice in this document is evaluated against this single template. TriDB v1
does not generalize beyond it. If a parse or plan decision is not exercised by this query,
it is deferred.

---

## 2. Grammar Assembly (No New Query Language)

TriDB assembles its surface from three existing standards without invention:

| Surface piece | Standard | Notes |
|---|---|---|
| Outer `SELECT … FROM … WHERE … ORDER BY … LIMIT` | PostgreSQL 13.4 parser | Unchanged. |
| `GRAPH_TABLE ( MATCH … COLUMNS … )` | ISO SQL/PGQ (SQL:2023 Part 16) | Treated as a table-valued function in the FROM list. |
| `<->` distance operator | pgvector extension grammar | Registered operator family; plugs into ORDER BY natively. |
| `MATCH (n:label)-[:edge]->(m:label)` inside GRAPH_TABLE | SQL/PGQ MATCH clause; AGE grammar as reference | Pattern compiled to graph-leg plan node. |

Apache AGE's grammar is a reference only — TriDB does not pull in AGE as a dependency. The
adjacency-list access method is native (DEV-1163/1164); AGE's relational-join-table approach
is exactly what is rejected (spec §2, golden rule 3).

---

## 3. Parse → Raw Parse Tree

The Postgres parser sees a standard `SelectStmt`. The `GRAPH_TABLE(...)` call in the FROM
list is parsed as a `RangeFunction` node (table-valued function syntax). The SQL/PGQ MATCH
clause is carried in the function's argument list as an opaque string node at parse time and
expanded by the analyzer into a `GraphTableExpr` during the semantic analysis pass.

Key analyzer responsibilities:
1. Resolve `src`, `dst` as node pattern variables bound to the `entity` label.
2. Resolve `[:related_to]` as an edge-label predicate on the adjacency-list store.
3. Bind `src.embedding`, `dst.chunk`, `dst.timestamp` to their physical column locations
   (vector segment / adjacency-list node-property / relational tuple, respectively).
4. Register `<->` as the pgvector L2 (or cosine) operator over `src.embedding`.
5. Recognize `LIMIT 5` and propagate it as a top-k constraint into the planner.

After the analyzer the query is represented as a `Query` node (standard Postgres querytree)
with a `GraphTableScan` subplan node hanging off the rtable.

---

## 4. Logical Plan Tree

The canonical query produces **one logical plan** spanning all three legs. There is no
physical separation between "graph query" and "relational query" in the plan tree — they
are sub-nodes of a single rooted tree.

```
TJS (Tri-modal Join & Score)                         ← DEV-1169
│   Composes the three legs with one global score accumulator.
│   Maintains an internal streaming k-heap (k=5) for the ORDER BY.
│   Join-order heuristic selects which leg drives (DEV-1170).
│   Emits tuples one at a time via Open/Next/Close (Volcano).
│   [TR-1 root: TJS.Next() returns NULL once k=5 heap is confirmed;
│    TJS.Close() propagates immediately to all three child legs.
│    No separate TopK node — the k-heap lives inside TJS to avoid an
│    extra blocking boundary. See §9 physical mapping.]
│
├── [LEG-G] GraphTraversalScan
│   Access method: native adjacency-list (DEV-1164/1165)
│   Pattern: (src:entity)-[:related_to]->(dst:entity)
│   Projects: src.embedding, dst.chunk, dst.timestamp
│   Iterator: Open/Next/Close; yields one (src,dst) pair per Next()
│   NodeLabelFilter applied as predicate arg to amgettuple() — NOT a
│   separate child iterator node (see §9: "Predicate in amgettuple filter arg")
│   [TR-1: no full materialization of the edge set]
│
├── [LEG-V] HNSWRelaxedScan                          ← DEV-1168
│   Input: :question_embedding (parameter)
│   Index: HNSW on src.embedding column (MSVBASE segment)
│   Iterator: Open/Next/Close; yields candidates in approximately
│             ascending distance order using relaxed monotonicity
│   [TR-1: HNSW beam search stops once top-k is stable — no full
│           corpus scan; satisfies SM-3 <25% corpus examined]
│   (no child — HNSW drives its own traversal internally)
│
└── [LEG-R] RelationalFilter
        Predicate: timestamp IN :selected_time_range
        Access: B-tree index on timestamp column (PG table)
        Iterator: Open/Next/Close over IndexScan
        [TR-1: index range scan terminates when range exhausted;
               never scans outside the time window]
```

### Why one tree, not three sub-queries

The canonical query has a single `ORDER BY <-> LIMIT 5`. A plan that materializes all
graph matches, then ANN-ranks them, then filters, is the multi-system baseline TriDB
replaces. The LogicalPlan tree above enforces top-k globally at the TJS level: every leg
yields tuples lazily, the TJS accumulates a running k-heap, and TJS.Next() returns NULL
(issuing Close to all legs) once the k-th result is confirmed.

---

## 5. Leg Descriptions

### LEG-G — GraphTraversalScan

Drives the native adjacency-list access method (spec §4, golden rule 3). The access method
exposes a standard Postgres AM interface (amopen/amgettuple/amclose). Each call to
`amgettuple` returns one (src_node_id, dst_node_id) pair satisfying the label and edge-type
predicates. Node properties (embedding, chunk, timestamp) are fetched from the adjacency-list
node-property store by node_id — no relational table join required.

The 32KB page layout (MSVBASE --with-blocksize=32 fork constraint) applies here: the
adjacency-list page format must pack adjacency array entries to avoid cross-page pointer
chains that would break streaming. See `docs/graph_store_layout_v0.1.0.md` for the page
layout spec.

TR-1 compliance: `amgettuple` returns one row per call. No pre-fetch of the full neighbor
list. Early termination is issued via `amclose` when TJS.Close() propagates downward after
the k-heap is full and confirmed.

### LEG-V — HNSWRelaxedScan

Wraps MSVBASE's HNSW index as a Volcano iterator. MSVBASE exposes HNSW traversal through
the pgvector operator family and its own relaxed-monotonicity scan path (the VBASE
contribution). The iterator yields candidate (node_id, distance) pairs in approximately
ascending distance order.

"Relaxed monotonicity" means the iterator does not guarantee strict ascending order on each
Next() call, but guarantees that after consuming `ef_search` candidates (the HNSW beam
width parameter) the true top-k (for k << ef_search) is contained in the output. The TJS
k-heap uses this property: it continues pulling from HNSWRelaxedScan until the k-th
candidate's distance bound is tighter than the (k+1)-th candidate's lower bound.

TR-1 compliance: no full corpus scan. The HNSW beam search is bounded by the ef_search
parameter and the relaxed-monotonicity termination condition. SM-3 (<25% corpus examined)
is met by HNSW's sublinear scan property.

### LEG-R — RelationalFilter

Standard Postgres IndexScan over a B-tree index on the `timestamp` column. The predicate
`timestamp IN :selected_time_range` becomes an index range scan. The Volcano interface is
native Postgres: the executor calls `index_getnext` per tuple.

TR-1 compliance: range scan terminates when the B-tree range is exhausted. No table-scan
fallback in v1 — if no index exists, the planner raises an error (not a fallback seqscan,
which would violate SM-3 in the worst case).

---

## 6. TJS Operator (DEV-1169)

The **Tri-modal Join & Score** operator is the composition point. It is the only node in
the plan that has access to all three legs simultaneously.

Responsibilities:
1. **Open()**: calls Open() on all three child legs (order of Open() calls is
   immaterial; join order is determined by which leg's Next() the driver loop
   calls first, encoded in the child-slot ordering set at plan time by DEV-1170).
2. **Next()**: pulls one tuple from the driving leg, probes the other two legs for
   matching node_ids, assembles the full output tuple (chunk, src_embedding, timestamp,
   distance), and returns it.
3. **Close()**: calls Close() on all three child legs. This is the early-termination
   propagation path: once the internal k-heap is confirmed full, TJS stops calling Next()
   on legs and invokes Close() on all three, stopping HNSW beam search, graph traversal,
   and index scan simultaneously. The executor above TJS (the standard Postgres LIMIT node
   or query end) triggers this by calling TJS.Close() after receiving k tuples.

The TJS does not materialize any intermediate result. It holds at most one "in-flight"
tuple assembly at a time. Intermediate-result reduction (SM-1, target ≥5×) comes from the
fact that LEG-V only explores the HNSW graph until top-k is stable, rather than ranking
all graph matches.

Join-order decision (see §7) is evaluated once at plan time and encoded as the ordering of
child slots in the TJS node. The TJS operator itself is join-order-agnostic at runtime.

---

## 7. Join-Order Decision (DEV-1170)

The cross-modal join-order heuristic (spec §4.2, FR-6) answers: which leg drives the TJS?

Decision sits in the **planner**, between logical plan construction and physical plan
emission. The logical plan tree (§4) is join-order-independent — the TJS node always has
three children. The heuristic selects which child is promoted to "driving" role and which
two become "probe" legs.

The heuristic makes a single two-way choice — relational-first vs. vector-first — using
one selectivity signal at plan time:

| Input | Estimated by | Role |
|---|---|---|
| Relational selectivity | `rel_filter_matches / table_size` via `pg_statistic` | **Driver-selection signal**: if ≤ 0.10 → filter_first, else vector_first |
| Graph avg_out_degree | avg degree of `:entity` nodes (graph metapage) | Feeds intermediate-row **estimate** for EXPLAIN; not used in the driver selection itself |
| Vector ef_search | HNSW beam width config | Bounds the over-fetch cost estimate for the vector-first path |

The leg with the lowest estimated output cardinality drives, but v1 only compares relational
vs. vector; the graph leg is never a standalone driver (its cost scales with whatever seed set
the leading leg produces, so it is always in the middle). See `join_order_heuristic_v0.1.0.md`
§2 for the full two-ordering table and `src/planner/join_order_ref.py` for the reference
implementation.

The heuristic is a single decision function — no cost model, no cardinality estimation
machinery. This is the "20% effort for 80% of the ordering win" referenced in spec §2.

The reference model is implemented in Python for design validation and unit testing
(DEV-1170 deliverable: `docs/join_order_heuristic_v0.1.0.md` + `tests/`). The production
implementation is a C function in the planner (`src/planner/join_order.c`, GX10-gated).

---

## 8. TR-1 Early-Termination Flow (LIMIT 5 Push-Down)

TR-1 is the non-negotiable execution invariant: every operator is a Volcano iterator with
early termination. The LIMIT 5 must push down so no leg fully materializes.

Flow trace for `LIMIT 5`:

```
TJS.Open(k=5)                                [k embedded in TrimodalJoinState]
    → LEG-G.Open()    [graph traversal cursor positioned, no rows fetched]
    → LEG-V.Open()    [HNSW beam initialized, no candidates fetched]
    → LEG-R.Open()    [index scan cursor positioned, no tuples fetched]

[loop: executor pulls tuples from TJS via the standard LIMIT 5 node]
TJS.Next() × N  (N ≥ 5; TJS discards non-joining tuples internally)
    → driving leg.Next() per iteration
    → TJS inserts qualifying tuples into internal k-heap

TJS k-heap confirmed full (relaxed-monotonicity bound satisfied for LEG-V);
TJS.Next() returns NULL to executor (LIMIT node receives 5 tuples and calls Close)
TJS.Close()
    → LEG-G.Close()   [graph traversal cursor released; adjacency-list AM amclose()]
    → LEG-V.Close()   [HNSW beam search aborted; ef_search budget not fully consumed]
    → LEG-R.Close()   [index scan cursor released]
```

No leg runs to completion. LEG-V's HNSW beam is the most computationally expensive
sub-process; aborting it after top-k stabilizes is the primary source of SM-3 (<25%
corpus examined) and SM-1 (≥5× intermediate-result reduction).

**What "fully materializes" means and why it is forbidden:** A blocking operator reads its
entire input before producing any output (e.g., a sort over all graph matches). If any
operator in the plan is blocking, the LIMIT 5 cannot propagate downward through it, and
every leg upstream of the blocking operator runs to completion regardless of k. This
forfeits the TriDB efficiency thesis (spec §2) and fails SM-1 and SM-3.

The plan tree in §4 contains no blocking operators. The TJS internal k-heap is a streaming
heap (size k), not a full sort — it evicts as it fills. TJS is a probe-join with one
in-flight tuple. All three legs are index-driven iterators.

---

## 9. Physical Plan Mapping Summary

| Logical node | Physical realization | Status |
|---|---|---|
| TJS (with embedded k-heap) | `TrimodalJoinState` executor node; k-heap (k=5) is a field of `TrimodalJoinState`, not a separate plan node | DEV-1169, GX10-gated |
| GraphTraversalScan | Adjacency-list AM (`amopen`/`amgettuple`/`amclose`) | DEV-1164/1165, GX10-gated |
| HNSWRelaxedScan | MSVBASE HNSW iterator, wrapped | DEV-1168, GX10-gated |
| RelationalFilter | Postgres IndexScan (existing) | Inherited from PG 13.4 |
| NodeLabelFilter | Predicate arg to `amgettuple`; no separate iterator node | DEV-1164, GX10-gated |

---

## 10. Open Questions / Deferred

- **Multiple hops:** `MATCH (src)-[:r]->(mid)-[:r]->(dst)` is not in scope for v1. The
  GraphTraversalScan is designed for single-hop only. Multi-hop would require a recursive
  iterator; deferred to v2.
- **Edge properties beyond `:related_to`:** marker #3 in spec §12. The COLUMNS projection
  only exposes node properties in v1; edge property projection requires adjacency-list
  page layout additions.
- **EXPLAIN output:** the plan tree in §4 should map to a TriDB-specific EXPLAIN node
  format. Deferred — not required for correctness.
- **Adaptive re-planning:** join-order is fixed at plan time. If the driving leg's
  selectivity estimate is wrong at runtime (e.g., the timestamp range is wider than
  statistics suggest), the plan does not adapt. Deferred to v2.

---

## 11. Landed surface (DEV-1167) — how the canonical text lowers to `tjs()`

**Surface mechanism.** `graph_store.graph_query(canonical_sql text) RETURNS SETOF text`
(plpgsql, in the `graph_store` extension; install SQL `src/graph_store_ext/graph_store--0.1.0.sql`).
The whole canonical statement is passed as one text argument with the three `:params`
substituted to literals. Rationale (both verified on `tridb/msvbase:dev`, ADR-0008):

1. The verbatim canonical `GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->… )` is a **PG 13.4
   syntax error** (`syntax error at or near ":"`) — `(:label)` / `-[:edge]->` are not valid SQL
   expression grammar, so a bare RangeFunction call never reaches the function body. Carrying the
   payload verbatim without a grammar fork is only possible as a string literal.
2. The single global top-k must live inside `tjs()`. An outer `ORDER BY <-> LIMIT` is a blocking
   sort over the scalar `<->`, which returns 0 outside an index scan (ADR-0006) — wrong rankings,
   forfeits TR-1. So the front door owns WHERE + ORDER BY + LIMIT, not just the MATCH.

**Scope guard.** One anchored, case-insensitive regex over the whitespace-normalized statement
validates the single canonical template. Off-template variants (wrong edge label, extra hop,
wrong COLUMNS projection, missing `<->`, missing LIMIT) fail to match and `RAISE EXCEPTION` —
golden rule 4.

**Lowering (argument mapping).**

```
SELECT chunk
FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
  COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
WHERE src.id = <N> AND timestamp IN (<window>)
ORDER BY src_embedding <-> '<vector>'
LIMIT <k>
        │
        ▼
tjs('entities', <k>, 0, <N>::bigint, 'id, chunk', 'ts IN (<window>)', 'embedding <-> ''<vector>''')
```

| `tjs` arg | from |
|---|---|
| `table_name = 'entities'` | the `:entity` label backing relation |
| `k` | `LIMIT <k>` |
| `term_cond = 0` | canonical has no override |
| `src` | `WHERE src.id = <N>` (the v1 single-src binding) |
| `attr_exp = 'id, chunk'` | 1st col = candidate graph id (tjs reachability contract); chunk returned |
| `filter_exp` | `timestamp IN (<window>)`, canonical `timestamp` → physical `ts` |
| `orderby_exp` | `embedding <-> '<vector>'` (the dst embedding) |

**Surface ↔ operator contract gaps (real, bridged by a documented v1 binding — ADR-0008):**
(1) the canonical `src` is a pattern variable (a SET); `tjs` takes one vertex → v1 **requires**
`WHERE src.id = <const>`. (2) The canonical `ORDER BY src_embedding` cannot rank a single pinned
src; it is mapped onto the dst `embedding` column (exactly what the oracle does). Both match
`test/trimodal_compose.sql` / `test/canonical_e2e_test.sql`. Recommend a spec §5 addendum.

**Test.** `test/parse_canonical.sql` (wired into `Makefile` `ENGINE_TESTS`; run via
`scripts/tjs_test.sh … test/parse_canonical.sql`) asserts: (1) canonical via the surface returns
`{20,10}` = the direct `tjs()` answer (FR-4 one plan); (2) the timestamp filter is load-bearing
(window `IN (100)` → 30, `IN (100,999)` → 40); (3) the scope guard rejects 5 off-template
variants. All green.

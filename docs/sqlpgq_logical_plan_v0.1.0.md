# SQL/PGQ Surface → Logical Plan v0.1.0

> **Date:** 2026-06-23
> **Status:** Draft — design deliverable for DEV-1167 (Phase 2)
> **Gating:** 🟡 design unblocked here; TJS operator implementation is GX10-gated (DEV-1169)
> **Depends on:** `spec/tridb_spec_v0.1.0.md §5`, `docs/graph_store_layout_v0.1.0.md`,
>   DEV-1169 (TJS operator), DEV-1170 (join-order heuristic)

---

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
TopK (k=5)
│   ORDER BY src_embedding <-> :question_embedding
│   [TR-1: stops pulling tuples once k=5 confirmed — early termination root]
│
└── TJS (Tri-modal Join & Score)                     ← DEV-1169
    │   Composes the three legs with one global score accumulator.
    │   Join-order heuristic selects which leg drives (DEV-1170).
    │   Emits (chunk, src_embedding, timestamp, score) tuples lazily.
    │
    ├── [LEG-G] GraphTraversalScan
    │   │   Access method: native adjacency-list (DEV-1164/1165)
    │   │   Pattern: (src:entity)-[:related_to]->(dst:entity)
    │   │   Projects: src.embedding, dst.chunk, dst.timestamp
    │   │   Iterator: Open/Next/Close; yields one (src,dst) pair per Next()
    │   │   [TR-1: no full materialization of the edge set]
    │   │
    │   └── NodeLabelFilter (label = 'entity')
    │           Applied at Next() time, not before full scan
    │
    ├── [LEG-V] HNSWRelaxedScan                      ← DEV-1168
    │   │   Input: :question_embedding (parameter)
    │   │   Index: HNSW on src.embedding column (MSVBASE segment)
    │   │   Iterator: Open/Next/Close; yields candidates in ascending
    │   │             distance order using relaxed monotonicity
    │   │   [TR-1: HNSW beam search stops once top-k is stable — no full
    │   │           corpus scan; satisfies SM-3 <25% corpus examined]
    │   │
    │   └── (no child — HNSW drives its own traversal internally)
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
yields tuples lazily, the TJS accumulates a running top-k heap, and the TopK node pulls
exactly k tuples before issuing Close to all legs.

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
list. Early termination is issued via `amclose` when the TJS receives a Close() from TopK.

### LEG-V — HNSWRelaxedScan

Wraps MSVBASE's HNSW index as a Volcano iterator. MSVBASE exposes HNSW traversal through
the pgvector operator family and its own relaxed-monotonicity scan path (the VBASE
contribution). The iterator yields candidate (node_id, distance) pairs in approximately
ascending distance order.

"Relaxed monotonicity" means the iterator does not guarantee strict ascending order on each
Next() call, but guarantees that after consuming b candidates the true top-k (for k << b)
is contained in the output. The TopK node uses this property: it continues pulling from
HNSWRelaxedScan until the k-th candidate's distance bound is tighter than the
(k+1)-th candidate's lower bound.

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
1. **Open()**: calls Open() on all three child legs with the join-order-selected driving
   leg first.
2. **Next()**: pulls one tuple from the driving leg, probes the other two legs for
   matching node_ids, assembles the full output tuple (chunk, src_embedding, timestamp,
   distance), and returns it.
3. **Close()**: calls Close() on all three child legs. This is the early-termination
   propagation path: when TopK issues Close() after receiving k tuples, TJS immediately
   propagates Close() to all legs, stopping HNSW beam search, graph traversal, and index
   scan simultaneously.

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

The heuristic evaluates three selectivity proxies at plan time:

| Proxy | Estimated by |
|---|---|
| Graph fan-out | avg degree of `:entity` nodes (stored in graph metadata catalog) |
| Vector k candidates | ef_search parameter / corpus size ratio |
| Relational selectivity | timestamp range width / total timestamp span (column stats) |

The leg with the lowest estimated output cardinality drives. The heuristic is a single
decision function — no cost model, no cardinality estimation machinery. This is the "20%
effort for 80% of the ordering win" referenced in spec §2.

The heuristic reference model is implemented in Python for design validation and unit
testing (DEV-1170 deliverable: `docs/join_order_heuristic_v0.1.0.md` + `tests/`).
The production implementation is a C function in the planner (`src/planner/join_order.c`,
GX10-gated).

---

## 8. TR-1 Early-Termination Flow (LIMIT 5 Push-Down)

TR-1 is the non-negotiable execution invariant: every operator is a Volcano iterator with
early termination. The LIMIT 5 must push down so no leg fully materializes.

Flow trace for `LIMIT 5`:

```
TopK.Open(k=5)
  → TJS.Open()
      → LEG-G.Open()    [graph traversal cursor positioned, no rows fetched]
      → LEG-V.Open()    [HNSW beam initialized, no candidates fetched]
      → LEG-R.Open()    [index scan cursor positioned, no tuples fetched]

[loop: TopK pulls tuples]
TopK.Next() × 5
  → TJS.Next() × N  (N ≥ 5; TJS may discard non-joining tuples)
      → driving leg.Next() per iteration

TopK sees k=5 confirmed (relaxed-monotonicity bound satisfied for LEG-V)
TopK.Close()
  → TJS.Close()
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

The plan tree in §4 contains no blocking operators. TopK is a streaming top-k heap (size
k), not a full sort. TJS is a probe-join with one in-flight tuple. All three legs are
index-driven iterators.

---

## 9. Physical Plan Mapping Summary

| Logical node | Physical realization | Status |
|---|---|---|
| TopK | Streaming k-heap in TJS driver loop | Design (this doc) |
| TJS | `TrimodalJoinState` executor node | DEV-1169, GX10-gated |
| GraphTraversalScan | Adjacency-list AM (`amopen`/`amgettuple`/`amclose`) | DEV-1164/1165, GX10-gated |
| HNSWRelaxedScan | MSVBASE HNSW iterator, wrapped | DEV-1168, GX10-gated |
| RelationalFilter | Postgres IndexScan (existing) | Inherited from PG 13.4 |
| NodeLabelFilter | Predicate in `amgettuple` filter arg | DEV-1164, GX10-gated |

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

# ADR-0019: Re-homing the fused operator on stock PostgreSQL — `tjs_open` as an extension over pgvector

- **Status:** Accepted (2026-07-15) — roadmap D2 phase 2.5; the second half of Gate B
- **Inputs:** ADR-0015 (E1 fork mechanism inventory, E3 pgvector iterative-scan gaps),
  ADR-0007 (term_cond), DEV-1290/ADR-0011 (filter-first physical body), Gate A/B verdicts
  (`docs/wikidata_spike_v0.2.0.md`, `docs/gate_b_spike_v0.1.0.md`)
- **Scope:** the *seedless / vector-first* physical path on stock PG. Filter-first needs no
  operator on stock PG (a single fused SQL statement over `gph_traverse_bfs` won Gate B).

## Context

The fork exists (ADR-0015 E1) because stock PostgreSQL's **executor** rejects an index scan that
emits approximately-ordered tuples (`"index returned tuples in wrong order"`), and no stock hook
lets a top-k sort stop pulling from a still-streaming ANN scan. The MSVBASE fork patches the AM
API, the scan struct, the executor, and nodeSort to legalize relaxed-order streaming.

**The load-bearing observation of this ADR: that entire mechanism is only needed when the
*executor* drives the scan.** An extension function that opens the index itself —
`index_beginscan` + an ORDER-BY `ScanKey` + `index_getnext_tid` + `table_index_fetch_tuple` —
is not subject to nodeIndexscan's ordering check or nodeSort's pull semantics. The operator can
consume pgvector's HNSW scan (with `hnsw.iterative_scan = relaxed_order`, pgvector ≥ 0.8: a
genuinely resumable ordered candidate stream, ADR-0015 E3) directly, apply its own early
termination, and stop pulling whenever TR-1 says so. **The fork's executor surgery is unnecessary
for an operator that owns its scan loop.**

## Decision

Build `tridb_tjs` — a stock-PG PGXS extension (src/tjs_pg/) providing the fused operator:

```
tjs_open(table regclass, k int, term_cond int, m_seeds int, hops int,
         id_col text, filter text, rank_expr_vec vector,
         src bigint DEFAULT NULL, edge_type int DEFAULT 0)
  RETURNS SETOF bigint
```

vs the fork surface: the rank expression becomes a **vector parameter** (the operator locates the
HNSW index on `table`'s vector column itself; no SQL-fragment distance expression to parse), and
the **typed-traversal slot is absorbed** (`src` + `edge_type`, the Gate A surface caveat): when
`src IS NOT NULL` the operator runs **filter-first** (graph reach via SPI `gph_traverse_bfs(src,
hops, edge_type)` → relational filter → exact rank — the Gate B winning plan, now behind the
operator surface); when `src IS NULL` it runs **vector-first / seedless**:

1. **Own the scan loop (closes E3 gap 2).** Open `table`'s HNSW index directly; ORDER-BY ScanKey
   on the vector column vs the query vector; `index_getnext_tid` streams candidate TIDs in
   relaxed order. The operator copies each TID itself — no `xs_heaptid_orig` needed.
2. **Recompute the candidate distance from the heap tuple (closes E3 gap 1).** pgvector does not
   populate `xs_orderbyvals`; the operator fetches the heap tuple (visibility-checked) and calls
   the pgvector distance function (`l2_distance(vector,vector)`, resolved by name once per Open
   and cached as an `FmgrInfo` — no dependence on pgvector's internal struct layout).
3. **Termination = term_cond over the operator's own top-k (E3 gap 3, partially).** The fork's
   consecutive-drops `term_cond` (ADR-0007) is applied by the operator on its recomputed
   distances; when it fires, the scan is closed mid-stream (TR-1 early termination — legal, we
   own the loop). pgvector's `hnsw.max_scan_tuples` remains a *disclosed outer budget*: if the
   stream ends on budget before term_cond fires, the result is BUDGET-CAPPED and the operator
   reports it (see counters). The SM-4 recall curve on stock PG is therefore
   (term_cond, max_scan_tuples)-shaped; the harness must sweep and report both — the honest
   consequence ADR-0015 E3.3 predicted. This is the one fork capability not fully reproduced:
   the fork's stream never ends before the operator decides; pgvector's can.
4. **Graph + relational predicates per candidate** via SPI (prepared, generic plans, cached for
   the call): the graph reachability probe reuses `graph_store.gph_neighbors_ext` /
   `gph_traverse_bfs` exactly as the fork operator does today (SPI is already its mechanism);
   the relational filter is a prepared `EXISTS(SELECT 1 FROM <table> WHERE <id_col>=$1 AND
   (<filter>))`. MVP accepts per-candidate SPI cost; a batched probe is a later optimization.
5. **Counters** mirror the fork: `tjs_open_candidates_examined()` (per-backend), plus
   `tjs_open_budget_capped()` (bool: the last call's stream ended on max_scan_tuples rather than
   term_cond) — the honesty signal the gate consumes.

## What dies / what this does NOT do

- No executor or planner hooks; no core patches; no relaxed-order AM flag. The operator is a
  plain SRF — like the fork's, it is invoked via SELECT, not planned as a join node.
- The fork's 10 HNSW hardening patches are superseded by pgvector's mature AM (ADR-0015 E1/F5);
  plan 043 (seedless non-determinism) is moot — pgvector's build is deterministic under fixed
  input order, and its iterative scan has no known hang class.
- Not in scope here: BM25 seam, WCOJ, the CSR migration (2.3, separately gated).

## Consequences

- The canonical-query surface (spec §5) exists on stock PG end-to-end once this lands: fused
  operator (both physical paths) + native graph AM + pgvector + relational — `CREATE EXTENSION`
  installable, no fork.
- The E3.3 budget-shaped recall becomes a *measured, reported* property (harness sweep), not a
  silent regression.
- The fork remains the reference implementation until the stock operator's SM-4 curve and the
  Gate-B-style h2h reproduce on it; then the fork moves to maintenance (launch-vehicle posture,
  ADR-0015 FAQ).

## Addendum (2026-07-16, advisor plan 075) — the canonical front door lowers to tjs_open

`graph_store.graph_query(text)` (the spec §5 v1 surface, DEV-1167) previously recognized only
the fork `tjs()` signatures, so a stock install with `vector + graph_store_am + tjs_pg` could
call `tjs_open` directly but the documented front door failed. The lowering now adds a stock
branch, selected only when the exact
`public.tjs_open(regclass,integer,integer,integer,integer,text,text,vector,bigint,integer)`
signature is installed (detection is catalog-safe: gated on `to_regtype('vector')` and the
`pg_extension` row before probing the signature, so it cannot error on installs without
pgvector). Mapping: `k`=LIMIT, `term_cond`=0, `m_seeds`=0, `hops`=1, `id_col`='id', the parsed
timestamp window, the parsed query vector (brace dialect converted to pgvector brackets, bound
as a parameter), the pinned `src.id`, and `edge_type` = the `graph_store.edge_type` catalog id
of the canonical label `related_to` (RAISES if the row is absent — never "any edge"). Result
ids join back to `entities` for the canonical `chunk` column in operator emit order
(`WITH ORDINALITY`). With the v1 pinned src this is always the filter-first body;
`graph_store.last_join_order()` reports `filter_first`. Grammar stays pinned; the only admitted
widening is the brace/bracket vector-literal dialect pair. Proven end-to-end on PG 16 and 17 by
`test/canonical_stock_e2e_test.sql` (STOCK_TESTS + CI `stock-pg`).

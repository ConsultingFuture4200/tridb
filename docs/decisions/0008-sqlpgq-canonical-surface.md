# ADR-0008: The SQL/PGQ canonical surface — whole-statement front door, no grammar fork

**Status:** Accepted (2026-06-25)
**Issue:** DEV-1167 (SQL/PGQ surface: parse canonical query to single logical plan)
**Related:** ADR-0007 / DEV-1169 (TJS operator — the lowering target), ADR-0006 / DEV-1168
(relaxed-monotonicity vector iterator), spec §5 (the one canonical query),
`docs/sqlpgq_logical_plan_v0.1.0.md` (governing design)
**Scope decision:** ship the front door as a plpgsql set-returning function in the
`graph_store` extension that takes the FULL canonical statement as text, validates it against
the single canonical template (scope guard), and lowers it to ONE `tjs(...)` call.

## Context

DEV-1167 is the front door: it must turn the ONE canonical query (spec §5) into the single
`tjs(...)` call shipped by DEV-1169 — no app-layer merge, no SQL nesting, one global
early-terminating top-k inside the operator (golden rules 1, 4; FR-4).

The intended path (per the issue) was: PG 13.4 has no native SQL/PGQ `GRAPH_TABLE`, but stock
PG parses `GRAPH_TABLE(...)` in a FROM clause as a table-valued function (RangeFunction), so
register `GRAPH_TABLE(...)` as a FUNCTION and hand-parse only the MATCH/COLUMNS payload.

**Two facts, both verified empirically on `tridb/msvbase:dev` (2026-06-25), invalidate the
naive form of that path:**

1. **The verbatim canonical MATCH payload does not parse in stock PG 13.4.** The tokens
   `(:label)` and `-[:related_to]->` are not valid SQL expression grammar, so a bare
   `FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity) COLUMNS (...) )`
   raises `syntax error at or near ":"` **before any TriDB code runs** — the function body
   never executes. A RangeFunction argument must be a valid SQL expression; the unquoted
   MATCH payload is not one. (Honoring "no grammar fork" means the payload can only be carried
   verbatim as a **string literal**.)

2. **The outer `ORDER BY src_embedding <-> :q LIMIT :k` cannot live in the user's SQL.** The
   single global top-k must live *inside* `tjs()` (ADR-0007). MSVBASE's scalar `<->` returns 0
   outside an HNSW index scan (ADR-0006), so an outer ORDER BY would be a **blocking sort over
   garbage distances** — forfeiting TR-1 and producing wrong rankings. The front door must
   therefore own the WHERE + ORDER BY + LIMIT, not just the MATCH payload.

## Decision

### 1. Whole-statement text front door, lowering to one `tjs()` call

The surface is `graph_store.graph_query(canonical_sql text) RETURNS SETOF text`. It accepts the
**full** canonical statement (with the three `:params` substituted to literals) as one text
argument, validates it against the single canonical template, and lowers to exactly one
`tjs(...)` call. It is plpgsql, not C: `tjs()` does all the heavy lifting (executor-driving
HNSW scan, graph reachability probe, early-terminating top-k), so the front door is pure
template-validation + argument assembly. plpgsql also keeps the surface hardware-independent
(no GX10 gate) and re-clone-stable (no compile step).

It is folded into the **`graph_store` extension**, not a third extension: the lowering depends
on this extension's reachability iterator and on the vectordb `tjs()` operator at runtime.

### 2. Scope guard = a single anchored template match

The "recursive-descent matcher for the single canonical template" is realized as one anchored,
case-insensitive regex over the whitespace-normalized statement. Anything off-template — wrong
edge label, extra hop, wrong COLUMNS projection, missing `<->` order-by, missing LIMIT — fails
to match and RAISEs. This is the scope guard (golden rule 4): TriDB v1 accepts ONLY the one
canonical query and refuses to generalize the surface.

### 3. Argument mapping (canonical → `tjs(table_name,k,term_cond,src,attr_exp,filter_exp,orderby_exp)`)

| `tjs` arg | Canonical source | Value |
|---|---|---|
| `table_name` | `:entity` label backing relation | `'entities'` |
| `k` | `LIMIT <k>` | the LIMIT literal |
| `term_cond` | (canonical has no override) | `0` |
| `src` | `WHERE src.id = <N>` | the pinned src vertex |
| `attr_exp` | projection + tjs contract | `'id, chunk'` (1st col MUST be the candidate graph id) |
| `filter_exp` | `timestamp IN (<window>)` | the predicate, `timestamp`→physical `ts` |
| `orderby_exp` | `ORDER BY … <-> '<vector>'` | `'embedding <-> ''<vector>'''` (dst embedding) |

## The surface ↔ operator contract gap (real finding, bridged by a documented v1 binding)

The canonical text as written in spec §5 does **not** lower 1:1 onto `tjs()`'s actual
signature. Two genuine mismatches, both resolved exactly the way the runnable v1 oracle
(`test/trimodal_compose.sql`, `test/canonical_e2e_test.sql`) resolves them:

1. **`src` is a SET, `tjs` takes one vertex.** The canonical `MATCH (src:entity)-...` binds
   `src` as a pattern *variable* — semantically a set of all entities with an outgoing
   `related_to` edge. `tjs(... src bigint ...)` takes exactly **one** source vertex. v1
   therefore **requires** the canonical WHERE to pin `src.id = <const>`; the front door rejects
   a canonical query that does not. This is the documented v1 single-src binding, not a
   generalization. A src-set surface (drive tjs once per source, merge top-k) is a v-next
   concern.

2. **`ORDER BY src_embedding` vs. the dst stream.** The canonical text orders by
   `src_embedding`, but `tjs`'s only ordered stream is the **dst** HNSW scan; with a single
   pinned `src`, `src_embedding` is a constant and cannot rank anything. The lowering maps the
   ORDER BY embedding onto the dst `embedding` column — which is exactly what the oracle does
   (`ORDER BY e.embedding <-> …` where `e` is the dst entity). The spec §5 `src_embedding` is
   thus a spec-text artifact of the RAG framing (rank by how close the *answer's* neighbors are
   to the question); operationally it is the dst embedding.

**Recommendation:** spec §5 should be amended (addendum, not rewrite) to either (a) order by
`dst_embedding` and pin a single `src`, or (b) explicitly define src-set semantics for a later
version. Until then, the v1 binding above is the contract, and it is enforced by the scope
guard and proven by `test/parse_canonical.sql`.

## Consequences

- **FR-4 (one plan) lands at the surface level without a grammar fork**: the canonical query,
  fed to `graph_store.graph_query(...)`, returns the SAME top-k `{20,10}` as the direct `tjs()`
  call — proven by `test/parse_canonical.sql` ASSERTION 1.
- **The `GRAPH_TABLE(...)` text is carried verbatim** inside the statement string; the surface
  never invents a query language and never patches `gram.y` (the banned, re-clone-fragile path).
- **SQL-injection surface (inherited from `tjs`).** `graph_query` interpolates the extracted
  query-vector / timestamp literals into the `tjs` SQL-fragment args. The regex only admits a
  brace-delimited vector and a parenthesized IN-list, so the admitted shapes are narrow, but the
  same "trusted input only" caveat as ADR-0007 §SQL-fragment applies: v1 feeds the controlled
  canonical query, not arbitrary end-user text. A multi-query surface must validate/bind first.
- **`STABLE`, not IMMUTABLE** — it reads live graph state via `tjs`.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| `gram.y` patch for native SQL/PGQ | The banned "new query language"; fragile under re-clone (golden rule 4). |
| Bare `FROM GRAPH_TABLE(MATCH …)` with unquoted payload | Verified PG 13.4 **syntax error** — never reaches the function body. |
| Outer `ORDER BY <-> LIMIT` in the user's SQL | Blocking sort over the scalar `<->` (returns 0 outside the index scan, ADR-0006) — wrong rankings, forfeits TR-1. |
| C parser in `src/parser/graph_table.c` | `tjs()` does the heavy lifting; a C parser adds a GX10-gated compile step for pure template validation. plpgsql is sufficient and re-clone-stable. |
| A third extension for the surface | The lowering depends on `graph_store.neighbors` (this extension) + `tjs` (vectordb); folding it into `graph_store` avoids an extra `CREATE EXTENSION`. |

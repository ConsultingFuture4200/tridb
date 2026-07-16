/* graph_store_am 0.1.0 — TriDB native adjacency-list graph store (DEV-1164) */
-- complain if script is sourced in psql, rather than via CREATE EXTENSION
\echo Use "CREATE EXTENSION graph_store_am" to load this file. \quit

/*
 * The container relation. Its 32KB blocks hold the native graph pages (metapage, vertex pages,
 * adjacency pages) managed by the C code through the shared buffer manager + WAL. autovacuum is
 * disabled and it is NEVER accessed as a heap — all access goes through the gph_* functions.
 */
CREATE TABLE gstore (dummy "char") WITH (autovacuum_enabled = false);
COMMENT ON TABLE gstore IS
  'TriDB graph store page container (DEV-1164): 32KB blocks hold native graph pages. Do NOT access as a heap; use the gph_* functions.';

CREATE FUNCTION gph_insert_vertex() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

CREATE FUNCTION gph_insert_edge(bigint, bigint) RETURNS void
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Typed edge insert (advisor plan 038 / DEV-1350): the 3-arg overload writes the caller's edge
-- type id into the existing es_edge_type_id slot field (NO page-layout change). Same C symbol as
-- the 2-arg form (PG_NARGS branches); the 2-arg form is unchanged and defaults the type to
-- GPH_EDGE_TYPE_RELATED_TO (id 1), so every existing caller/bench is byte-identical.
CREATE FUNCTION gph_insert_edge(bigint, bigint, integer) RETURNS void
  AS 'MODULE_PATHNAME', 'gph_insert_edge' LANGUAGE C VOLATILE STRICT;

-- Batched edge-append (DEV-1354 / design §2 "bulk edge loader"): append a whole adjacency run for
-- ONE source in a single call, returning the number of edges appended. Byte-identical on-disk
-- chains to N x gph_insert_edge(src, dst[i]) fed in array order, but O(1)-per-edge instead of
-- O(V) (dense src locate + metapage dst bounds check), so the 1M/39M wiki graph loads in minutes
-- not the O(E*V) hours the per-edge path costs. Requires the dense-in-order load precondition
-- (vids 0..N-1 materialized before any edge); a non-dense layout is HARD-rejected in C (never
-- mis-writes). Rides the host txn/WAL (golden rule 2); a rolled-back batch leaves ZERO visible
-- edges (es_xmin filtered — FR-7). Owner-guarded (REVOKEd from PUBLIC below, plan 026).
CREATE FUNCTION gph_insert_edges(bigint, bigint[]) RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Typed batched edge-append (advisor plan 091): the 3-arg overload stamps the caller's edge_type
-- dictionary id on EVERY slot in the run (one type per call — the Wikidata loader groups its
-- staged edges by (src, type_id) and issues one call per group). Same C symbol as the 2-arg form
-- (PG_NARGS branches, the gph_insert_edge pattern); the 2-arg form is unchanged and defaults the
-- type to GPH_EDGE_TYPE_RELATED_TO (id 1), so every existing caller/bench is byte-identical.
-- Result parity: identical on-disk chains to N x gph_insert_edge(src, dst[i], type_id) fed in
-- array order. Owner-guarded (REVOKEd from PUBLIC below) like its siblings.
CREATE FUNCTION gph_insert_edges(bigint, bigint[], integer) RETURNS bigint
  AS 'MODULE_PATHNAME', 'gph_insert_edges' LANGUAGE C VOLATILE STRICT;

-- ----------------------------------------------------------------------------
-- Edge type dictionary (advisor plan 038): gBrain's typed link model (founded/
-- founded_by, works_at/employs, mentions, attended, ...) maps link-type NAMES to
-- the small integer ids stored natively in es_edge_type_id. The name<->id mapping
-- is RELATIONAL metadata (golden rule 3: topology is native, properties/dictionary
-- are relational side-tables); only the id lives in the native slot. Rides the SAME
-- WAL + host txn as the native pages (golden rule 2), so a rolled-back registration
-- rolls back with its edges. Built-in id 1 = related_to (GPH_EDGE_TYPE_RELATED_TO).
CREATE TABLE edge_type (
    id   int  PRIMARY KEY,
    name text NOT NULL UNIQUE
);
INSERT INTO edge_type (id, name) VALUES (1, 'related_to');
GRANT SELECT ON edge_type TO PUBLIC;

-- register_edge_type(name) RETURNS int — idempotent: returns the existing id if the name is
-- already registered, else allocates the next id (max+1) and inserts it. Owner-guarded
-- (REVOKEd from PUBLIC like the other mutators, plan 026 discipline). The loader/gBrain adapter
-- calls this once per link_type, then passes the returned id to gph_insert_edge(src, dst, id).
CREATE FUNCTION register_edge_type(p_name text) RETURNS int
LANGUAGE plpgsql VOLATILE STRICT
AS $$
DECLARE
    v_id int;
BEGIN
    SELECT id INTO v_id FROM graph_store.edge_type WHERE name = p_name;
    IF FOUND THEN
        RETURN v_id;
    END IF;
    SELECT COALESCE(max(id), 0) + 1 INTO v_id FROM graph_store.edge_type;
    INSERT INTO graph_store.edge_type (id, name) VALUES (v_id, p_name)
        ON CONFLICT (name) DO NOTHING;
    -- Re-read: a concurrent registration of the SAME name (contract-violating under the v1
    -- single-writer model) would have won the unique index; return the winner's id.
    SELECT id INTO v_id FROM graph_store.edge_type WHERE name = p_name;
    RETURN v_id;
END
$$;
REVOKE EXECUTE ON FUNCTION register_edge_type(text) FROM PUBLIC;

CREATE FUNCTION gph_neighbors(bigint) RETURNS SETOF bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Edge-emitting traversal (DEV-1165): one :related_to edge per Next(), so callers can surface the
-- edge endpoints and join dst back to its relational/vector payload (the canonical query's COLUMNS
-- projection). Use in a target-list / ProjectSet position (SELECT gph_traverse(x)), NOT a
-- FROM-clause FunctionScan, or early termination under LIMIT is lost. v1 edge slots carry no
-- stored edge id, so only (src, dst) are surfaced.
CREATE FUNCTION gph_traverse(bigint, OUT src bigint, OUT dst bigint) RETURNS SETOF record
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Typed + directional + source-scoped traversal (advisor plan 038 / DEV-1350; gBrain traversePaths).
-- Same one-edge-per-Next() TR-1 engine as gph_traverse; only the inline gs_getnext filters differ:
--   type_id   = dictionary edge type id, or 0 (GPH_EDGE_TYPE_ANY) for any type;
--   direction = 0 out (v1); in/both RAISE (reverse adjacency deferred — docs/decisions/0016);
--   source_id = source vid to scope to, or -1 for unscoped.
-- gph_traverse_typed(src, 1, 0, -1) is byte-identical to gph_traverse(src) (parity oracle). Read
-- surface stays open (matches gph_traverse). Use in a target-list / ProjectSet position, not a
-- FROM-clause FunctionScan, or early termination under LIMIT is lost.
CREATE FUNCTION gph_traverse_typed(bigint, integer, integer, bigint,
                                   OUT src bigint, OUT dst bigint) RETURNS SETOF record
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Fused multi-hop BFS (the gBrain graph-leg fast path): distinct vertices reachable from a seed vid
-- within max_depth out-hops, computed ENTIRELY in C (frontier + visited over the native adjacency) in
-- ONE call — the native counterpart to a relational recursive-CTE traversal. type_id 0 = any type.
CREATE FUNCTION gph_traverse_bfs(bigint, integer, integer) RETURNS SETOF bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

CREATE FUNCTION gph_visits() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

-- Per-backend adjacency-page-read counter (read-once scan probe): one increment per adjacency page
-- a traversal reads, NOT per neighbor emitted. Backend-local + monotonic; read DELTAS. Demonstrates
-- that a degree-D hub over P chained pages now costs ~P page reads instead of ~D.
CREATE FUNCTION gph_page_reads() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

CREATE FUNCTION gph_vertex_count() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

-- Store-wide directed-edge count (plan 006): the metapage gm_edge_count counter, the
-- avg_out_degree source for the FR-6 join-order heuristic. Raw (non-MVCC) INSERT counter — it is
-- bumped once per edge ever inserted and is NEVER decremented, including by gph_tombstone_edge/
-- gph_tombstone_vertex (plan 037): after any delete this OVERCOUNTS live topology (advisor plan
-- 055). Maintained under GenericXLog so aborts/crashes roll it back with the page image. Used by
-- the crash-recovery edge-count assertion; see gph_visible_edge_count() below for a delete-aware
-- (but O(vertices+edges) scanning) live count.
CREATE FUNCTION gph_edge_count() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

-- MVCC-visible, delete-aware directed-edge count (advisor plan 055): scans every vertex's
-- adjacency chain and counts only edge slots that are inserted-visible and not tombstoned. Reflects
-- gph_tombstone_edge/gph_tombstone_vertex immediately, unlike the raw gph_edge_count() above. Use
-- this when an exact live count is required; use gph_edge_count() for the O(1) raw upper bound.
CREATE FUNCTION gph_visible_edge_count() RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE;

-- gph_freeze(horizon) RETURNS bigint — manual anti-wraparound freeze pass (advisor plan 036 /
-- DEV-1347; design docs/graph_store_freeze_design_v0.1.0.md). Rewrites every stored inserting xid
-- that PRECEDES `horizon` to a permanent one (committed -> frozen/visible, aborted -> invalid/
-- invisible) WHILE it is still resolvable in clog, records gm_frozen_horizon, and advances the
-- container's relfrozenxid. Visibility is byte-identical before and after (pure storage rewrite).
-- Every page rewrite is GenericXLog'd in the caller's txn (one WAL, FR-7); the pass is idempotent
-- and returns the number of records frozen.
--
-- OPERATIONS — this is the v1 MANUAL freeze (the auto-freeze / table-AM stage is deferred, design
-- §3 "Later"). PostgreSQL's forced anti-wraparound autovacuum IGNORES autovacuum_enabled=false and
-- would eventually walk gstore's NON-heap pages as a heap; there is NO reliable way to make it SKIP
-- a heap-typed relation short of the full table-AM handler. The disarm is therefore INDIRECT: run
-- gph_freeze() to keep age(relfrozenxid) on gstore low so the forced vacuum never triggers. Monitor:
--     SELECT age(relfrozenxid) FROM pg_class WHERE oid = 'graph_store.gstore'::regclass;
-- and run  SELECT graph_store.gph_freeze(<a committed past xid>::xid);  well before it approaches
-- autovacuum_freeze_max_age. Run it in AUTOCOMMIT (its own transaction), exactly like VACUUM: the
-- relfrozenxid advance is vacuum's in-place (non-transactional) update, so a rolled-back freeze
-- would leave relfrozenxid advanced past un-frozen pages.
CREATE FUNCTION gph_freeze(horizon xid) RETURNS bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Native delete (plan 037 / DEV-1349). Soft-delete (tombstone) by setting GPH_FLAG_DELETED +
-- the deleting xid under GenericXLog, atomic with the host txn (FR-7); the read path already
-- filters visible tombstones so traversal stops emitting immediately. Idempotent (re-deleting or
-- deleting an absent edge/vertex is a no-op). Physical slot reclamation is deferred to plan 036's
-- freeze pass, so gm_edge_count is NOT decremented (raw monotone counter). gph_tombstone_vertex
-- also tombstones the vertex's OUT-edges (every type); dangling IN-edges are filtered at read time
-- (no reverse index in v1 — full reverse-sweep is plan 038).
--
-- Typed tombstone (advisor plan 045 / DEV-1354 follow-up): the 2-arg form defaults type_id to
-- GPH_EDGE_TYPE_RELATED_TO, so it ONLY tombstones :related_to edges between src/dst — before this
-- fix it matched on dst alone and silently wiped any co-located typed edge (plan 038) between the
-- same endpoints too. The 3-arg overload (same C symbol, PG_NARGS() branch, matching the
-- gph_insert_edge pattern) lets a caller pass an explicit dictionary type id, or
-- GPH_EDGE_TYPE_ANY (0) to explicitly request the old all-type wipe.
CREATE FUNCTION gph_tombstone_edge(bigint, bigint) RETURNS void
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

CREATE FUNCTION gph_tombstone_edge(bigint, bigint, integer) RETURNS void
  AS 'MODULE_PATHNAME', 'gph_tombstone_edge' LANGUAGE C VOLATILE STRICT;

CREATE FUNCTION gph_tombstone_vertex(bigint) RETURNS void
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- Containment (advisor plan 026): the container holds NON-heap pages; any heap-path access
-- (SELECT/VACUUM/ANALYZE) misreads them. Deployers grant gph_* EXECUTE to trusted roles only.
REVOKE ALL ON TABLE gstore FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_insert_vertex(), gph_insert_edge(bigint,bigint) FROM PUBLIC;
-- gph_freeze is a maintenance mutator (superuser/owner only, plan 026 discipline).
REVOKE EXECUTE ON FUNCTION gph_freeze(xid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_tombstone_edge(bigint,bigint), gph_tombstone_vertex(bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_tombstone_edge(bigint,bigint,integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_insert_edge(bigint,bigint,integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_insert_edges(bigint,bigint[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION gph_insert_edges(bigint,bigint[],integer) FROM PUBLIC;

-- ============================================================================
-- ADR-0013 Stage A (advisor plan 025): the external-id mapping layer + the v0
-- compat surface, hosted INSIDE the v1 extension so the operators and every
-- bench/test keep their arbitrary-bigint entity-id ergonomics while the native
-- AM keeps its dense-vid invariant (design doc §3, Option A).
--
-- The map is a plain heap side-table: it rides the SAME WAL and the SAME host
-- transaction as the native pages (golden rule 2 — no second txn manager), so
-- a rolled-back edge insert rolls back its map entries with it. Concurrency:
-- gph_upsert_vertex follows the single-writer contract of the v1 core
-- (graph_am.c header); under a concurrent-writer race the loser's freshly
-- allocated vid is left unmapped/orphaned (harmless — no edges reference it)
-- and the winner's mapping is returned.
-- ============================================================================
CREATE TABLE gph_vid_map (
    ext_id bigint PRIMARY KEY,   -- caller-chosen external entity id (arbitrary, sparse)
    vid    bigint NOT NULL UNIQUE -- dense v1 vid assigned by gph_insert_vertex()
);
-- Read surface stays open (matches gph_neighbors); mutation goes through
-- gph_upsert_vertex, which is REVOKEd below like the other mutators (plan 026).
GRANT SELECT ON gph_vid_map TO PUBLIC;

-- ----------------------------------------------------------------------------
-- Identity fast-path flag (advisor plan 033 / PERF-02). One-row metadata table
-- recording whether the id-map is currently the identity function: the vertices
-- were loaded DENSE and IN ID ORDER, so ext_id == vid for every mapped vertex
-- (native vids are dense/monotone from 0 — gph_page.h gm_next_vid). When ON,
-- gph_neighbors_ext skips BOTH map probes (forward ext->vid and per-neighbor
-- reverse vid->ext) and treats src/dst vids as external ids directly — turning
-- the O(out-degree) reverse-probe cost into O(0). OFF by default: sparse/real
-- ids break the identity and fall back to the map (the general case is plan 034).
-- Rides the SAME WAL/host txn as gph_vid_map (golden rule 2): a rolled-back load
-- rolls the flag back with it.
CREATE TABLE gph_am_meta (
    only_row      boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    identity_mode boolean NOT NULL DEFAULT false
);
INSERT INTO gph_am_meta (only_row, identity_mode) VALUES (true, false);
GRANT SELECT ON gph_am_meta TO PUBLIC;

-- gph_set_identity_mode(bool): the loader calls this ONLY after a VERIFIED
-- dense-in-order load (ext ids 0..N-1 materialized in id order). Setting it ON
-- when ext_id != vid would make gph_neighbors_ext return wrong ids — hence it is
-- REVOKEd from PUBLIC like the other mutators (plan 026 discipline).
-- DEV-1352 latent guard: setting identity_mode ON while the map is NON-identity (any ext_id <> vid)
-- would make gph_neighbors_ext return WRONG ids (the identity fast-path treats src/dst as vids). The
-- loader only flips it on after a dense-in-order load (ext_id == vid), so refuse the corrupting case
-- outright rather than trust the caller — a mismatched map RAISES instead of silently mis-mapping
-- reads at scale. Turning it OFF is always allowed (the safe direction).
CREATE FUNCTION gph_set_identity_mode(p_on boolean) RETURNS void
LANGUAGE plpgsql VOLATILE STRICT
AS $$
BEGIN
    IF p_on AND EXISTS (SELECT 1 FROM graph_store.gph_vid_map WHERE ext_id <> vid) THEN
        RAISE EXCEPTION 'gph_set_identity_mode(true) refused: id map is non-identity '
            '(a row has ext_id <> vid) — the dense-in-order load precondition is violated (DEV-1352). '
            'Reads would return wrong ids under the identity fast-path.';
    END IF;
    UPDATE graph_store.gph_am_meta SET identity_mode = p_on WHERE only_row;
END
$$;
REVOKE EXECUTE ON FUNCTION gph_set_identity_mode(boolean) FROM PUBLIC;

-- gph_upsert_vertex(ext_id) RETURNS bigint — THE id-mapping layer (ADR-0013).
-- Returns the dense vid mapped to ext_id, creating the vertex + mapping on first use.
CREATE FUNCTION gph_upsert_vertex(p_ext bigint) RETURNS bigint
LANGUAGE plpgsql VOLATILE STRICT
AS $$
DECLARE
    v bigint;
BEGIN
    SELECT m.vid INTO v FROM graph_store.gph_vid_map m WHERE m.ext_id = p_ext;
    IF FOUND THEN
        RETURN v;
    END IF;
    v := graph_store.gph_insert_vertex();
    INSERT INTO graph_store.gph_vid_map (ext_id, vid) VALUES (p_ext, v)
        ON CONFLICT (ext_id) DO NOTHING;
    -- lost a (contract-violating) concurrent race: return the winner's vid; our freshly
    -- allocated vid stays unmapped and edge-less (harmless orphan). SHARPER EDGE (Linus
    -- review): under REPEATABLE READ the re-SELECT runs on the txn snapshot and could MISS
    -- a concurrent winner committed after our snapshot -> returns NULL. Both cases are
    -- excluded by the v1 single-writer contract (graph_am.c) and deferred to DIRECTION-04
    -- (concurrent/incremental ingest); disclosed in benchmark_sm2_1m_v0.3.0.md honesty box.
    SELECT m.vid INTO v FROM graph_store.gph_vid_map m WHERE m.ext_id = p_ext;
    RETURN v;
END
$$;

-- gph_neighbors_ext(ext_id) RETURNS SETOF bigint — traversal over EXTERNAL ids:
-- translate ext_id -> vid, walk the native adjacency chain, translate each
-- emitted dst vid back to its external id. This is the probe the tjs/tjs_open
-- operators SPI-call (Stage A). STRICT + unknown ext_id => empty set (matches
-- the v0 neighbors() contract for absent vertices). The per-row scalar lookup
-- preserves the storage emission order.
--
-- IDENTITY FAST-PATH (plan 033): when gph_am_meta.identity_mode is ON the CASE
-- guards short-circuit BOTH the forward probe (src is already the vid) and the
-- per-neighbor reverse probe (nvid is already the ext id), so a degree-D hub costs
-- 0 map descents instead of D. gph_neighbors stays the driving row source in BOTH
-- modes, so the storage emission order is byte-identical (the map path is the exact
-- pre-033 body under the ELSE). meta is a single row: the implicit-lateral cross
-- join yields gph_neighbors's rows in gph_neighbors's order. OFF is a no-op.
CREATE FUNCTION gph_neighbors_ext(src bigint) RETURNS SETOF bigint
LANGUAGE sql VOLATILE STRICT
AS $$
    SELECT CASE WHEN meta.identity_mode
                THEN n.nvid
                ELSE (SELECT m.ext_id FROM graph_store.gph_vid_map m WHERE m.vid = n.nvid)
           END
    FROM graph_store.gph_am_meta meta,
         graph_store.gph_neighbors(
             CASE WHEN meta.identity_mode
                  THEN src
                  ELSE (SELECT m2.vid FROM graph_store.gph_vid_map m2 WHERE m2.ext_id = src)
             END
         ) AS n(nvid)
$$;

-- gph_neighbors_ext_cached(ext_id) RETURNS SETOF bigint — byte-identical twin of
-- gph_neighbors_ext (same traversal, order, and lenient absent/unmapped contract),
-- but the per-neighbor reverse vid -> ext_id translation hits a backend-local hash
-- (~50ns) instead of a correlated btree + SPI subquery (~1us). The cache is loaded
-- lazily on first probe from gph_vid_map and flushed by a relcache-invalidation hook;
-- correct under the v1 single-writer bulk-load-then-query contract (plan 034 / DEV-1345,
-- PERF-03; see the header comment in graph_am.c). This is the probe the TJS operator's
-- reachable-set resolution (graphReachableT) SPI-calls instead of gph_neighbors_ext.
-- Also honors gph_am_meta.identity_mode (plan 047 / DEV-1354 follow-up): reads the flag
-- ONCE per Open (graph_am.c gph_read_identity_mode) and, when ON, skips BOTH the forward
-- map probe and the reverse hash lookup exactly like gph_neighbors_ext's CASE guards above
-- — before plan 047 this C twin ALWAYS hit gph_vid_map regardless of identity_mode, so
-- under identity ON with an empty/incomplete map it silently returned empty where the SQL
-- shim returned full adjacency (the bug this plan fixes).
CREATE FUNCTION gph_neighbors_ext_cached(bigint) RETURNS SETOF bigint
  AS 'MODULE_PATHNAME' LANGUAGE C VOLATILE STRICT;

-- ----------------------------------------------------------------------------
-- v0-compat front door (identical SQL signatures to src/graph_store_ext), so
-- every existing consumer keeps working with ONLY its CREATE EXTENSION line
-- changed. This is the permanent public surface; gph_* stays the native layer.
-- ----------------------------------------------------------------------------

-- add_edge(src, dst): v0 ergonomics (arbitrary bigint ids, vertices auto-created)
-- over the native AM: upsert both endpoints through the map, then insert the edge.
CREATE FUNCTION add_edge(src bigint, dst bigint) RETURNS void
LANGUAGE sql VOLATILE
AS $$
    SELECT graph_store.gph_insert_edge(graph_store.gph_upsert_vertex(src),
                                       graph_store.gph_upsert_vertex(dst));
$$;

-- remove_edge(src, dst): v0-ergonomic tombstone of the src->dst edge over EXTERNAL ids (the
-- removeLink twin of add_edge; plan 037). Translates both endpoints through the id map — but does
-- NOT auto-create: a remove naming an unmapped endpoint yields NULL, and the STRICT
-- gph_tombstone_edge then no-ops (removing an absent edge is a no-op). Honors the plan-033 identity
-- fast-path exactly like gph_neighbors_ext (the paired read surface): when identity_mode is ON,
-- ext_id == vid so src/dst pass straight through. Rides the SAME host txn/WAL (golden rule 2), so a
-- rolled-back remove rolls the tombstone back with it (FR-7).
CREATE FUNCTION remove_edge(src bigint, dst bigint) RETURNS void
LANGUAGE sql VOLATILE
AS $$
    SELECT graph_store.gph_tombstone_edge(
        CASE WHEN (SELECT identity_mode FROM graph_store.gph_am_meta)
             THEN src ELSE (SELECT m.vid FROM graph_store.gph_vid_map m WHERE m.ext_id = src) END,
        CASE WHEN (SELECT identity_mode FROM graph_store.gph_am_meta)
             THEN dst ELSE (SELECT m.vid FROM graph_store.gph_vid_map m WHERE m.ext_id = dst) END);
$$;

-- neighbors(src): v0-compat name for the external-id traversal.
CREATE FUNCTION neighbors(src bigint) RETURNS SETOF bigint
LANGUAGE sql VOLATILE STRICT
AS $$ SELECT graph_store.gph_neighbors_ext(src) $$;

-- visits(): v0-compat name for the TR-1 traversal-step probe.
CREATE FUNCTION visits() RETURNS bigint
LANGUAGE sql VOLATILE
AS $$ SELECT graph_store.gph_visits() $$;

-- Mutator containment (plan 026 discipline).
REVOKE EXECUTE ON FUNCTION gph_upsert_vertex(bigint), add_edge(bigint, bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION remove_edge(bigint, bigint) FROM PUBLIC;

-- ============================================================================
-- SQL/PGQ canonical surface (DEV-1167 / FR-4 "one plan"). The FRONT DOOR that lowers
-- the ONE canonical query (spec §5) into a single tjs() call (DEV-1169). Folded into
-- this extension rather than a third extension (the lowering depends on this graph
-- store's reachability iterator and on the vectordb tjs() operator at runtime).
--
-- WHY a whole-statement text argument, not `FROM GRAPH_TABLE(...)` in the user's SQL:
--   1. Stock PG 13.4 does NOT parse the verbatim canonical MATCH payload. The tokens
--      `(:label)` and `-[:related_to]->` are not valid SQL expression grammar, so a bare
--      `GRAPH_TABLE( MATCH (src:entity)-[:related_to]->... )` raises `syntax error at or
--      near ":"` BEFORE any TriDB code runs (verified on tridb/msvbase:dev, 2026-06-25).
--      The only no-grammar-fork way to carry the payload verbatim is as a string literal.
--   2. The canonical `ORDER BY src_embedding <-> :q LIMIT :k` MUST live INSIDE the operator.
--      MSVBASE's scalar `<->` returns 0 outside an HNSW index scan (ADR-0006); an outer
--      ORDER BY would be a blocking sort over garbage distances — forfeiting TR-1. So the
--      front door owns the whole statement (WHERE + ORDER BY + LIMIT), not just the MATCH.
--
-- The surface is a single set-returning function taking the FULL canonical statement text
-- (with the three `:params` substituted to literals). It (a) validates against the single
-- canonical template — anything off-template RAISES (scope guard: golden rule 4), (b)
-- extracts (src vertex, k, timestamp filter, query vector), (c) lowers to ONE tjs() call.
--
-- Argument mapping canonical -> tjs(table_name,k,term_cond,src,attr_exp,filter_exp,orderby_exp):
--   table_name='entities'; k=LIMIT; term_cond=0; src=pinned WHERE src.id=<N>;
--   attr_exp='id, chunk' (1st col MUST be the candidate graph id per tjs contract);
--   filter_exp=the timestamp predicate; orderby_exp='embedding <-> ''<vector>''' (dst embedding).
--
-- STOCK-PG lowering (advisor plan 075 / ADR-0019 addendum): when the fork tjs() is absent, the
-- SAME pinned template lowers to src/tjs_pg's
--   tjs_open(regclass,k,term_cond,m_seeds,hops,id_col,filter,query vector,src,edge_type)
--   = ('entities', k=LIMIT, term_cond=0, m_seeds=0, hops=1, 'id', ts filter, parsed vector,
--      pinned src.id, edge_type = the graph_store.edge_type catalog id of the canonical label
--      'related_to' — never "any edge").
-- tjs_open returns entity ids; they are joined back to `entities` for the canonical chunk
-- column in the operator's OWN emit order (WITH ORDINALITY — never heap order after the join).
--
-- SRC-BINDING note (surface<->operator contract): the canonical MATCH binds `src` as a
-- pattern VARIABLE (a SET of sources), but tjs() takes ONE `src bigint`. The runnable v1
-- oracle (test/trimodal_compose.sql, test/canonical_e2e_test.sql) pins a single src vertex.
-- v1 therefore REQUIRES the canonical WHERE to pin `src.id = <const>` (documented v1 binding,
-- not a generalization). A src-set surface is a v-next concern. ORDER BY src_embedding is
-- mapped onto the dst `embedding` column (tjs's only ordered stream is the dst HNSW scan; a
-- single pinned src has a constant embedding that cannot rank). See ADR-0008.
-- ============================================================================
-- VOLATILE (not STABLE): the Stage-2 join-order decision below records itself via
-- set_config (session-local), a side effect a STABLE contract would misdeclare.
CREATE FUNCTION graph_store.graph_query(canonical_sql text)
RETURNS SETOF text
LANGUAGE plpgsql
VOLATILE
AS $fn$
DECLARE
    q            text;
    m            text[];
    src_id       bigint;
    k_val        int;
    ts_filter    text;
    query_vec    text;
    tbl_size     bigint;
    est_matches  bigint;
    jorder       text;
    plan_json    text;
    etype        int;
    stock_ok     boolean := false;
BEGIN
    -- Normalize: collapse whitespace runs to single spaces, trim. Keeps the matcher a single
    -- fixed template regardless of how the caller line-wrapped the canonical query.
    q := btrim(regexp_replace(canonical_sql, '\s+', ' ', 'g'));

    -- SCOPE GUARD: validate the ONE canonical template. Off-template variants (wrong
    -- projection, wrong edge label, wrong hop count, missing <->, missing LIMIT, ...) do not
    -- match this anchored, case-insensitive template and fall through to the RAISE below.
    -- Capture: 1=src_id, 2=ts filter body, 3=query_vec, 4=k.
    m := regexp_match(
        q,
        '^SELECT\s+chunk\s+'
        || 'FROM\s+GRAPH_TABLE\s*\(\s*'
        ||   'MATCH\s+\(\s*src\s*:\s*entity\s*\)\s*-\s*\[\s*:\s*related_to\s*\]\s*->\s*\(\s*dst\s*:\s*entity\s*\)\s*'
        ||   'COLUMNS\s*\(\s*'
        ||     'src\.embedding\s+AS\s+src_embedding\s*,\s*'
        ||     'dst\.chunk\s+AS\s+chunk\s*,\s*'
        ||     'dst\.timestamp\s+AS\s+timestamp\s*'
        ||   '\)\s*\)\s+'
        -- src_id capture is (\d+): real entity ids are non-negative, and a negative src would
        -- make FR-6's filter_first body reject it AFTER lowering (ANALYZE-dependent errors).
        -- The graph-disabled src=-1 parity case stays available via DIRECT tjs() calls only
        -- (advisor plan 024).
        || 'WHERE\s+src\.id\s*=\s*(\d+)\s+AND\s+timestamp\s+IN\s*\((\d+(?:\s*,\s*\d+)*)\)\s+'
        -- Vector literal: the fork brace dialect ('{...}', float8[] form) OR the pgvector
        -- bracket dialect ('[...]'); delimiters must match. Each lowering below converts the
        -- accepted literal to its engine's dialect (plan 075) — this dialect pair is the ONLY
        -- admitted widening; no other template expansion.
        || 'ORDER\s+BY\s+src_embedding\s*<->\s*''(\{[^'']+\}|\[[^'']+\])''\s+'
        || 'LIMIT\s+(\d+)\s*;?\s*$',
        'i'
    );

    IF m IS NULL THEN
        RAISE EXCEPTION 'graph_query: off-template query rejected (scope guard). '
            'TriDB v1 accepts ONLY the single canonical query (spec §5): '
            'SELECT chunk FROM GRAPH_TABLE(MATCH (src:entity)-[:related_to]->(dst:entity) '
            'COLUMNS(src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp)) '
            'WHERE src.id = <n> AND timestamp IN (<window>) '
            'ORDER BY src_embedding <-> ''<vector>'' LIMIT <k>. Got: %', q;
    END IF;

    src_id    := m[1]::bigint;
    -- Build the filter against the PHYSICAL column directly (the canonical `timestamp` is aliased
    -- from dst.timestamp; the backing relation column is `ts`). m[2] is integers-and-commas only
    -- (the scope-guard regex), so this is a closed value set, not an injection surface.
    ts_filter := 'ts IN (' || m[2] || ')';
    query_vec := m[3];
    k_val     := m[4]::int;

    -- Bound k too (the guard validates params, not just shape): tjs's top-k heap is sized by k.
    IF k_val < 1 OR k_val > 10000 THEN
        RAISE EXCEPTION 'graph_query: LIMIT out of range (got %, allowed 1..10000)', k_val;
    END IF;

    -- JOIN-ORDER DECISION (ADR-0011 Stage 2, DEV-1285/FR-6). Build the LegStats inputs the
    -- FROZEN decision core needs and record the chosen order BEFORE lowering:
    --   table_size        = pg_class.reltuples (0 when never ANALYZEd -> the FROZEN
    --                       "selectivity 1.0 -> vector_first" safe default),
    --   rel_filter_matches = the planner's own row estimate for the canonical WHERE via
    --                       EXPLAIN (= clauselist_selectivity x reltuples — the exact
    --                       estimator ADR-0011 names, reached without planner-C plumbing).
    -- The decision is recorded via graph_store.last_join_order() (Option B's EXPLAIN-
    -- visibility companion at the lowering level) and — on a DEV-1290 engine — passed into
    -- tjs() as the join_order argument at the lowering below, where it selects the physical
    -- body (assert on the operator-level tjs_last_join_order()).
    SELECT reltuples::bigint INTO tbl_size FROM pg_class WHERE oid = 'entities'::regclass;

    EXECUTE 'EXPLAIN (FORMAT JSON) SELECT 1 FROM entities WHERE ' || ts_filter
        INTO plan_json;
    est_matches := floor((plan_json::json -> 0 -> 'Plan' ->> 'Plan Rows')::float8)::bigint;

    IF to_regprocedure('tridb_choose_join_order(bigint,bigint,float8)') IS NOT NULL THEN
        jorder := tridb_choose_join_order(est_matches, tbl_size);
    ELSE
        -- decision core (join_order extension) not installed: today's only physical path.
        jorder := 'vector_first';
    END IF;
    PERFORM set_config('graph_store.last_join_order', jorder, false);

    -- LOWERING: one tjs(...) call. attr_exp's first column is `id` (the candidate graph id tjs
    -- probes); chunk is the second projected column we return. On a DEV-1290 engine the Stage-2
    -- decision above is PASSED INTO the operator (ADR-0011 Stage 4) and selects the physical
    -- body: vector_first (dst HNSW scan is the rank authority, timestamp predicate pushed into
    -- its WHERE, graph as predicate) or filter_first (graph drain drives, exact rank). On an
    -- older engine (no 8-arg tjs) the decision stays recorded-but-inert and the hardwired
    -- vector-first body runs — same answers, pre-DEV-1290 behavior.
    -- (Both fork branches take the fork's brace dialect: an accepted bracket literal is
    -- converted with translate(); a brace literal passes through unchanged. Plan 075.)
    IF to_regprocedure('tjs(text,integer,integer,bigint,text,text,text,text)') IS NOT NULL THEN
        RETURN QUERY EXECUTE format(
            'SELECT t.chunk FROM tjs(%L, %s, 0, %s::bigint, %L, %L, %L, %L) AS t(id bigint, chunk text)',
            'entities', k_val, src_id, 'id, chunk', ts_filter,
            'embedding <-> ''' || translate(query_vec, '[]', '{}') || '''',
            jorder                                    -- the Stage-2 FR-6 decision, now binding
        );
    ELSIF to_regprocedure('tjs(text,integer,integer,bigint,text,text,text)') IS NOT NULL THEN
        RETURN QUERY EXECUTE format(
            'SELECT t.chunk FROM tjs(%L, %s, 0, %s::bigint, %L, %L, %L) AS t(id bigint, chunk text)',
            'entities', k_val, src_id, 'id, chunk', ts_filter,
            'embedding <-> ''' || translate(query_vec, '[]', '{}') || ''''
        );
    ELSE
        -- STOCK-PG lowering (advisor plan 075 / ADR-0019): no fork tjs() — target the re-homed
        -- operator public.tjs_open (src/tjs_pg) IF its exact signature is installed. Detection
        -- is catalog-SAFE: probing to_regprocedure with `vector` in the signature can error on
        -- installs where the pgvector type is absent, so gate on to_regtype('vector') (NULL-safe)
        -- and the tjs_pg extension row FIRST, and only then resolve the exact signature.
        IF to_regtype('vector') IS NOT NULL
           AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'tjs_pg') THEN
            stock_ok := to_regprocedure(
                'public.tjs_open(regclass,integer,integer,integer,integer,text,text,vector,bigint,integer)'
            ) IS NOT NULL;
        END IF;
        IF NOT stock_ok THEN
            RAISE EXCEPTION 'graph_query: no compatible lowering target installed. The canonical '
                'query lowers to the fork tjs() operator (vectordb) or the stock tjs_open() '
                'operator (CREATE EXTENSION tjs_pg, requires pgvector >= 0.8 + graph_store_am). '
                'See ADR-0019.';
        END IF;

        -- Catalog-justified edge type: the canonical label `related_to` is resolved through the
        -- graph_store.edge_type dictionary (seeded id 1 at CREATE EXTENSION; the 2-arg
        -- add_edge/gph_insert_edge default). A missing row RAISES — the lowering never silently
        -- widens the typed traversal to "any edge".
        SELECT id INTO etype FROM graph_store.edge_type WHERE name = 'related_to';
        IF etype IS NULL THEN
            RAISE EXCEPTION 'graph_query: canonical edge label related_to has no id in '
                'graph_store.edge_type (catalog row removed?); cannot lower the typed traversal';
        END IF;

        -- With the v1 pinned src, tjs_open ALWAYS runs its FILTER-FIRST body (typed BFS reach ->
        -- relational filter -> exact rank; src IS NOT NULL selects it — src/tjs_pg contract).
        -- Record the physical truth, overriding the Stage-2 default above: there is no
        -- vector-first body to choose on this path.
        jorder := 'filter_first';
        PERFORM set_config('graph_store.last_join_order', jorder, false);

        -- Argument mapping (spec §5 -> tjs_open): k=LIMIT, term_cond=0, m_seeds=0 (ignored by
        -- filter-first), hops=1 (single-hop template), id_col='id', filter=the parsed window,
        -- query=the parsed vector (brace dialect converted to pgvector brackets, passed as a
        -- bound PARAMETER, cast-validated by ::vector), src=pinned src.id, edge_type=the catalog
        -- id above. tjs_open emits ranked entity ids; join back to the pinned relation for the
        -- canonical chunk column, preserving the operator's emit order via WITH ORDINALITY.
        RETURN QUERY EXECUTE format(
            'SELECT e.%I::text FROM public.tjs_open(%L::regclass, %s, 0, 0, 1, %L, %L, '
            '$1::vector, %s::bigint, %s) WITH ORDINALITY AS t(id, ord) '
            'JOIN %I e ON e.%I = t.id ORDER BY t.ord',
            'chunk', 'entities', k_val, 'id', ts_filter, src_id, etype,
            'entities', 'id')
        USING translate(query_vec, '{}', '[]');
    END IF;
    RETURN;
END;
$fn$;

COMMENT ON FUNCTION graph_store.graph_query(text) IS
    'TriDB SQL/PGQ canonical surface (DEV-1167). Lowers the ONE canonical query (spec §5) to a '
    'single fused-operator call: the fork tjs() when installed, else the stock tjs_open() '
    '(tjs_pg, ADR-0019 / plan 075). Off-template queries RAISE (scope guard: one canonical '
    'query for v1). Requires WHERE to pin src.id = <const> (v1 single-src binding). See ADR-0008.';

-- last_join_order(): what the Stage-2 lowering decided for the MOST RECENT graph_query()
-- call in this session ('filter_first' / 'vector_first'), or NULL before the first call.
-- This is the lowering-level half of ADR-0011's Option-B observability tax; the operator-level
-- tjs_last_join_order() companion (DEV-1290 engines) reports which body actually RAN.
CREATE FUNCTION graph_store.last_join_order()
RETURNS text
LANGUAGE sql
STABLE
AS $$ SELECT current_setting('graph_store.last_join_order', true) $$;

COMMENT ON FUNCTION graph_store.last_join_order() IS
    'Join order chosen by the Stage-2 lowering (ADR-0011/DEV-1285) for the most recent '
    'graph_store.graph_query() call in this session; NULL before the first call. On DEV-1290 '
    'engines the decision is passed into tjs() and selects the physical body (see '
    'tjs_last_join_order() for what actually ran); on older engines it is recorded but inert.';

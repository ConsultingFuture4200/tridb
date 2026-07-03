-- graph_store extension v0 — native adjacency-list graph store.
-- complain loudly if loaded outside CREATE EXTENSION
\echo Use "CREATE EXTENSION graph_store" to load this file. \quit

CREATE SCHEMA graph_store;

-- Adjacency list: one row per vertex, out-neighbors co-located in nbrs[].
-- This is adjacency-list storage, NOT an edge join table.
CREATE TABLE graph_store.adjacency (
    vid  bigint PRIMARY KEY,
    nbrs bigint[] NOT NULL DEFAULT '{}'
);

-- add_edge(src, dst): append dst to src's adjacency list (upsert).
CREATE FUNCTION graph_store.add_edge(src bigint, dst bigint)
RETURNS void
LANGUAGE sql
AS $$
    INSERT INTO graph_store.adjacency (vid, nbrs)
    VALUES (src, ARRAY[dst])
    ON CONFLICT (vid)
    DO UPDATE SET nbrs = graph_store.adjacency.nbrs || EXCLUDED.nbrs;
$$;

-- neighbors(src): Open/Next/Close traversal iterator over src's out-neighbors.
CREATE FUNCTION graph_store.neighbors(src bigint)
RETURNS SETOF bigint
AS 'MODULE_PATHNAME', 'graph_neighbors'
LANGUAGE C STRICT;

-- visits(): session traversal-step counter — proves TR-1 early termination.
CREATE FUNCTION graph_store.visits()
RETURNS bigint
AS 'MODULE_PATHNAME', 'graph_visits'
LANGUAGE C;

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
        || 'ORDER\s+BY\s+src_embedding\s*<->\s*''(\{[^'']+\})''\s+'
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
    IF to_regprocedure('tjs(text,integer,integer,bigint,text,text,text,text)') IS NOT NULL THEN
        RETURN QUERY EXECUTE format(
            'SELECT t.chunk FROM tjs(%L, %s, 0, %s::bigint, %L, %L, %L, %L) AS t(id bigint, chunk text)',
            'entities', k_val, src_id, 'id, chunk', ts_filter,
            'embedding <-> ''' || query_vec || '''',
            jorder                                    -- the Stage-2 FR-6 decision, now binding
        );
    ELSE
        RETURN QUERY EXECUTE format(
            'SELECT t.chunk FROM tjs(%L, %s, 0, %s::bigint, %L, %L, %L) AS t(id bigint, chunk text)',
            'entities', k_val, src_id, 'id, chunk', ts_filter,
            'embedding <-> ''' || query_vec || ''''
        );
    END IF;
    RETURN;
END;
$fn$;

COMMENT ON FUNCTION graph_store.graph_query(text) IS
    'TriDB SQL/PGQ canonical surface (DEV-1167). Lowers the ONE canonical query (spec §5) to a '
    'single tjs() call. Off-template queries RAISE (scope guard: one canonical query for v1). '
    'Requires WHERE to pin src.id = <const> (v1 single-src binding). See ADR-0008.';

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

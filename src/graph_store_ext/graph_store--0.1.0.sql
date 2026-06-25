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
CREATE FUNCTION graph_store.graph_query(canonical_sql text)
RETURNS SETOF text
LANGUAGE plpgsql
STABLE
AS $fn$
DECLARE
    q            text;
    m            text[];
    src_id       bigint;
    k_val        int;
    ts_filter    text;
    query_vec    text;
    rec          record;
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
        || 'WHERE\s+src\.id\s*=\s*(-?\d+)\s+AND\s+timestamp\s+IN\s*\(([^)]+)\)\s+'
        || 'ORDER\s+BY\s+src_embedding\s*<->\s*''(\{[^'']*\})''\s+'
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
    ts_filter := 'timestamp IN (' || m[2] || ')';
    query_vec := m[3];
    k_val     := m[4]::int;

    -- LOWERING: one tjs(...) call. The vector leg (dst HNSW scan) is the sole rank authority;
    -- the timestamp predicate is pushed into its WHERE; the graph leg is the reachability
    -- predicate on src. attr_exp's first column is `id` (the candidate graph id tjs probes);
    -- chunk is the second projected column we return. The canonical column name is `timestamp`;
    -- the backing relation column is `ts` (COLUMNS aliases dst.timestamp AS timestamp), so the
    -- canonical predicate is mapped onto the physical `ts` column here.
    FOR rec IN
        EXECUTE format(
            'SELECT t.chunk FROM tjs(%L, %s, 0, %s::bigint, %L, %L, %L) AS t(id bigint, chunk text)',
            'entities',                                   -- table_name
            k_val,                                        -- k
            src_id,                                       -- src
            'id, chunk',                                  -- attr_exp (1st col = candidate id)
            replace(ts_filter, 'timestamp', 'ts'),        -- filter_exp (canonical->physical col)
            'embedding <-> ''' || query_vec || ''''       -- orderby_exp (dst embedding)
        )
    LOOP
        RETURN NEXT rec.chunk;
    END LOOP;
    RETURN;
END;
$fn$;

COMMENT ON FUNCTION graph_store.graph_query(text) IS
    'TriDB SQL/PGQ canonical surface (DEV-1167). Lowers the ONE canonical query (spec §5) to a '
    'single tjs() call. Off-template queries RAISE (scope guard: one canonical query for v1). '
    'Requires WHERE to pin src.id = <const> (v1 single-src binding). See ADR-0008.';

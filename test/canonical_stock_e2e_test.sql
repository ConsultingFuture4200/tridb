-- canonical_stock_e2e_test.sql — the ONE canonical query (spec §5) end-to-end on STOCK
-- PG 16/17 (advisor plan 075). Proves graph_store.graph_query() lowers the pinned template
-- to the stock fused operator public.tjs_open (src/tjs_pg, ADR-0019) and returns the
-- canonical chunks in the operator's emit order. Runs via scripts/pg17_graph_test.sh
-- (stock PG + pgvector + graph_store_am + tjs_pg); psql -v ON_ERROR_STOP=1, so any
-- RAISE EXCEPTION fails the suite.
--
-- Corpus mirrors the fork canonical suites (test/parse_canonical.sql /
-- test/canonical_e2e_test.sql): entity k has embedding [k,0,...]; entity 40 is stale
-- (ts 999); src 1 -related_to-> {10,20,30,40}. PLUS a typed decoy this suite adds:
-- 1 -knows_about-> 15 (|15-19|=4, NEARER to q=19 than 10 at |10-19|=9) — if the lowering
-- traversed "any edge" instead of the catalog-mapped related_to type, 15 would displace
-- 10 in the top-2 and the exact-array assertions below would fail.

CREATE EXTENSION vector;
CREATE EXTENSION graph_store_am;   -- tjs_pg is created LATER (assertion 0a runs without it)

CREATE TABLE entities (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding vector(8)
);
INSERT INTO entities
SELECT k,
       'chunk ' || k,
       CASE WHEN k = 40 THEN 999 ELSE 100 END,
       ('[' || k || ',0,0,0,0,0,0,0]')::vector(8)
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops)
    WITH (m = 16, ef_construction = 64);

-- graph: dense vids 0..2000 in order (ext id == vid — tjs_open's filter-first BFS joins
-- graph vids straight against entities.id, same precondition as test/tjs_pg_test.sql).
DO $$
DECLARE g int; v bigint;
BEGIN
    FOR g IN 0..2000 LOOP
        v := graph_store.gph_upsert_vertex(g);
        IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;
    END LOOP;
END $$;

-- add_edge's 2-arg form writes the BUILT-IN edge type 1 = 'related_to' (the canonical label,
-- seeded in graph_store.edge_type at CREATE EXTENSION) — same fixture calls as the fork suites.
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);
-- the typed decoy: a knows_about edge the canonical query must NOT traverse
SELECT set_config('t.ktype', graph_store.register_edge_type('knows_about')::text, false);
SELECT graph_store.gph_insert_edge(1, 15, current_setting('t.ktype')::int);

-- ===========================================================================
-- ASSERTION 0a: tjs_pg ABSENT -> the canonical wrapper fails CLOSED with the explicit
-- no-compatible-lowering error (a clear install hint), not an incidental
-- "function tjs(...) does not exist" from a fork-only lowering.
-- ===========================================================================
DO $$
DECLARE raised boolean := false;
BEGIN
    BEGIN
        PERFORM graph_store.graph_query($q$
            SELECT chunk
            FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
              COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
            WHERE src.id = 1 AND timestamp IN (100)
            ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'
            LIMIT 2
        $q$);
    EXCEPTION WHEN others THEN
        raised := true;
        IF SQLERRM NOT LIKE '%no compatible lowering%' THEN
            RAISE EXCEPTION 'tjs_pg-absent error is not the explicit lowering error: %', SQLERRM;
        END IF;
    END;
    IF NOT raised THEN
        RAISE EXCEPTION 'canonical query without tjs_pg did NOT raise';
    END IF;
    RAISE NOTICE 'PASS 0a: tjs_pg absent -> explicit no-compatible-lowering error';
END $$;

-- ===========================================================================
-- ASSERTION 0b: detection is CATALOG-SAFE without pgvector. In a database with ONLY
-- graph_store_am (no vector type at all), the wrapper must reach the SAME explicit
-- lowering error — not a 'type "vector" does not exist' from the signature probe.
-- ===========================================================================
CREATE DATABASE novec;
\connect novec
CREATE EXTENSION graph_store_am;
CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int);
DO $$
DECLARE raised boolean := false;
BEGIN
    BEGIN
        PERFORM graph_store.graph_query($q$
            SELECT chunk
            FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
              COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
            WHERE src.id = 1 AND timestamp IN (100)
            ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'
            LIMIT 2
        $q$);
    EXCEPTION WHEN others THEN
        raised := true;
        IF SQLERRM NOT LIKE '%no compatible lowering%' THEN
            RAISE EXCEPTION 'no-pgvector detection not catalog-safe, got: %', SQLERRM;
        END IF;
    END;
    IF NOT raised THEN
        RAISE EXCEPTION 'canonical query without pgvector did NOT raise';
    END IF;
    RAISE NOTICE 'PASS 0b: no pgvector -> same explicit error (catalog-safe detection)';
END $$;
\connect postgres

-- the stock operator arrives; everything below exercises the REAL lowering
CREATE EXTENSION tjs_pg;

-- ===========================================================================
-- ASSERTION 1: the canonical query returns the exact chunks IN OPERATOR ORDER.
-- reach(1, related_to) = {10,20,30,40}; window IN (100) drops 40; distances to q=[19,...]:
-- 20->1, 10->9, 30->11 => top-2 = ['chunk 20','chunk 10'] (exact array = order asserted).
-- The knows_about decoy 15 (dist 4) must NOT appear — typed traversal, not "any edge".
-- ===========================================================================
DO $$
DECLARE got text[];
BEGIN
    SELECT array_agg(c ORDER BY ord) INTO got
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'
        LIMIT 2
    $q$) WITH ORDINALITY AS t(c, ord);
    IF got IS DISTINCT FROM ARRAY['chunk 20','chunk 10'] THEN
        RAISE EXCEPTION 'canonical stock top-2 FAILED: got % (expected {chunk 20,chunk 10}; '
            'chunk 15 present would mean the typed edge mapping leaked)', got;
    END IF;
    RAISE NOTICE 'PASS 1: canonical query on stock -> [chunk 20, chunk 10] (exact order, typed edge honored)';
END $$;

-- ===========================================================================
-- ASSERTION 1b: the lowering records its physical join order: with the v1 pinned src,
-- tjs_open always runs FILTER-FIRST, and last_join_order() must say so.
-- ===========================================================================
DO $$
BEGIN
    IF graph_store.last_join_order() IS DISTINCT FROM 'filter_first' THEN
        RAISE EXCEPTION 'last_join_order = % (expected filter_first on the stock lowering)',
            graph_store.last_join_order();
    END IF;
    RAISE NOTICE 'PASS 1b: last_join_order() = filter_first';
END $$;

-- ===========================================================================
-- ASSERTION 2: direct/canonical PARITY on the same fixture. A direct public.tjs_open call
-- with the exact lowered arguments (k=5, term_cond=0, m_seeds=0, hops=1, id_col='id',
-- filter=ts IN (100), src=1, edge_type=catalog id of related_to) must emit the SAME ordered
-- ids the canonical wrapper returns — and both must be the oracle [20,10,30].
-- ===========================================================================
DO $$
DECLARE direct bigint[]; canon bigint[];
BEGIN
    SELECT array_agg(t ORDER BY ord) INTO direct
    FROM public.tjs_open('entities', 5, 0, 0, 1, 'id', 'ts IN (100)',
                         '[19,0,0,0,0,0,0,0]'::vector, 1,
                         (SELECT id FROM graph_store.edge_type WHERE name = 'related_to'))
         WITH ORDINALITY AS x(t, ord);

    SELECT array_agg(replace(c, 'chunk ', '')::bigint ORDER BY ord) INTO canon
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'
        LIMIT 5
    $q$) WITH ORDINALITY AS t(c, ord);

    IF canon IS DISTINCT FROM direct THEN
        RAISE EXCEPTION 'direct/canonical DIVERGED: canonical % vs direct tjs_open %', canon, direct;
    END IF;
    IF canon IS DISTINCT FROM ARRAY[20,10,30]::bigint[] THEN
        RAISE EXCEPTION 'parity pair wrong vs oracle: got % (expected {20,10,30})', canon;
    END IF;
    RAISE NOTICE 'PASS 2: canonical == direct tjs_open == oracle [20,10,30] (same ordered ids)';
END $$;

-- ===========================================================================
-- ASSERTION 3: the relational filter removes a NEARER row (the 40-vs-30 construction).
-- q=[40,...]: 40 is the exact match (dist 0) but stale (ts 999). Window IN (100) drops it
-- -> 'chunk 30'; the wide window IN (100, 999) keeps it -> 'chunk 40'. Both on-template.
-- ===========================================================================
DO $$
DECLARE narrow text; wide text;
BEGIN
    SELECT c INTO narrow FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '{40,0,0,0,0,0,0,0}'
        LIMIT 1
    $q$) AS c;
    SELECT c INTO wide FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100, 999)
        ORDER BY src_embedding <-> '{40,0,0,0,0,0,0,0}'
        LIMIT 1
    $q$) AS c;
    IF narrow <> 'chunk 30' OR wide <> 'chunk 40' THEN
        RAISE EXCEPTION 'filter not load-bearing: narrow=% wide=% (expected chunk 30, chunk 40)',
            narrow, wide;
    END IF;
    RAISE NOTICE 'PASS 3: filter drops the nearer stale 40 -> chunk 30; wide window -> chunk 40';
END $$;

-- ===========================================================================
-- ASSERTION 4: BOTH vector-literal dialects are accepted (the one admitted widening):
-- the pgvector bracket form returns the same answer as the fork brace form in assertion 1.
-- ===========================================================================
DO $$
DECLARE got text[];
BEGIN
    SELECT array_agg(c ORDER BY ord) INTO got
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '[19,0,0,0,0,0,0,0]'
        LIMIT 2
    $q$) WITH ORDINALITY AS t(c, ord);
    IF got IS DISTINCT FROM ARRAY['chunk 20','chunk 10'] THEN
        RAISE EXCEPTION 'bracket-dialect vector FAILED: got %', got;
    END IF;
    RAISE NOTICE 'PASS 4: bracket vector dialect == brace dialect answer';
END $$;

-- ===========================================================================
-- ASSERTION 5: the SCOPE GUARD still fails closed on stock — off-template variants RAISE
-- with no rows (grammar not widened beyond the brace/bracket vector dialect pair).
-- ===========================================================================
DO $$
DECLARE
    variants text[] := ARRAY[
        -- (a) wrong edge label — knows_about EXISTS in graph_store.edge_type (registered above),
        --     but the TEMPLATE pins related_to; the guard must reject it at parse, pre-catalog
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:knows_about]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 2$v$,
        -- (b) two hops — v1 is single-hop only
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(mid:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 2$v$,
        -- (c) INJECTION: non-numeric IN-list must never reach the filter fragment
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100 OR 1=1) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 2$v$,
        -- (d) missing LIMIT (no top-k bound)
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'$v$,
        -- (e) MISMATCHED vector delimiters ('{...]') — the dialect pair is matched-only
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0]' LIMIT 2$v$
    ];
    v       text;
    raised  boolean;
BEGIN
    FOREACH v IN ARRAY variants LOOP
        raised := false;
        BEGIN
            PERFORM graph_store.graph_query(v);
        EXCEPTION WHEN others THEN
            raised := true;
        END;
        IF NOT raised THEN
            RAISE EXCEPTION 'scope guard FAILED on stock: off-template variant ACCEPTED: %', v;
        END IF;
    END LOOP;
    RAISE NOTICE 'PASS 5: all % off-template variants rejected on stock', array_length(variants, 1);
END $$;

\echo '========== canonical query on STOCK PG via tjs_open (plan 075): ALL TESTS PASSED =========='

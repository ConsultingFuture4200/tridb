-- release_stock_smoke.sql — runtime smoke for the SHIPPED release image (advisor plan 076).
-- Proves the prebaked tridb/postgres-trimodal:pg16|pg17 image can install all three
-- extensions in dependency order and execute the actual tri-modal front door: one direct
-- public.tjs_open call and one canonical graph_store.graph_query() (plan 075 lowering).
-- Deliberately tiny — the full correctness suites live in STOCK_TESTS (Makefile) / CI job
-- `stock-pg`; this file only gates packaging, dependency order, and shared-library loading.
-- Run with psql -v ON_ERROR_STOP=1 (scripts/pg17_release_smoke.sh): any RAISE fails it.

-- dependency order: vector, then graph_store_am, then tjs_pg (requires both)
CREATE EXTENSION vector;
CREATE EXTENSION graph_store_am;
CREATE EXTENSION tjs_pg;

-- tiny relational + vector fixture: entity k has embedding [k,0,0,0]
CREATE TABLE entities (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding vector(4)
);
INSERT INTO entities
SELECT k, 'chunk ' || k, 100, ('[' || k || ',0,0,0]')::vector(4)
FROM generate_series(1, 50) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops);

-- native graph AM: dense vids 0..50 (ext id == vid — tjs_open's filter-first BFS joins
-- graph vids straight against entities.id) + one canonical related_to edge 1 -> 10
DO $$
DECLARE g int; v bigint;
BEGIN
    FOR g IN 0..50 LOOP
        v := graph_store.gph_upsert_vertex(g);
        IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;
    END LOOP;
END $$;
SELECT graph_store.add_edge(1, 10);

-- ===========================================================================
-- ASSERTION 1: a direct fused-operator call works — reach(1, related_to) = {10},
-- ts window keeps it, so the top-1 for q=[10,...] is exactly [10].
-- ===========================================================================
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(t ORDER BY ord) INTO got
    FROM public.tjs_open('entities', 1, 0, 0, 1, 'id', 'ts IN (100)',
                         '[10,0,0,0]'::vector, 1,
                         (SELECT id FROM graph_store.edge_type WHERE name = 'related_to'))
         WITH ORDINALITY AS x(t, ord);
    IF got IS DISTINCT FROM ARRAY[10]::bigint[] THEN
        RAISE EXCEPTION 'direct tjs_open smoke FAILED: got % (expected {10})', got;
    END IF;
    RAISE NOTICE 'PASS: direct tjs_open -> [10]';
END $$;

-- ===========================================================================
-- ASSERTION 2: the canonical front door (plan 075) lowers to tjs_open on stock
-- and returns the expected chunk.
-- ===========================================================================
DO $$
DECLARE got text;
BEGIN
    SELECT c INTO got FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '[10,0,0,0]'
        LIMIT 1
    $q$) AS c;
    IF got IS DISTINCT FROM 'chunk 10' THEN
        RAISE EXCEPTION 'canonical graph_query smoke FAILED: got % (expected chunk 10)', got;
    END IF;
    RAISE NOTICE 'PASS: canonical graph_query -> chunk 10';
END $$;

\echo RELEASE SMOKE PASS

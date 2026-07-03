-- join_order_lowering_test.sql — ADR-0011 Stage 2 (DEV-1285): the lowering makes the
-- FR-6 join-order decision at the graph_query() call site and records it.
--
-- Proves, on the live engine:
--   (1) graph_store.last_join_order() is NULL before any graph_query() call,
--   (2) inverted-selectivity WINDOWS pick OPPOSITE orders through the FULL lowering
--       (selective ~1% -> filter_first; broad ~80% -> vector_first) — the decision-level
--       half of test/join_order_integration_test.sql's done-criterion (a),
--   (3) answers are body-independent: both windows return the canonical results whichever
--       engine runs this (on DEV-1290 engines the decision selects the physical body; on
--       older engines it is recorded but the hardwired vector-first body runs),
--   (4) without the join_order extension the lowering still works and records the
--       'vector_first' default (soft dependency).
--
-- Runs under psql -v ON_ERROR_STOP=1; any failed ASSERT / RAISE aborts the suite.

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store;
CREATE EXTENSION join_order;

-- Corpus: 2000 entities, ts = k % 100 (uniform, 100 distinct values -> 20 rows per value,
-- so one IN value estimates ~1% and an 80-value window ~80%). INSERTs precede CREATE INDEX
-- (fork HNSW incremental-insert limitation — see canonical_e2e_test.sql:33-36).
CREATE TABLE entities (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding float8[8]
);

INSERT INTO entities
SELECT k, 'chunk ' || k, (k % 100)::int, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;

CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- graph: source vertex 1 relates to {10, 20, 30, 40} (ts 10, 20, 30, 40 respectively).
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);

-- Stage 2 reads pg_class.reltuples + the planner's WHERE row estimate: both need stats.
ANALYZE entities;

SET enable_seqscan = off;

-- ===========================================================================
-- ASSERTION 1: no decision recorded before the first graph_query() call.
-- ===========================================================================
DO $$
BEGIN
    ASSERT graph_store.last_join_order() IS NULL,
        'last_join_order must be NULL before any graph_query call';
    RAISE NOTICE 'PASS dev-1285 stage2: last_join_order NULL before first call';
END $$;

-- ===========================================================================
-- ASSERTION 2: selective window (~1% of the corpus) -> filter_first decision recorded, and
-- the answer is the canonical result regardless of which body the engine ran.
-- Window IN (10): among reachable {10,20,30,40} only id 10 (ts 10) survives.
-- ===========================================================================
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(replace(c, 'chunk ', '')::bigint) INTO got
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (10)
        ORDER BY src_embedding <-> '{9,0,0,0,0,0,0,0}'
        LIMIT 1
    $q$) AS c;

    ASSERT graph_store.last_join_order() = 'filter_first',
        format('selective window must choose filter_first (got %s)',
               graph_store.last_join_order());
    ASSERT got = ARRAY[10]::bigint[],
        format('selective window answer changed: got %s (expected {10})', got);
    RAISE NOTICE 'PASS dev-1285 stage2: selective (~1%%) window -> filter_first, canonical answer';
END $$;

-- ===========================================================================
-- ASSERTION 3: broad window (80 of 100 ts values, ~80%) -> vector_first, and the
-- answer equals the parse_canonical.sql FR-4 oracle {20,10}.
-- ===========================================================================
DO $$
DECLARE got bigint[]; window_list text;
BEGIN
    SELECT string_agg(g::text, ',') INTO window_list FROM generate_series(0, 79) AS g;

    SELECT array_agg(replace(c, 'chunk ', '')::bigint) INTO got
    FROM graph_store.graph_query(format($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (%s)
        ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'
        LIMIT 2
    $q$, window_list)) AS c;

    ASSERT graph_store.last_join_order() = 'vector_first',
        format('broad window must choose vector_first (got %s)',
               graph_store.last_join_order());
    ASSERT got = ARRAY[20,10]::bigint[],
        format('broad window answer changed: got %s (expected {20,10})', got);
    RAISE NOTICE 'PASS dev-1285 stage2: broad (~80%%) window -> vector_first, answer = FR-4 oracle';
END $$;

-- ===========================================================================
-- ASSERTION 4: soft dependency — without the decision core the lowering still
-- runs and records the 'vector_first' default (today's only physical path).
-- ===========================================================================
DROP EXTENSION join_order;
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(replace(c, 'chunk ', '')::bigint) INTO got
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (10)
        ORDER BY src_embedding <-> '{9,0,0,0,0,0,0,0}'
        LIMIT 1
    $q$) AS c;

    ASSERT graph_store.last_join_order() = 'vector_first',
        format('without join_order ext the default must be vector_first (got %s)',
               graph_store.last_join_order());
    ASSERT got = ARRAY[10]::bigint[],
        format('fallback answer changed: got %s (expected {10})', got);
    RAISE NOTICE 'PASS dev-1285 stage2: join_order ext absent -> vector_first default, lowering intact';
END $$;

\echo === join_order_lowering_test: ALL PASS (ADR-0011 Stage 2 decision at the call site) ===

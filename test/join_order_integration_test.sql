-- join_order_integration_test.sql — FR-6 end-to-end: the join-order DECISION CHANGES EXECUTION
-- (DEV-1285 done-criterion, realized by DEV-1290). Successor to the retired
-- test/join_order_integration_stub.sql, whose promised assertions this file now RUNS:
--
--   (a) inverted-selectivity WINDOWS through the FULL lowering (graph_store.graph_query) pick
--       OPPOSITE drivers, asserted on BOTH companions: graph_store.last_join_order() (what the
--       lowering decided) AND tjs_last_join_order() (what the operator actually RAN) — the
--       "EXPLAIN shows selected driver" criterion under ADR-0011 Option B.
--   (b) the decision is NOT inert: on the selective window, the filter-first drive examines
--       MATERIALLY fewer candidates than the forced vector-first drive (SM-1/SM-3 evidence),
--       with identical answers.
--
-- Needs vectordb (DEV-1290 engine) + graph_store + join_order in one cluster
-- (scripts/join_order_integration_test.sh). Runs under psql -v ON_ERROR_STOP=1.

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store;
CREATE EXTENSION join_order;

-- Same corpus shape as test/join_order_lowering_test.sql: 2000 entities, ts = k % 100
-- (one window value ≈ 1% selectivity -> filter_first; an 80-value window ≈ 80% -> vector_first).
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
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);
ANALYZE entities;
SET enable_seqscan = off;

-- ===========================================================================
-- (a) Opposite drivers through the FULL lowering, on BOTH companions.
-- ===========================================================================
DO $$
DECLARE got bigint[]; window_list text;
BEGIN
    -- Selective window (~1%): the heuristic must choose filter_first AND the operator must RUN it.
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
        format('lowering decision: expected filter_first, got %s', graph_store.last_join_order());
    ASSERT tjs_last_join_order() = 'filter_first',
        format('operator execution: expected filter_first, got %s', tjs_last_join_order());
    ASSERT got = ARRAY[10]::bigint[], format('selective answer: %s (expected {10})', got);

    -- Broad window (~80%): vector_first, decided AND executed.
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
        format('lowering decision: expected vector_first, got %s', graph_store.last_join_order());
    ASSERT tjs_last_join_order() = 'vector_first',
        format('operator execution: expected vector_first, got %s', tjs_last_join_order());
    ASSERT got = ARRAY[20,10]::bigint[], format('broad answer: %s (expected {20,10})', got);

    RAISE NOTICE 'PASS dev-1285/1290 (a): inverted-selectivity windows pick OPPOSITE drivers, decided AND executed';
END $$;

-- ===========================================================================
-- (b) The decision is not inert: forced bodies on the selective predicate give identical
-- answers but MATERIALLY different candidates-examined (SM-1/SM-3).
-- ===========================================================================
DO $$
DECLARE vf bigint[]; ff bigint[]; vf_ex bigint; ff_ex bigint;
BEGIN
    SELECT array_agg(t.id) INTO vf FROM tjs('entities', 1, 10000, 1::bigint, 'id, chunk',
        'ts IN (10)', 'embedding <-> ''{9,0,0,0,0,0,0,0}''', 'vector_first') AS t(id bigint, chunk text);
    vf_ex := tjs_candidates_examined();
    SELECT array_agg(t.id) INTO ff FROM tjs('entities', 1, 10000, 1::bigint, 'id, chunk',
        'ts IN (10)', 'embedding <-> ''{9,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
    ff_ex := tjs_candidates_examined();
    ASSERT vf = ff, format('bodies disagree: %s vs %s', vf, ff);
    ASSERT ff_ex * 10 <= vf_ex,
        format('expected filter-first examined (%s) << vector-first examined (%s)', ff_ex, vf_ex);
    RAISE NOTICE 'PASS dev-1285/1290 (b): same answer, examined vf=% ff=% — the decision changes the work',
        vf_ex, ff_ex;
END $$;

\echo === join_order_integration_test: ALL PASS (FR-6 decision changes execution end-to-end) ===

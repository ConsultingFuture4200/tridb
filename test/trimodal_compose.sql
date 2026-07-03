-- Tri-modal composition (DEV-1167/1169 functional precursor): the three legs —
-- graph traversal + relational filter + vector similarity — in ONE SQL query in
-- ONE Postgres process. This is the correctness baseline the TJS operator will
-- later make early-terminating; here it proves the legs compose and agree.
--
-- It also illustrates the join-order thesis (DEV-1170): the graph + relational
-- legs are selective (restrict to a handful of vertices), so the vector leg only
-- orders the survivors — "filter-first".

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store_am;  -- v1 native AM (v0-compat surface, ADR-0013 Stage B)

-- relational + vector store: entities with an embedding and a timestamp.
CREATE TABLE entities (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding float8[8]
);

-- corpus: entity k has embedding [k,0,0,0,0,0,0,0]; entity 40 is "stale" (ts 999).
INSERT INTO entities
SELECT k,
       'chunk ' || k,
       CASE WHEN k = 40 THEN 999 ELSE 100 END,
       ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 50) AS k;

CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- graph: source vertex 1 relates to {10, 20, 30, 40}.
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);

-- The tri-modal query: from vertex 1, traverse to neighbors (GRAPH), keep only
-- non-stale ones (RELATIONAL filter ts < 500 drops 40), order by distance to the
-- question embedding [19,...] (VECTOR), take the closest 2.
--   distances among {10,20,30}: |10-19|=9, |20-19|=1, |30-19|=11  ->  [20, 10]
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(id) INTO got FROM (
        SELECT e.id
        FROM graph_store.neighbors(1) AS dst
        JOIN entities e ON e.id = dst
        WHERE e.ts < 500
        ORDER BY e.embedding <-> '{19,0,0,0,0,0,0,0}'
        LIMIT 2
    ) q;
    IF got IS DISTINCT FROM ARRAY[20,10]::bigint[] THEN
        RAISE EXCEPTION 'tri-modal compose FAILED: got % (expected {20,10})', got;
    END IF;
    RAISE NOTICE 'PASS tri-modal: graph(1)->filter(ts<500)->vector(<->19) top-2 = %', got;
END $$;

-- Sanity: without the relational filter, vertex 40 (stale) would still be excluded
-- by distance here, so prove the filter is load-bearing with a query where 40 IS close.
--   q = [40,...]: among {10,20,30,40}, 40 is exact match (dist 0). With ts filter it
--   is dropped, so the closest survivor is 30.
DO $$
DECLARE with_filter bigint; without_filter bigint;
BEGIN
    SELECT e.id INTO with_filter
    FROM graph_store.neighbors(1) AS dst JOIN entities e ON e.id = dst
    WHERE e.ts < 500
    ORDER BY e.embedding <-> '{40,0,0,0,0,0,0,0}' LIMIT 1;

    SELECT e.id INTO without_filter
    FROM graph_store.neighbors(1) AS dst JOIN entities e ON e.id = dst
    ORDER BY e.embedding <-> '{40,0,0,0,0,0,0,0}' LIMIT 1;

    IF with_filter <> 30 OR without_filter <> 40 THEN
        RAISE EXCEPTION 'filter not load-bearing: with=% without=% (expected 30, 40)',
            with_filter, without_filter;
    END IF;
    RAISE NOTICE 'PASS filter load-bearing: ts filter drops the closest (40) -> 30; unfiltered -> 40';
END $$;

\echo '================ tri-modal composition: ALL TESTS PASSED ================'

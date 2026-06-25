-- canonical_e2e_test.sql — the DEV-1169 TJS operator end-to-end oracle (FR-4).
--
-- Proves the Traversal-Join-Similarity operator composes all THREE modalities in ONE plan call
-- (no app-layer merge, no SQL nesting) with a single global, early-terminating top-k:
--
--   tjs(table, k, term_cond, src, attr_exp, filter_exp, orderby_exp)
--     VECTOR leg   — orderby_exp is the HNSW order (xs_orderbyvals[0] = the sole rank authority)
--     RELATIONAL   — filter_exp pushed into the vector leg's SQL WHERE
--     GRAPH        — src is the graph source; only candidates reachable (src)->(dst) survive
--
-- This is the SAME corpus and the SAME answer as test/trimodal_compose.sql (the nested-SQL
-- correctness oracle): the canonical query graph(1)->filter(ts<500)->vector(<->19) top-2 = {20,10}.
-- Here it runs as ONE tjs(...) call — that is FR-4 (single plan).
--
-- FORK CONSTRAINT (test/trimodal_early_term.sql): MSVBASE's scalar `<->` returns 0 OUTSIDE an index
-- scan, so the only authoritative distance is the HNSW index scan's internal xs_orderbyvals[0].
-- TJS reads exactly that; this test never re-ranks in SQL.

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store;

-- Identical corpus to trimodal_compose.sql: entity k has embedding [k,0,...]; entity 40 is stale.
CREATE TABLE entities (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding float8[8]
);

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

SET enable_seqscan = off;

-- ===========================================================================
-- ASSERTION 1: all three legs engage — ONE tjs() call returns {20,10}.
--
-- distances among reachable {10,20,30,40} to q=[19,...]: |10-19|=9, |20-19|=1, |30-19|=11,
-- |40-19|=21. With ts<500 dropping 40 (which is far anyway here), the closest 2 are {20,10}.
-- The result {20,10} is only reachable if ALL THREE legs engage: graph membership (restricts to
-- {10,20,30,40}), the ts filter, AND the vector order. A vector-only run over all 50 entities, or
-- a no-graph run, returns a different set.
-- ===========================================================================
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id
        FROM tjs('entities', 2, 0, 1::bigint, 'id', 'ts < 500',
                 'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    IF got IS DISTINCT FROM ARRAY[20,10]::bigint[] THEN
        RAISE EXCEPTION 'TJS three-legs FAILED: got % (expected {20,10})', got;
    END IF;
    RAISE NOTICE 'PASS three legs: tjs(graph=1, filter=ts<500, vector=<->19) top-2 = %', got;
END $$;

-- ===========================================================================
-- ASSERTION 1b: distinct from a vector-only / no-graph run.
-- With the graph leg DISABLED (src = -1) and the same vector+filter, the answer changes (the
-- closest entities overall — {18,20} for q=[19] over all of {1..50} minus stale 40 — are NOT the
-- graph-restricted {20,10}). This proves the graph leg is load-bearing, not incidental.
-- ===========================================================================
DO $$
DECLARE no_graph bigint[];
BEGIN
    SELECT array_agg(id) INTO no_graph FROM (
        SELECT t.id
        FROM tjs('entities', 2, 0, (-1)::bigint, 'id', 'ts < 500',
                 'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    IF no_graph @> ARRAY[20,10]::bigint[] AND ARRAY[20,10]::bigint[] @> no_graph THEN
        RAISE EXCEPTION 'graph leg NOT load-bearing: no-graph run also returned {20,10} (got %)', no_graph;
    END IF;
    RAISE NOTICE 'PASS graph load-bearing: no-graph top-2 = % (differs from graph-restricted {20,10})', no_graph;
END $$;

-- ===========================================================================
-- ASSERTION 2: relational filter is load-bearing (the 40-vs-30 construction).
-- q = [40,...]: among reachable {10,20,30,40}, 40 is the exact match (dist 0). WITH the ts filter
-- it is dropped -> closest survivor is 30. WITHOUT the filter -> 40. Same construction as
-- trimodal_compose.sql:63-80, now through tjs().
-- ===========================================================================
DO $$
DECLARE with_filter bigint; without_filter bigint;
BEGIN
    SELECT t.id INTO with_filter
    FROM tjs('entities', 1, 0, 1::bigint, 'id', 'ts < 500',
             'embedding <-> ''{40,0,0,0,0,0,0,0}''') AS t(id bigint);

    SELECT t.id INTO without_filter
    FROM tjs('entities', 1, 0, 1::bigint, 'id', '',
             'embedding <-> ''{40,0,0,0,0,0,0,0}''') AS t(id bigint);

    IF with_filter <> 30 OR without_filter <> 40 THEN
        RAISE EXCEPTION 'TJS filter not load-bearing: with=% without=% (expected 30, 40)',
            with_filter, without_filter;
    END IF;
    RAISE NOTICE 'PASS filter load-bearing: ts filter drops the closest (40) -> 30; unfiltered -> 40';
END $$;

-- ===========================================================================
-- ASSERTION 3 (increment 3 — early termination, SM-3): on a LARGER corpus, the TJS top-k examines
-- far fewer ANN candidates than the corpus size. tjs_candidates_examined() reports the candidates
-- the LAST tjs() scan pulled from the HNSW stream; with the consecutive_drops bound it stops long
-- before draining all rows. This is the no-blocking property: top-k settles via early termination,
-- not full materialization.
-- ===========================================================================
INSERT INTO entities
SELECT k, 'chunk ' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(51, 2000) AS k;

-- src 1 reaches only {10,20,30,40}; rank by distance to q=[19]. The scan should terminate well
-- before examining all 2000 rows once the top-5 is settled (consecutive_drops default 50).
DO $$
DECLARE got bigint[]; examined bigint; corpus bigint;
BEGIN
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id
        FROM tjs('entities', 5, 0, 1::bigint, 'id', 'ts < 500',
                 'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    examined := tjs_candidates_examined();
    SELECT count(*) INTO corpus FROM entities;

    -- Graph restricts to {10,20,30,40} (40 stale -> dropped); the top-5 reachable survivors are
    -- {10,20,30} ordered by distance. The exact set is {20,10,30} (only 3 reachable+fresh exist).
    IF NOT (got @> ARRAY[10,20,30]::bigint[] AND ARRAY[10,20,30]::bigint[] @> got) THEN
        RAISE EXCEPTION 'TJS early-term result set wrong: got % (expected {10,20,30})', got;
    END IF;
    IF examined >= corpus THEN
        RAISE EXCEPTION 'early-termination FAILED: examined % of % candidates (no early stop)',
            examined, corpus;
    END IF;
    RAISE NOTICE 'PASS early termination: examined % of % candidates (<< corpus) -> top-k = %',
        examined, corpus, got;
END $$;

\echo '================ TJS canonical e2e (FR-4): ALL TESTS PASSED ================'

-- DEV-1165: graph traversal iterator (Open/Next/Close, early-terminating) — the gph_traverse SRF.
-- Exercises edge-tuple emission (one (src,dst) :related_to edge per Next(), not just the bare
-- neighbor vid), correctness, parity with gph_neighbors (shared gs_* engine), and TR-1 early
-- termination on the edge stream. Run by scripts/graph_am_test.sh in its own database.

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;

-- 6 vertices (dense vids 0..5); topology 0 -> {1,2,3}, 1 -> {4}.
SELECT gph_insert_vertex() FROM generate_series(1, 6);
SELECT gph_insert_edge(0, 1);
SELECT gph_insert_edge(0, 2);
SELECT gph_insert_edge(0, 3);
SELECT gph_insert_edge(1, 4);

-- NB: the correctness checks below call gph_traverse in a FROM-clause position — fine here
-- because they are full scans (no LIMIT). TR-1 early termination is asserted ONLY via the
-- target-list position at the LIMIT 5 check further down; a FROM-clause SRF would materialize.

-- Edge emission: gph_traverse(0) yields (src=0, dst in {1,2,3}) — the edge, not just dst.
DO $$
DECLARE d bigint[]; s bigint[];
BEGIN
    SELECT array_agg(dst ORDER BY dst), array_agg(DISTINCT src) INTO d, s FROM gph_traverse(0);
    IF d IS DISTINCT FROM ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION 'gph_traverse(0) dst=% (expected {1,2,3})', d;
    END IF;
    IF s IS DISTINCT FROM ARRAY[0]::bigint[] THEN
        RAISE EXCEPTION 'gph_traverse(0) src=% (expected {0}; edge src must be carried)', s;
    END IF;
    PERFORM * FROM gph_traverse(4);		-- present vertex, no out-edges: empty, not an error
    RAISE NOTICE 'PASS edge emission: gph_traverse(0) = {(0,1),(0,2),(0,3)}, gph_traverse(4) = {}';
END $$;

-- Engine parity: gph_traverse's dst set equals gph_neighbors (both drive the shared gs_* engine).
DO $$
DECLARE a bigint[]; b bigint[];
BEGIN
    SELECT array_agg(dst ORDER BY dst) INTO a FROM gph_traverse(0);
    SELECT array_agg(x ORDER BY x) INTO b FROM gph_neighbors(0) x;
    IF a IS DISTINCT FROM b THEN
        RAISE EXCEPTION 'engine divergence: gph_traverse dst=% , gph_neighbors=%', a, b;
    END IF;
    RAISE NOTICE 'PASS engine parity: gph_traverse dst == gph_neighbors';
END $$;

-- Multi-page adjacency chaining + TR-1 early termination on the EDGE stream.
-- 1500 edges from vertex 5 span two 32KB adjacency pages (1022 EdgeSlots/page).
SELECT gph_insert_edge(5, g % 5) FROM generate_series(1, 1500) g;

DO $$
DECLARE total bigint; v0 bigint; v1 bigint;
BEGIN
    SELECT count(*) INTO total FROM gph_traverse(5);
    IF total <> 1500 THEN
        RAISE EXCEPTION 'gph_traverse(5) full scan = % (expected 1500; multi-page chain broken)', total;
    END IF;

    v0 := gph_visits();
    -- target-list SRF (nodeProjectSet) is pull-based, so LIMIT stops the iterator early; a
    -- FROM-clause FunctionScan would be materialized by nodeFunctionscan and could not.
    PERFORM gph_traverse(5) LIMIT 5;
    v1 := gph_visits();
    IF v1 - v0 <> 5 THEN
        RAISE EXCEPTION 'early termination broken: LIMIT 5 did % edge-steps (expected 5)', v1 - v0;
    END IF;
    RAISE NOTICE 'PASS early termination: gph_traverse LIMIT 5 => 5 edge-steps, not 1500 (<<|E|, 2nd adj page untouched)';
END $$;

\echo '============ graph traversal iterator (gph_traverse, DEV-1165): ALL TESTS PASSED ============'

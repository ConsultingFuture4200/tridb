-- DEV-1164: native adjacency-list graph store (v1 core) — correctness suite.
-- Exercises the access method end to end on the MSVBASE fork: vertex/edge insert through the
-- shared buffer manager + WAL, the incremental Open/Next/Close iterator with TR-1 early
-- termination, multi-page adjacency chaining, and FR-7 transaction-abort atomicity.
-- Run by scripts/graph_am_test.sh (which also restarts the cluster to prove WAL persistence).

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;	-- the extension installs into the graph_store schema

-- 6 vertices -> dense vids 0..5 (auto-committed, so visible).
SELECT gph_insert_vertex() FROM generate_series(1, 6);

DO $$
BEGIN
    IF gph_vertex_count() <> 6 THEN
        RAISE EXCEPTION 'vertex_count % <> 6', gph_vertex_count();
    END IF;
    RAISE NOTICE 'PASS insert: 6 vertices persisted (vids 0..5)';
END $$;

-- Topology: 0 -> {1,2,3}, 1 -> {4}.
SELECT gph_insert_edge(0, 1);
SELECT gph_insert_edge(0, 2);
SELECT gph_insert_edge(0, 3);
SELECT gph_insert_edge(1, 4);

DO $$
DECLARE n bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO n FROM gph_neighbors(0) x;
    IF n IS DISTINCT FROM ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION 'neighbors(0)=% (expected {1,2,3})', n;
    END IF;
    SELECT array_agg(x ORDER BY x) INTO n FROM gph_neighbors(1) x;
    IF n IS DISTINCT FROM ARRAY[4]::bigint[] THEN
        RAISE EXCEPTION 'neighbors(1)=% (expected {4})', n;
    END IF;
    PERFORM x FROM gph_neighbors(4) x;	-- no out-edges; must not error
    RAISE NOTICE 'PASS traversal: neighbors(0)={1,2,3}, neighbors(1)={4}, neighbors(4)={}';
END $$;

-- Multi-page adjacency chaining + TR-1 early termination.
-- 1500 edges from vertex 5 span two 32KB adjacency pages (1022 EdgeSlots/page).
SELECT gph_insert_edge(5, g % 5) FROM generate_series(1, 1500) g;

DO $$
DECLARE total bigint; v0 bigint; v1 bigint;
BEGIN
    SELECT count(*) INTO total FROM gph_neighbors(5);
    IF total <> 1500 THEN
        RAISE EXCEPTION 'vertex 5 full scan = % (expected 1500; multi-page chain broken)', total;
    END IF;

    v0 := gph_visits();
    -- SRF in the target list (nodeProjectSet) is pull-based, so LIMIT stops the iterator
    -- early; a FROM-clause SRF would be materialized by nodeFunctionscan and could not.
    PERFORM gph_neighbors(5) LIMIT 5;
    v1 := gph_visits();
    IF v1 - v0 <> 5 THEN
        RAISE EXCEPTION 'early termination broken: LIMIT 5 did % traversal steps (expected 5)', v1 - v0;
    END IF;
    RAISE NOTICE 'PASS chaining (1500 edges, 2 adj pages) + early termination (LIMIT 5 => 5 steps, not 1500)';
END $$;

-- FR-7 substrate: graph writes participate in the host transaction.
BEGIN;
SELECT gph_insert_vertex();		-- a 7th vertex, inside an uncommitted txn
DO $$
BEGIN
    IF gph_vertex_count() <> 7 THEN
        RAISE EXCEPTION 'in-txn vertex_count % <> 7 (own uncommitted write should be visible to itself)', gph_vertex_count();
    END IF;
END $$;
ROLLBACK;

DO $$
BEGIN
    IF gph_vertex_count() <> 6 THEN
        RAISE EXCEPTION 'after ROLLBACK vertex_count % <> 6 (graph write did not roll back atomically)', gph_vertex_count();
    END IF;
    RAISE NOTICE 'PASS FR-7 substrate: own write visible in-txn (7), invisible after ROLLBACK (6)';
END $$;

\echo '============ graph_store_am v1 core: ALL TESTS PASSED (DEV-1164) ============'

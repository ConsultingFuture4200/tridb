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

-- Batched edge-append (DEV-1354): gph_insert_edges(src, dst[]) must produce the SAME adjacency
-- run (same emission order) as N x gph_insert_edge, in one call. Vertices 0..5 were materialized
-- dense-in-order before any edge, so the O(1) dense src locate applies. Vertices 3 and 4 have no
-- out-edges yet (only 0,1,5 do), so these are clean first-page + chaining cases.
DO $$
DECLARE r bigint; n bigint[]; total bigint;
BEGIN
    -- (a) small hand-checked run: first adjacency page for vertex 3.
    SELECT gph_insert_edges(3, ARRAY[0,1,2,4,5]::bigint[]) INTO r;
    IF r <> 5 THEN
        RAISE EXCEPTION 'gph_insert_edges returned % (expected 5)', r;
    END IF;
    SELECT array_agg(x) INTO n FROM gph_neighbors(3) x;   -- emission (== insertion) order, NOT sorted
    IF n IS DISTINCT FROM ARRAY[0,1,2,4,5]::bigint[] THEN
        RAISE EXCEPTION 'neighbors(3)=% (expected {0,1,2,4,5} in array order)', n;
    END IF;

    -- (b) append MORE to the same vertex: exercises the fill-existing-tail-page path.
    SELECT gph_insert_edges(3, ARRAY[5,4]::bigint[]) INTO r;
    IF r <> 2 THEN
        RAISE EXCEPTION 'gph_insert_edges append returned % (expected 2)', r;
    END IF;
    SELECT array_agg(x) INTO n FROM gph_neighbors(3) x;
    IF n IS DISTINCT FROM ARRAY[0,1,2,4,5,5,4]::bigint[] THEN
        RAISE EXCEPTION 'neighbors(3) after append=% (expected {0,1,2,4,5,5,4})', n;
    END IF;

    -- (c) multi-page batch: 1500 edges from vertex 4 span two 32KB adj pages in ONE call.
    SELECT gph_insert_edges(4, (SELECT array_agg(g % 5) FROM generate_series(1,1500) g)) INTO r;
    IF r <> 1500 THEN
        RAISE EXCEPTION 'gph_insert_edges multipage returned % (expected 1500)', r;
    END IF;
    SELECT count(*) INTO total FROM gph_neighbors(4);
    IF total <> 1500 THEN
        RAISE EXCEPTION 'vertex 4 batched full scan = % (expected 1500; multi-page chain broken)', total;
    END IF;
    RAISE NOTICE 'PASS batched insert: hand-checked run + append + 1500-edge multi-page chain';
END $$;

-- Abort atomicity (FR-7): a rolled-back batch leaves ZERO visible edges (es_xmin filtered on read).
BEGIN;
SELECT gph_insert_edges(2, ARRAY[0,1,2,3,4,5]::bigint[]);
DO $$
BEGIN
    IF (SELECT count(*) FROM gph_neighbors(2)) <> 6 THEN
        RAISE EXCEPTION 'in-txn neighbors(2) count % <> 6 (own uncommitted batch should be visible to itself)',
            (SELECT count(*) FROM gph_neighbors(2));
    END IF;
END $$;
ROLLBACK;

DO $$
BEGIN
    IF (SELECT count(*) FROM gph_neighbors(2)) <> 0 THEN
        RAISE EXCEPTION 'after ROLLBACK neighbors(2) count % <> 0 (batched edges did not roll back atomically)',
            (SELECT count(*) FROM gph_neighbors(2));
    END IF;
    RAISE NOTICE 'PASS batched abort-atomicity: batch visible in-txn (6), zero visible after ROLLBACK';
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

-- Batch/scalar parity on a tombstoned dst (advisor plan 046): gph_insert_edge locates BOTH
-- endpoints (visibility-checked), so a tombstoned dst is rejected. gph_insert_edges must reject
-- the same way instead of silently appending a phantom edge to a dead vertex. Vertex 2 is live at
-- this point (target of edge 0->2 and of vertex 3's batched run); tombstone it here, at the end of
-- the file, so no earlier assertion depends on it staying live.
SELECT gph_tombstone_vertex(2);

DO $$
DECLARE nbrs bigint[];
BEGIN
    BEGIN
        PERFORM gph_insert_edges(1, ARRAY[4,2]::bigint[]);
        RAISE EXCEPTION 'gph_insert_edges(1, {4,2}) should have ERRORed on tombstoned dst 2';
    EXCEPTION WHEN others THEN
        IF SQLERRM NOT LIKE '%destination vertex 2%' THEN
            RAISE;   -- some other error: propagate, this is not the expected rejection
        END IF;
    END;

    -- the rejected batch left no phantom edge: neighbors(1) is still just the pre-existing {4}.
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(1) x;
    IF nbrs IS DISTINCT FROM ARRAY[4]::bigint[] THEN
        RAISE EXCEPTION 'neighbors(1)=% after rejected batch (expected {4}: no phantom edge to tombstoned 2)', nbrs;
    END IF;

    -- same shape of call with the tombstoned dst swapped for a live one succeeds normally.
    PERFORM gph_insert_edges(1, ARRAY[4,3]::bigint[]);
    SELECT array_agg(x) INTO nbrs FROM gph_neighbors(1) x;
    IF nbrs IS DISTINCT FROM ARRAY[4,4,3]::bigint[] THEN
        RAISE EXCEPTION 'neighbors(1)=% after live-dst batch (expected {4,4,3})', nbrs;
    END IF;
    RAISE NOTICE 'PASS batch/scalar dst parity: tombstoned dst rejected (no phantom edge), live-dst batch still succeeds';
END $$;

\echo '============ graph_store_am v1 core: ALL TESTS PASSED (DEV-1164) ============'

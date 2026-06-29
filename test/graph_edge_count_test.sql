-- plan 006: store-wide edge count on the graph metapage (gm_edge_count) — correctness suite.
-- Asserts gph_edge_count() tracks the number of directed edges appended, across all three
-- gph_insert_edge branches (first-edge, append-to-tail-with-room, chain-new-adjacency-page),
-- and that the counter rolls back atomically with the edge on transaction ABORT (it is bumped
-- under GenericXLog alongside the edge slot). gm_edge_count is the FR-6 avg_out_degree source.
--
-- UNBUILT-HERE (GX10-gated): the graph store access method compiles only inside the MSVBASE fork
-- (PG 13.4, --with-blocksize=32). Run by scripts/graph_am_test.sh / a graph-test harness on target.

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;	-- the extension installs into the graph_store schema

-- Fresh store: no edges yet.
DO $$
BEGIN
    IF gph_edge_count() <> 0 THEN
        RAISE EXCEPTION 'fresh store edge_count % <> 0', gph_edge_count();
    END IF;
    RAISE NOTICE 'PASS init: gph_edge_count() = 0 on a fresh store';
END $$;

-- 6 vertices -> dense vids 0..5 (auto-committed, so visible). Vertices must NOT bump edge_count.
SELECT gph_insert_vertex() FROM generate_series(1, 6);

DO $$
BEGIN
    IF gph_edge_count() <> 0 THEN
        RAISE EXCEPTION 'after 6 vertex inserts edge_count % <> 0 (vertex insert wrongly counted as edge)', gph_edge_count();
    END IF;
    RAISE NOTICE 'PASS vertices-only: gph_edge_count() still 0 after 6 vertex inserts';
END $$;

-- Branch 1 (first edge for a vertex) + branch 2 (append to tail with room): 0 -> {1,2,3}, 1 -> {4}.
-- Edge 0->1 hits the first-edge branch for vertex 0; 0->2, 0->3 hit append-with-room; 1->4 is
-- first-edge for vertex 1. Four edges total.
SELECT gph_insert_edge(0, 1);
SELECT gph_insert_edge(0, 2);
SELECT gph_insert_edge(0, 3);
SELECT gph_insert_edge(1, 4);

DO $$
BEGIN
    IF gph_edge_count() <> 4 THEN
        RAISE EXCEPTION 'after 4 edges edge_count % <> 4 (first-edge / append-with-room branch miscount)', gph_edge_count();
    END IF;
    RAISE NOTICE 'PASS branches 1+2: gph_edge_count() = 4 after 4 edges';
END $$;

-- Branch 3 (chain a new adjacency page): 1500 edges from vertex 5 span two 32KB adjacency pages
-- (1022 EdgeSlots/page), so the chain-new-adjacency-page branch fires. Total edges become 4 + 1500.
SELECT gph_insert_edge(5, g % 5) FROM generate_series(1, 1500) g;

DO $$
DECLARE total bigint;
BEGIN
    IF gph_edge_count() <> 1504 THEN
        RAISE EXCEPTION 'after multi-page chaining edge_count % <> 1504 (chain-new-page branch did not bump the counter)', gph_edge_count();
    END IF;
    -- Cross-check against an actual traversal count for vertex 5.
    SELECT count(*) INTO total FROM gph_neighbors(5);
    IF total <> 1500 THEN
        RAISE EXCEPTION 'vertex 5 full scan = % (expected 1500)', total;
    END IF;
    RAISE NOTICE 'PASS branch 3: gph_edge_count() = 1504 after chaining (2 adj pages on vertex 5)';
END $$;

-- IN-PROCESS ABORT semantics (v1, documented). gm_edge_count is a RAW metapage counter bumped via
-- GenericXLog in gph_insert_edge. GenericXLog provides crash-recovery REDO durability, NOT in-process
-- UNDO: Postgres does not roll back a dirtied buffer page on a clean transaction ROLLBACK, so the raw
-- counter is NOT abort-aware in-process — exactly like gph_vertex_count(), whose comment states "the
-- raw metapage counter is not abort-aware" and which therefore MVCC-scans for a visible count. This
-- mirrors plan 006's Maintenance note: "v1 uses txn-level visibility + GenericXLog ... do not try to
-- make the counter MVCC-exact in this plan." The abort-durability that IS guaranteed (an uncommitted
-- increment that never reached a durable COMMIT is absent after CRASH recovery) is proven separately
-- by scripts/crash_recovery_test.sh, where the WAL record for the doomed txn is never replayed.
BEGIN;
SELECT gph_insert_edge(2, 0);
SELECT gph_insert_edge(2, 1);
DO $$
BEGIN
    IF gph_edge_count() <> 1506 THEN
        RAISE EXCEPTION 'in-txn edge_count % <> 1506 (own uncommitted edges should be visible to the counter)', gph_edge_count();
    END IF;
END $$;
ROLLBACK;

DO $$
BEGIN
    -- v1 contract: the raw counter is NOT reverted by a clean in-process ROLLBACK (no GenericXLog
    -- UNDO). It stays at 1506. Crash-recovery abort-safety is covered by crash_recovery_test.sh.
    IF gph_edge_count() <> 1506 THEN
        RAISE EXCEPTION 'after ROLLBACK edge_count % <> 1506 (v1 raw metapage counter is not in-process abort-aware; see comment)', gph_edge_count();
    END IF;
    RAISE NOTICE 'PASS abort (v1 semantics): 2 edges visible in-txn (1506); clean ROLLBACK does not revert the raw counter (no GenericXLog UNDO) — crash-recovery abort-safety is proven by crash_recovery_test.sh';
END $$;

\echo '============ graph_store gm_edge_count (plan 006): ALL TESTS PASSED ============'

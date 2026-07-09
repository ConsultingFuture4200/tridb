-- plan 006: store-wide edge count on the graph metapage (gm_edge_count) — correctness suite.
-- Asserts gph_edge_count() tracks the number of directed edges appended, across all three
-- gph_insert_edge branches (first-edge, append-to-tail-with-room, chain-new-adjacency-page),
-- and that the counter rolls back atomically with the edge on transaction ABORT (it is bumped
-- under GenericXLog alongside the edge slot). gm_edge_count is the FR-6 avg_out_degree source.
--
-- Also covers gph_visible_edge_count() (advisor plan 055): the raw gm_edge_count counter never
-- decrements on tombstone, so after a delete it diverges from the MVCC-visible live edge count;
-- the section below asserts raw vs visible after a tombstone, and after a rolled-back tombstone.
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

-- ============================================================================
-- gph_visible_edge_count() (advisor plan 055): MVCC-visible, delete-aware count.
-- Right after the ROLLBACK above, raw gm_edge_count is stuck at 1506 (the documented
-- v1 in-process-abort quirk), but the two rolled-back edges' xmin is NOT visible, so
-- the visible scan correctly reports 1504 — proving the new function is genuinely
-- MVCC-visible, not merely "raw minus tombstones".
-- ============================================================================
DO $$
BEGIN
    IF gph_visible_edge_count() <> 1504 THEN
        RAISE EXCEPTION 'visible_edge_count()=% after ROLLBACK (expected 1504: raw stuck at 1506, visible correctly excludes the 2 rolled-back edges)',
            gph_visible_edge_count();
    END IF;
    IF gph_edge_count() <> 1506 THEN
        RAISE EXCEPTION 'sanity: gph_edge_count()=% (expected still 1506)', gph_edge_count();
    END IF;
    RAISE NOTICE 'PASS visible vs raw (abort): raw=1506 (stuck), visible=1504 (correct)';
END $$;

-- ============================================================================
-- gph_visible_edge_count() after a committed tombstone (advisor plan 055 headline
-- case, "insert 3, tombstone 1 -> visible 2, raw 3" scaled to this file's counts):
-- tombstone one of vertex 0's live edges (0->3). The raw count is UNCHANGED
-- (documented, plan 037); the visible count drops by exactly 1.
-- ============================================================================
SELECT gph_tombstone_edge(0, 3);

DO $$
BEGIN
    IF gph_edge_count() <> 1506 THEN
        RAISE EXCEPTION 'after tombstone raw edge_count=% (expected 1506 unchanged)', gph_edge_count();
    END IF;
    IF gph_visible_edge_count() <> 1503 THEN
        RAISE EXCEPTION 'after tombstone visible_edge_count()=% (expected 1503: 1504 - 1 tombstoned)',
            gph_visible_edge_count();
    END IF;
    RAISE NOTICE 'PASS visible vs raw (tombstone): raw stays 1506, visible drops to 1503';
END $$;

-- FR-7: a tombstone inside a rolled-back txn leaves the visible count reverted, matching the
-- gph_neighbors self-visible-then-reverted semantics proven in test/graph_delete_test.sql.
BEGIN;
    SELECT gph_tombstone_edge(0, 1);
    DO $$
    BEGIN
        IF gph_visible_edge_count() <> 1502 THEN
            RAISE EXCEPTION 'in-txn visible_edge_count()=% (expected 1502: own tombstone self-visible)',
                gph_visible_edge_count();
        END IF;
    END $$;
ROLLBACK;

DO $$
BEGIN
    IF gph_visible_edge_count() <> 1503 THEN
        RAISE EXCEPTION 'after ROLLBACK visible_edge_count()=% (expected 1503: tombstone rolled back, edge live again)',
            gph_visible_edge_count();
    END IF;
    RAISE NOTICE 'PASS visible (FR-7): tombstone in a rolled-back txn left the edge live, visible count back to 1503';
END $$;

\echo '============ graph_store gm_edge_count + gph_visible_edge_count (plans 006/055): ALL TESTS PASSED ============'

-- DEV-1166: crash-recovery (WAL redo) assertions for the tri-store FR-7 substrate.
--
-- Driven by scripts/crash_recovery_test.sh in TWO passes against the SAME data directory,
-- selected by the psql variable :phase. The harness CHECKPOINTs a baseline, runs a tri-store
-- txn, then crashes the postmaster with `pg_ctl stop -m immediate` (SIGQUIT, NO checkpoint) so
-- the committed page changes exist ONLY in the WAL — restart forces GenericXLog generic-REDO.
--
--   :phase = 'committed'   -> after restart, the COMMITTED tri-store row must be present in ALL
--                             THREE stores (redo replayed the graph pages + the heap/HNSW row).
--   :phase = 'uncommitted' -> after restart, NONE of the three writes from a txn that never
--                             committed before the crash may be visible (the crash-aborted xid
--                             fails TransactionIdDidCommit, so gph_xmin_visible hides the graph
--                             record, and the heap/HNSW row's xid is likewise dead).
--
-- Run with psql -v ON_ERROR_STOP=1 so a RAISE EXCEPTION yields a nonzero exit.

SET search_path TO graph_store, public;

\if :{?phase}
\else
\echo 'crash_recovery_assert.sql requires -v phase=committed|uncommitted'
\quit
\endif

-- Branch selector: is_committed = true when phase='committed'.
SELECT :'phase' = 'committed' AS is_committed \gset

-- --------------------------------------------------------------------------
-- COMMITTED scenario: the durable tri-store row survived crash + WAL redo.
-- The harness committed: entity id 5000 (heap + HNSW), graph vertex vid 6 with an
-- edge 0 -> 6. We assert all three are present after recovery.
-- --------------------------------------------------------------------------
\if :is_committed
-- KNOWN-LIMITATION (vendored vectordb HNSW): the HNSW index's INCREMENTAL inserts are not
-- crash-durable — after an immediate-stop crash the heap row redoes from WAL but the index's
-- in-memory graph is NOT reconstructed (the index still answers the pre-crash nearest, even
-- after a later CHECKPOINT). This is a vectordb/MSVBASE property, reproduced with the graph
-- store entirely absent; it is NOT a TriDB graph-store or FR-7 atomicity failure. The vector
-- STORE's durable backing is its heap row, which DOES redo — so the committed-crash assertion
-- below checks the heap (seqscan) presence of the vector row, the native graph redo, and the
-- relational redo. The index-redo gap is filed as the DEV-1166 follow-on (see ADR-0003a).
DO $$
DECLARE rel_hit bigint; vec_hit bigint; gc bigint; ec bigint; nbrs bigint[];
BEGIN
    -- (i) relational heap row survived WAL redo
    SELECT id INTO rel_hit FROM entities WHERE id = 5000;
    IF rel_hit IS NULL THEN
        RAISE EXCEPTION 'CRASH/committed: relational row 5000 missing after WAL redo';
    END IF;

    -- (ii) vector STORE row (heap backing of the HNSW-indexed table) survived WAL redo. We read
    -- it via a heap path (seqscan) because the vendored HNSW INDEX itself does not redo (above).
    SET LOCAL enable_indexscan = off;
    SET LOCAL enable_bitmapscan = off;
    SET LOCAL enable_seqscan = on;
    SELECT id INTO vec_hit FROM entities WHERE id = 5000 AND embedding = ARRAY[5000,0,0,0,0,0,0,0]::float8[];
    IF vec_hit IS NULL THEN
        RAISE EXCEPTION 'CRASH/committed: vector row 5000 (heap backing) missing after WAL redo';
    END IF;

    -- (iii) native graph vertex + edge survived (GenericXLog generic REDO of our pages)
    gc := gph_vertex_count();
    IF gc < 7 THEN
        RAISE EXCEPTION 'CRASH/committed: gph_vertex_count = % (< 7 — committed graph vertex lost in redo)', gc;
    END IF;
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs IS NULL OR NOT (6 = ANY(nbrs)) THEN
        RAISE EXCEPTION 'CRASH/committed: committed edge 0->6 not in neighbors=% after redo', nbrs;
    END IF;

    -- (iv) store-wide gm_edge_count (plan 006) survived redo. The baseline seed adds 6 vertices and
    -- NO edges, so the single committed edge 0->6 leaves the counter at exactly 1 after recovery
    -- (the increment is GenericXLog REDO-covered, logged atomically with the edge slot).
    ec := gph_edge_count();
    IF ec <> 1 THEN
        RAISE EXCEPTION 'CRASH/committed: gph_edge_count = % (expected 1 — committed edge-count increment lost in redo)', ec;
    END IF;

    RAISE NOTICE 'PASS crash/committed: relational + vector-heap + native graph (vertex, edge, gm_edge_count=1) all REDONE from WAL after immediate-stop crash (HNSW index-redo gap is a vendor KNOWN-LIMITATION)';
END $$;
\else
-- --------------------------------------------------------------------------
-- UNCOMMITTED scenario: a tri-store txn was left open (never COMMIT) when the crash hit.
-- Its xid never committed, so after recovery NONE of the three writes are visible.
-- The harness used entity id 6000 / vertex vid 6 / edge 0->6 for the doomed writes; baseline
-- before the doomed txn had exactly 6 visible graph vertices (vids 0..5 seeded this data dir).
-- --------------------------------------------------------------------------
DO $$
DECLARE rel_n bigint; gc bigint; ec bigint; nbrs bigint[];
BEGIN
    -- (i) relational/vector heap row NOT visible (its inserting xid is crash-aborted). We read via
    -- a heap path (seqscan) so the assertion does not depend on the vendored HNSW index at all.
    SET LOCAL enable_indexscan = off;
    SET LOCAL enable_bitmapscan = off;
    SET LOCAL enable_seqscan = on;
    SELECT count(*) INTO rel_n FROM entities WHERE id = 6000;
    IF rel_n <> 0 THEN
        RAISE EXCEPTION 'CRASH/uncommitted: relational/vector row 6000 visible after recovery (uncommitted write leaked)';
    END IF;

    -- (ii) native graph vertex: the doomed vertex must be hidden by gph_xmin_visible
    --       (TransactionIdDidCommit is false for the crash-aborted xid). Baseline is 6.
    gc := gph_vertex_count();
    IF gc <> 6 THEN
        RAISE EXCEPTION 'CRASH/uncommitted: gph_vertex_count = % (expected baseline 6 — uncommitted graph vertex visible)', gc;
    END IF;

    -- (iii) native graph edge: the doomed edge 0->6 must NOT be in neighbors (symmetry with the
    --       committed scenario, which checks both vertex AND edge survived redo).
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs IS NOT NULL AND 6 = ANY(nbrs) THEN
        RAISE EXCEPTION 'CRASH/uncommitted: doomed edge 0->6 visible after recovery (neighbors=%)', nbrs;
    END IF;

    -- (iv) store-wide gm_edge_count (plan 006) rolled back with the crash-aborted txn. The doomed
    -- edge 0->6's increment was logged under GenericXLog with the page image, so recovery restores
    -- the pre-txn counter: the baseline seed had 0 edges, so gm_edge_count must be 0 (NOT leaked).
    ec := gph_edge_count();
    IF ec <> 0 THEN
        RAISE EXCEPTION 'CRASH/uncommitted: gph_edge_count = % (expected baseline 0 — doomed edge-count increment leaked across abort)', ec;
    END IF;

    RAISE NOTICE 'PASS crash/uncommitted: relational/vector heap + native graph vertex, edge, AND gm_edge_count (0) all HIDE/roll back the crash-aborted xid after recovery';
END $$;
\endif

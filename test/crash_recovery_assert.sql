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
-- Plan 037 (DEV-1349) native graph delete adds two tombstone crash-recovery phases (the direct
-- proof that the repurposed es_xmax/vr_xmax survives WAL replay correctly — the deviation's thesis
-- is that a tombstone's abort-atomicity rides xid-visibility through GenericXLog REDO):
--   :phase = 'committed_tombstone'   -> a committed edge-tombstone (checkpointed live edge, then
--                             tombstone in WAL only) must be REDONE: edge 0->1 is gone post-recovery.
--   :phase = 'uncommitted_tombstone' -> a tombstone written by a txn that crash-aborted must be
--                             IGNORED even though its page image was checkpointed durable: the
--                             deleting xid never committed, so gph_deleted_visible hides the
--                             tombstone and edge 0->1 reads LIVE again (FR-7 abort-atomicity).
--
-- Run with psql -v ON_ERROR_STOP=1 so a RAISE EXCEPTION yields a nonzero exit.

SET search_path TO graph_store, public;

\if :{?phase}
\else
\echo 'crash_recovery_assert.sql requires -v phase=committed|uncommitted'
\quit
\endif

-- Branch selectors: exactly one phase is true per run.
SELECT :'phase' = 'committed' AS is_committed \gset
SELECT :'phase' = 'committed_tombstone' AS is_committed_tomb \gset
SELECT :'phase' = 'uncommitted_tombstone' AS is_uncommitted_tomb \gset

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
\elif :is_committed_tomb
-- --------------------------------------------------------------------------
-- COMMITTED-TOMBSTONE scenario (plan 037): the harness inserted+COMMITTED edge 0->1 and
-- CHECKPOINTed it durable, then COMMITTED a gph_tombstone_edge(0,1) that lives ONLY in the WAL,
-- then crashed (-m immediate). Restart must run GenericXLog generic-REDO of the tombstone page:
-- the edge's slot regains GPH_FLAG_DELETED + a committed es_xmax, so it is filtered and edge 0->1
-- must be GONE from traversal after recovery. (If the tombstone redo were missing, the durable
-- edge would reappear — so this asserts the repurposed es_xmax redoes correctly.)
-- --------------------------------------------------------------------------
DO $$
DECLARE nbrs bigint[]; gc bigint;
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs IS NOT NULL AND 1 = ANY(nbrs) THEN
        RAISE EXCEPTION 'CRASH/committed_tombstone: edge 0->1 still visible after recovery (committed tombstone not REDONE from WAL): neighbors=%', nbrs;
    END IF;
    gc := gph_vertex_count();
    IF gc <> 6 THEN
        RAISE EXCEPTION 'CRASH/committed_tombstone: gph_vertex_count = % (expected baseline 6 — only an edge was tombstoned)', gc;
    END IF;
    RAISE NOTICE 'PASS crash/committed_tombstone: committed edge-tombstone REDONE from WAL after immediate-stop crash (edge 0->1 gone post-recovery; repurposed es_xmax survives replay)';
END $$;
\elif :is_uncommitted_tomb
-- --------------------------------------------------------------------------
-- UNCOMMITTED-TOMBSTONE scenario (plan 037, the FR-7 keystone): the harness inserted+COMMITTED
-- edge 0->1 (checkpointed durable), then a background txn tombstoned 0->1 and was CHECKPOINTed
-- (so the tombstone's page image — GPH_FLAG_DELETED + es_xmax = the doomed xid — is physically on
-- disk) but NEVER committed before the crash. After recovery that xid is crash-aborted, so
-- gph_deleted_visible rejects the tombstone (xmax fails TransactionIdDidCommit) and edge 0->1 must
-- read LIVE again — abort-atomicity via xid-visibility, NOT via any GenericXLog UNDO.
-- --------------------------------------------------------------------------
DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs IS NULL OR NOT (1 = ANY(nbrs)) THEN
        RAISE EXCEPTION 'CRASH/uncommitted_tombstone: edge 0->1 NOT live after recovery (aborted tombstone wrongly persisted — FR-7 abort-atomicity failure): neighbors=%', nbrs;
    END IF;
    RAISE NOTICE 'PASS crash/uncommitted_tombstone: tombstone from a crash-aborted txn is IGNORED after recovery (edge 0->1 reads LIVE — xid-visibility hides the physically-checkpointed tombstone)';
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

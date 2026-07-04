-- plan 037 (DEV-1349): native graph delete via gph_tombstone_edge/vertex — correctness suite.
-- Asserts the soft-delete (tombstone) path: an edge/vertex tombstoned by gph_tombstone_* vanishes
-- from traversal and gph_vertex_count immediately; the tombstone is IDEMPOTENT; and — the keystone
-- FR-7 property — a tombstone written inside a rolled-back txn leaves the record PRESENT after
-- ROLLBACK (the delete is stamped with the deleting xid in the repurposed es_xmax/vr_xmax field and
-- honored only when that xid is visible, exactly as an INSERT is honored only when its xmin is
-- visible — so delete rolls back atomically with the host txn, not via any GenericXLog UNDO).
--
-- UNBUILT-HERE (GX10-gated): the graph store access method compiles only inside the MSVBASE fork
-- (PG 13.4, --with-blocksize=32). Run by scripts/graph_delete_test.sh on target.

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;	-- the extension installs into the graph_store schema

-- 6 vertices -> dense vids 0..5 (auto-committed, visible). Edges: 0->{1,2,3}, 1->{4}.
SELECT gph_insert_vertex() FROM generate_series(1, 6);
SELECT gph_insert_edge(0, 1);
SELECT gph_insert_edge(0, 2);
SELECT gph_insert_edge(0, 3);
SELECT gph_insert_edge(1, 4);

DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION 'setup: neighbors(0)=% (expected {1,2,3})', nbrs;
    END IF;
    IF gph_edge_count() <> 4 OR gph_vertex_count() <> 6 THEN
        RAISE EXCEPTION 'setup: edge_count=% vertex_count=% (expected 4,6)',
            gph_edge_count(), gph_vertex_count();
    END IF;
    RAISE NOTICE 'PASS setup: 6 vertices, neighbors(0)={1,2,3}';
END $$;

-- ============================================================================
-- Test A — edge tombstone + idempotency. Tombstone 0->2; it leaves traversal.
-- Re-tombstoning it (and tombstoning an absent edge) is a no-op, not an error.
-- The raw gm_edge_count is deliberately unchanged (reclamation is plan 036).
-- ============================================================================
SELECT gph_tombstone_edge(0, 2);

DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,3]::bigint[] THEN
        RAISE EXCEPTION 'A: neighbors(0)=% after tombstone 0->2 (expected {1,3})', nbrs;
    END IF;
    -- idempotent: re-tombstone the already-deleted edge, and an absent edge.
    PERFORM gph_tombstone_edge(0, 2);
    PERFORM gph_tombstone_edge(0, 99);   -- absent edge => no-op
    PERFORM gph_tombstone_edge(42, 2);   -- absent src => no-op
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,3]::bigint[] THEN
        RAISE EXCEPTION 'A: neighbors(0)=% after idempotent re-tombstone (expected {1,3})', nbrs;
    END IF;
    IF gph_edge_count() <> 4 THEN
        RAISE EXCEPTION 'A: gph_edge_count()=% (expected 4 — raw counter is not decremented)',
            gph_edge_count();
    END IF;
    RAISE NOTICE 'PASS A (edge tombstone): 0->2 gone, idempotent, raw edge_count unchanged';
END $$;

-- ============================================================================
-- Test B — FR-7: an edge tombstone inside a rolled-back txn leaves the edge PRESENT.
-- In-txn the delete is self-visible (deleting xid is the current txn); after ROLLBACK
-- the deleting xid aborted, so the tombstone is ignored and 0->1 is live again.
-- ============================================================================
BEGIN;
    SELECT gph_tombstone_edge(0, 1);
    DO $$
    DECLARE nbrs bigint[];
    BEGIN
        SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
        IF nbrs <> ARRAY[3]::bigint[] THEN
            RAISE EXCEPTION 'B(in-txn): neighbors(0)=% (expected {3}: own tombstone self-visible)', nbrs;
        END IF;
    END $$;
ROLLBACK;

DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,3]::bigint[] THEN
        RAISE EXCEPTION 'B: neighbors(0)=% after ROLLBACK (expected {1,3}: 0->1 tombstone rolled back)', nbrs;
    END IF;
    RAISE NOTICE 'PASS B (FR-7 edge): tombstone in a rolled-back txn left 0->1 PRESENT';
END $$;

-- ============================================================================
-- Test C — vertex tombstone. tombstone_vertex(1): vertex 1 is invisible as a source
-- (its out-edge 1->4 vanishes) and drops out of gph_vertex_count. Its dangling
-- IN-edge 0->1 is NOT swept (no reverse index in v1); traversal from 0 still yields
-- the edge, but vertex 1 itself reads deleted (documented in-edge semantics, plan 038).
-- ============================================================================
SELECT gph_tombstone_vertex(1);

DO $$
DECLARE nbrs1 bigint[]; nbrs0 bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs1 FROM gph_neighbors(1) x;
    IF nbrs1 IS NOT NULL THEN
        RAISE EXCEPTION 'C: neighbors(1)=% (expected empty: tombstoned vertex has no out-edges)', nbrs1;
    END IF;
    IF gph_vertex_count() <> 5 THEN
        RAISE EXCEPTION 'C: gph_vertex_count()=% (expected 5: vertex 1 tombstoned)', gph_vertex_count();
    END IF;
    -- dangling in-edge 0->1 is still emitted (documented v1 semantics; target reads deleted).
    SELECT array_agg(x ORDER BY x) INTO nbrs0 FROM gph_neighbors(0) x;
    IF nbrs0 <> ARRAY[1,3]::bigint[] THEN
        RAISE EXCEPTION 'C: neighbors(0)=% (expected {1,3}: in-edge to tombstoned 1 not swept)', nbrs0;
    END IF;
    RAISE NOTICE 'PASS C (vertex tombstone): vertex 1 invisible as source, out-edge gone, count=5';
END $$;

-- ============================================================================
-- Test D — FR-7: a vertex tombstone inside a rolled-back txn leaves the vertex PRESENT.
-- ============================================================================
BEGIN;
    SELECT gph_tombstone_vertex(0);
    DO $$
    BEGIN
        IF gph_vertex_count() <> 4 THEN
            RAISE EXCEPTION 'D(in-txn): gph_vertex_count()=% (expected 4: own vertex tombstone self-visible)',
                gph_vertex_count();
        END IF;
    END $$;
ROLLBACK;

DO $$
DECLARE nbrs bigint[];
BEGIN
    IF gph_vertex_count() <> 5 THEN
        RAISE EXCEPTION 'D: gph_vertex_count()=% after ROLLBACK (expected 5: vertex 0 tombstone rolled back)',
            gph_vertex_count();
    END IF;
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,3]::bigint[] THEN
        RAISE EXCEPTION 'D: neighbors(0)=% after ROLLBACK (expected {1,3}: vertex 0 live again)', nbrs;
    END IF;
    RAISE NOTICE 'PASS D (FR-7 vertex): vertex tombstone in a rolled-back txn left vertex 0 PRESENT';
END $$;

-- ============================================================================
-- Test E — remove_edge external-id compat (the removeLink twin of add_edge).
-- add_edge auto-creates + maps arbitrary external ids; remove_edge tombstones the
-- src->dst edge over those same external ids and no-ops on an absent/unmapped edge.
-- ============================================================================
SELECT add_edge(100, 200);
SELECT add_edge(100, 300);

DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM neighbors(100) x;
    IF nbrs <> ARRAY[200,300]::bigint[] THEN
        RAISE EXCEPTION 'E: neighbors(100)=% (expected {200,300})', nbrs;
    END IF;

    PERFORM remove_edge(100, 200);
    PERFORM remove_edge(100, 999);   -- unmapped dst => STRICT gph_tombstone_edge no-op
    PERFORM remove_edge(500, 200);   -- unmapped src => no-op

    SELECT array_agg(x ORDER BY x) INTO nbrs FROM neighbors(100) x;
    IF nbrs <> ARRAY[300]::bigint[] THEN
        RAISE EXCEPTION 'E: neighbors(100)=% after remove_edge(100,200) (expected {300})', nbrs;
    END IF;
    RAISE NOTICE 'PASS E (remove_edge compat): external-id 100->200 removed, 100->300 kept';
END $$;

\echo '============ graph_store native delete (plan 037): ALL TESTS PASSED ============'

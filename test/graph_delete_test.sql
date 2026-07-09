-- plan 037 (DEV-1349): native graph delete via gph_tombstone_edge/vertex — correctness suite.
-- Asserts the soft-delete (tombstone) path: an edge/vertex tombstoned by gph_tombstone_* vanishes
-- from traversal and gph_vertex_count immediately; the tombstone is IDEMPOTENT; and — the keystone
-- FR-7 property — a tombstone written inside a rolled-back txn leaves the record PRESENT after
-- ROLLBACK (the delete is stamped with the deleting xid in the repurposed es_xmax/vr_xmax field and
-- honored only when that xid is visible, exactly as an INSERT is honored only when its xmin is
-- visible — so delete rolls back atomically with the host txn, not via any GenericXLog UNDO).
--
-- Test F (advisor plan 045): gph_tombstone_edge now filters by es_edge_type_id (default
-- RELATED_TO), so a typed edge (plan 038) co-located with a related_to edge between the same
-- src/dst survives an untyped tombstone; the 3-arg overload takes an explicit type id, or
-- GPH_EDGE_TYPE_ANY (0) for the old all-type wipe.
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

-- ============================================================================
-- Test F — typed tombstone (advisor plan 045 / DEV-1354 follow-up). Before this fix,
-- gph_tombstone_edge matched on dst ALONE (no es_edge_type_id check), so tombstoning a
-- related_to edge between two vertices also silently wiped any co-located typed edge (plan 038)
-- between the SAME endpoints. Fresh vertices 6,7 to avoid interaction with the earlier tests'
-- tombstones. Three edges 6->7: related_to (default), works_at, mentions.
-- ============================================================================
SELECT gph_insert_vertex() FROM generate_series(1, 2);   -- vids 6, 7

DO $$
DECLARE w int; m int;
BEGIN
    w := register_edge_type('works_at');   -- id 2
    m := register_edge_type('mentions');   -- id 3
    IF (w, m) IS DISTINCT FROM (2, 3) THEN
        RAISE EXCEPTION 'F setup: works_at=%, mentions=% (expected 2,3)', w, m;
    END IF;
END $$;

SELECT gph_insert_edge(6, 7);        -- related_to (default 2-arg)
SELECT gph_insert_edge(6, 7, 2);     -- works_at
SELECT gph_insert_edge(6, 7, 3);     -- mentions

-- F1: default (2-arg) tombstone only removes the related_to edge; works_at/mentions survive.
SELECT gph_tombstone_edge(6, 7);

DO $$
DECLARE all_types bigint[]; rel bigint[]; work bigint[]; ment bigint[];
BEGIN
    SELECT array_agg(dst) INTO rel  FROM gph_traverse_typed(6, 1, 0, -1);   -- related_to
    SELECT array_agg(dst) INTO work FROM gph_traverse_typed(6, 2, 0, -1);   -- works_at
    SELECT array_agg(dst) INTO ment FROM gph_traverse_typed(6, 3, 0, -1);   -- mentions
    SELECT array_agg(dst) INTO all_types FROM gph_traverse_typed(6, 0, 0, -1);  -- any type

    IF rel IS NOT NULL THEN
        RAISE EXCEPTION 'F1: related_to(6->7)=% after default tombstone (expected gone)', rel;
    END IF;
    IF work <> ARRAY[7]::bigint[] THEN
        RAISE EXCEPTION 'F1: works_at(6->7)=% (expected {7}: typed edge must survive an untyped tombstone)', work;
    END IF;
    IF ment <> ARRAY[7]::bigint[] THEN
        RAISE EXCEPTION 'F1: mentions(6->7)=% (expected {7}: typed edge must survive an untyped tombstone)', ment;
    END IF;
    IF all_types <> ARRAY[7,7]::bigint[] THEN
        RAISE EXCEPTION 'F1: any-type(6->7)=% (expected two surviving typed edges {7,7})', all_types;
    END IF;
    RAISE NOTICE 'PASS F1 (typed tombstone default): related_to gone, works_at + mentions survive';
END $$;

-- F2: explicit-type 3-arg tombstone removes only the named type (mentions); works_at still lives.
SELECT gph_tombstone_edge(6, 7, 3);

DO $$
DECLARE work bigint[]; ment bigint[];
BEGIN
    SELECT array_agg(dst) INTO work FROM gph_traverse_typed(6, 2, 0, -1);
    SELECT array_agg(dst) INTO ment FROM gph_traverse_typed(6, 3, 0, -1);
    IF ment IS NOT NULL THEN
        RAISE EXCEPTION 'F2: mentions(6->7)=% after explicit-type tombstone (expected gone)', ment;
    END IF;
    IF work <> ARRAY[7]::bigint[] THEN
        RAISE EXCEPTION 'F2: works_at(6->7)=% (expected {7}: untouched by mentions tombstone)', work;
    END IF;
    RAISE NOTICE 'PASS F2 (explicit-type tombstone): mentions gone, works_at untouched';
END $$;

-- F3: GPH_EDGE_TYPE_ANY (0) sentinel explicitly requests the old all-type wipe (documented
-- migration path for a caller that depended on it) — removes the remaining works_at edge too.
SELECT gph_tombstone_edge(6, 7, 0);

DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM gph_traverse_typed(6, 0, 0, -1);
    IF n <> 0 THEN
        RAISE EXCEPTION 'F3: any-type(6->7) count=% after ANY-sentinel tombstone (expected 0)', n;
    END IF;
    RAISE NOTICE 'PASS F3 (ANY-sentinel tombstone): all remaining types wiped';
END $$;

\echo '============ graph_store native delete (plan 037 + typed tombstone, plan 045): ALL TESTS PASSED ============'

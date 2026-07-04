-- DEV-1350 / advisor plan 038: typed + directional + source-scoped native traversal.
-- Exercises the edge_type dictionary, the 3-arg typed gph_insert_edge overload, and the typed
-- gph_traverse_typed(src, type_id, direction, source_id) SRF: filter by one type / any type /
-- wrong type (empty), source scope, direction=in/both rejection, TR-1 early termination on the
-- typed stream, and the DEFAULT-PATH PARITY ORACLE (typing is invisible to gph_traverse /
-- gph_neighbors on RELATED_TO edges). Run by scripts/graph_typed_traversal_test.sh (GX10/image).

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;

-- 8 vertices (dense vids 0..7).
SELECT gph_insert_vertex() FROM generate_series(1, 8);

-- Dictionary: built-in related_to=1; register two gBrain link types. Idempotent + max+1 alloc.
DO $$
DECLARE r int; w int; me int;
BEGIN
    w  := register_edge_type('works_at');   -- first free id => 2
    me := register_edge_type('mentions');   -- next            => 3
    r  := register_edge_type('related_to');  -- already built in => 1 (idempotent)
    IF (w, me, r) IS DISTINCT FROM (2, 3, 1) THEN
        RAISE EXCEPTION 'edge_type dictionary: got works_at=%, mentions=%, related_to=% (expected 2,3,1)', w, me, r;
    END IF;
    IF register_edge_type('works_at') <> 2 THEN
        RAISE EXCEPTION 'register_edge_type not idempotent: re-register works_at gave %', register_edge_type('works_at');
    END IF;
    RAISE NOTICE 'PASS dictionary: related_to=1, works_at=2, mentions=3 (idempotent)';
END $$;

-- Mixed-type edges out of vertex 0: 0->1 related_to (default 2-arg), 0->2 works_at, 0->3 mentions,
-- 0->4 works_at.
SELECT gph_insert_edge(0, 1);        -- default => related_to (id 1), byte-identical to pre-038
SELECT gph_insert_edge(0, 2, 2);     -- works_at
SELECT gph_insert_edge(0, 3, 3);     -- mentions
SELECT gph_insert_edge(0, 4, 2);     -- works_at

-- Typed round-trip + one-type filter: works_at (id 2) => {2,4} only.
DO $$
DECLARE d bigint[];
BEGIN
    SELECT array_agg(dst ORDER BY dst) INTO d FROM gph_traverse_typed(0, 2, 0, -1);
    IF d IS DISTINCT FROM ARRAY[2,4]::bigint[] THEN
        RAISE EXCEPTION 'type filter works_at: got % (expected {2,4})', d;
    END IF;
    RAISE NOTICE 'PASS one-type filter: works_at => {2,4}';
END $$;

-- Any-type (type_id 0 = GPH_EDGE_TYPE_ANY) => all four out-neighbors.
DO $$
DECLARE d bigint[];
BEGIN
    SELECT array_agg(dst ORDER BY dst) INTO d FROM gph_traverse_typed(0, 0, 0, -1);
    IF d IS DISTINCT FROM ARRAY[1,2,3,4]::bigint[] THEN
        RAISE EXCEPTION 'any-type filter: got % (expected {1,2,3,4})', d;
    END IF;
    RAISE NOTICE 'PASS any-type: => {1,2,3,4}';
END $$;

-- Wrong / unregistered type id => empty (no slot matches).
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM gph_traverse_typed(0, 99, 0, -1);
    IF n <> 0 THEN
        RAISE EXCEPTION 'wrong-type filter: got % rows (expected 0)', n;
    END IF;
    RAISE NOTICE 'PASS wrong-type: unknown type => {}';
END $$;

-- DEFAULT-PATH PARITY ORACLE: on the RELATED_TO subset, the untyped default path
-- (gph_traverse / gph_neighbors) and the explicit related_to typed path all agree on {1} — typing
-- is invisible to pre-038 callers. (The default path filters to RELATED_TO, so the works_at /
-- mentions edges are correctly NOT emitted — this is the pre-038 semantics, not a regression.)
DO $$
DECLARE tv bigint[]; nb bigint[]; ty bigint[];
BEGIN
    SELECT array_agg(dst ORDER BY dst) INTO tv FROM gph_traverse(0);
    SELECT array_agg(x   ORDER BY x)   INTO nb FROM gph_neighbors(0) x;
    SELECT array_agg(dst ORDER BY dst) INTO ty FROM gph_traverse_typed(0, 1, 0, -1);  -- explicit related_to
    IF tv IS DISTINCT FROM ARRAY[1]::bigint[] THEN
        RAISE EXCEPTION 'parity: gph_traverse(0)=% (expected {1}; default filters RELATED_TO)', tv;
    END IF;
    IF nb IS DISTINCT FROM tv OR ty IS DISTINCT FROM tv THEN
        RAISE EXCEPTION 'parity break: gph_traverse=%, gph_neighbors=%, typed(related_to)=%', tv, nb, ty;
    END IF;
    RAISE NOTICE 'PASS parity oracle: default == gph_neighbors == typed(related_to) == {1}';
END $$;

-- Source scope: scoping to source 0 (the actual owner) keeps all edges; scoping to a different
-- source vid excludes them (es_src_vid filter). Adjacency chains are per-vertex so es_src_vid is
-- uniform per scan — this asserts the scope filter excludes cross-source edges.
DO $$
DECLARE d bigint[]; n bigint;
BEGIN
    SELECT array_agg(dst ORDER BY dst) INTO d FROM gph_traverse_typed(0, 0, 0, 0);   -- scope = owner 0
    IF d IS DISTINCT FROM ARRAY[1,2,3,4]::bigint[] THEN
        RAISE EXCEPTION 'source scope (owner): got % (expected {1,2,3,4})', d;
    END IF;
    SELECT count(*) INTO n FROM gph_traverse_typed(0, 0, 0, 5);                       -- scope = other source
    IF n <> 0 THEN
        RAISE EXCEPTION 'source scope (cross): got % rows (expected 0 — cross-source excluded)', n;
    END IF;
    RAISE NOTICE 'PASS source scope: owner => {1,2,3,4}, cross-source => {}';
END $$;

-- Direction in/both are not supported (out-only adjacency): both must RAISE, not silently drop
-- in-edges (honest scoping — reverse index is a follow-on, docs/decisions/0016).
DO $$
BEGIN
    BEGIN
        PERFORM * FROM gph_traverse_typed(0, 0, 1, -1);  -- direction=in
        RAISE EXCEPTION 'direction=in did NOT raise (must reject; reverse index deferred)';
    EXCEPTION WHEN feature_not_supported THEN
        NULL;  -- expected
    END;
    BEGIN
        PERFORM * FROM gph_traverse_typed(0, 0, 2, -1);  -- direction=both
        RAISE EXCEPTION 'direction=both did NOT raise (must reject; reverse index deferred)';
    EXCEPTION WHEN feature_not_supported THEN
        NULL;  -- expected
    END;
    RAISE NOTICE 'PASS direction guard: in/both raise feature_not_supported';
END $$;

-- Multi-page chaining + TR-1 early termination on the TYPED stream. 1500 related_to edges from
-- vertex 6 span two 32KB adjacency pages (1022 EdgeSlots/page).
SELECT gph_insert_edge(6, g % 5) FROM generate_series(1, 1500) g;

DO $$
DECLARE total bigint; v0 bigint; v1 bigint;
BEGIN
    SELECT count(*) INTO total FROM gph_traverse_typed(6, 0, 0, -1);
    IF total <> 1500 THEN
        RAISE EXCEPTION 'typed full scan gph_traverse_typed(6,any) = % (expected 1500)', total;
    END IF;

    v0 := gph_visits();
    -- target-list SRF (nodeProjectSet) is pull-based, so LIMIT stops the iterator early.
    PERFORM gph_traverse_typed(6, 0, 0, -1) LIMIT 5;
    v1 := gph_visits();
    IF v1 - v0 <> 5 THEN
        RAISE EXCEPTION 'typed early termination broken: LIMIT 5 did % edge-steps (expected 5)', v1 - v0;
    END IF;
    RAISE NOTICE 'PASS typed early termination: LIMIT 5 => 5 edge-steps, 2nd adj page untouched (TR-1)';
END $$;

\echo '============ typed/directional/source-scoped traversal (plan 038, DEV-1350): ALL TESTS PASSED ============'

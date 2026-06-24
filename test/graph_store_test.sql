-- graph_store v0 self-checking tests (DEV-1165 traversal iterator, DEV-1166 FR-7).
-- Runs under psql -v ON_ERROR_STOP=1: any RAISE EXCEPTION fails the suite (nonzero exit).

CREATE EXTENSION graph_store;

-- relational store (stands in for the inherited PostgreSQL/vector leg in FR-7)
CREATE TABLE rel_store (id bigint PRIMARY KEY, payload text);

-- build a small graph: vertex 1 has 3 out-neighbors, vertex 2 has 1.
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 11);
SELECT graph_store.add_edge(1, 12);
SELECT graph_store.add_edge(2, 20);

-- a high-degree vertex (7 -> 100 neighbors) for the early-termination test.
SELECT graph_store.add_edge(7, g) FROM generate_series(1000, 1099) AS g;

-- 1) Traversal correctness ---------------------------------------------------
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(n ORDER BY n) INTO got FROM graph_store.neighbors(1) AS n;
    IF got IS DISTINCT FROM ARRAY[10,11,12]::bigint[] THEN
        RAISE EXCEPTION 'traversal FAILED: neighbors(1) = %', got;
    END IF;
    RAISE NOTICE 'PASS traversal: neighbors(1) = %', got;
END $$;

-- 2) TR-1 early termination --------------------------------------------------
-- Vertex 7 has 100 neighbors; pulling only 3 (LIMIT 3) must do a small constant
-- amount of traversal work (~k), NOT visit all 100 — the iterator stops early
-- rather than materializing the full adjacency. (The executor may pull one past
-- the limit, so we assert a small bound, not an exact count.)
DO $$
DECLARE before bigint; after bigint; delta bigint;
BEGIN
    SELECT graph_store.visits() INTO before;
    -- Target-list SRF form: ProjectSet -> Limit is pulled lazily (the FROM-clause
    -- form materializes via ExecMakeTableFunctionResult and would NOT early-terminate).
    PERFORM graph_store.neighbors(7) LIMIT 3;
    SELECT graph_store.visits() INTO after;
    delta := after - before;
    IF delta > 10 THEN
        RAISE EXCEPTION 'early-termination FAILED: visited % of 100 neighbors under LIMIT 3', delta;
    END IF;
    RAISE NOTICE 'PASS early termination: LIMIT 3 visited % of 100 neighbors (iterator stopped early)', delta;
END $$;

-- 3) FR-7 shared transaction manager: cross-store ATOMIC ROLLBACK ------------
BEGIN;
    INSERT INTO rel_store VALUES (999, 'doomed');
    SELECT graph_store.add_edge(999, 1000);
ROLLBACK;
DO $$
DECLARE r int; g int;
BEGIN
    SELECT count(*) INTO r FROM rel_store WHERE id = 999;
    SELECT count(*) INTO g FROM graph_store.adjacency WHERE vid = 999;
    IF r <> 0 OR g <> 0 THEN
        RAISE EXCEPTION 'FR-7 rollback FAILED: rel=% graph=% (expected 0,0)', r, g;
    END IF;
    RAISE NOTICE 'PASS FR-7 rollback: both stores empty after ROLLBACK (one shared txn mgr)';
END $$;

-- 4) FR-7 cross-store ATOMIC COMMIT -----------------------------------------
BEGIN;
    INSERT INTO rel_store VALUES (5, 'kept');
    SELECT graph_store.add_edge(5, 50);
COMMIT;
DO $$
DECLARE r int; g int;
BEGIN
    SELECT count(*) INTO r FROM rel_store WHERE id = 5;
    SELECT count(*) INTO g FROM graph_store.adjacency WHERE vid = 5;
    IF r <> 1 OR g <> 1 THEN
        RAISE EXCEPTION 'FR-7 commit FAILED: rel=% graph=% (expected 1,1)', r, g;
    END IF;
    RAISE NOTICE 'PASS FR-7 commit: both stores durable after COMMIT';
END $$;

\echo '===================== graph_store v0: ALL TESTS PASSED ====================='

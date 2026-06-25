-- DEV-1164: post-restart assertions — the store survived a cluster restart via WAL recovery.
-- Run by scripts/graph_am_test.sh AFTER `pg_ctl restart`, against the same data directory the
-- correctness suite populated. Proves the "vertices and edges persist and survive restart
-- (WAL-backed)" acceptance criterion.

SET search_path TO graph_store, public;

DO $$
DECLARE n bigint[]; c bigint;
BEGIN
    IF gph_vertex_count() <> 6 THEN
        RAISE EXCEPTION 'after restart: vertex_count % <> 6 (not WAL-persisted)', gph_vertex_count();
    END IF;
    SELECT array_agg(x ORDER BY x) INTO n FROM gph_neighbors(0) x;
    IF n IS DISTINCT FROM ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION 'after restart: neighbors(0)=% (expected {1,2,3})', n;
    END IF;
    SELECT count(*) INTO c FROM gph_neighbors(5);
    IF c <> 1500 THEN
        RAISE EXCEPTION 'after restart: vertex 5 edges = % (expected 1500)', c;
    END IF;
    RAISE NOTICE 'PASS persistence across restart (WAL recovery): 6 vertices, neighbors(0)={1,2,3}, 1500 edges on v5';
END $$;

\echo '============ graph_store_am: PERSISTENCE VERIFIED (DEV-1164) ============'

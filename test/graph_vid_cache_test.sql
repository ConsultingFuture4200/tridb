-- graph_vid_cache_test.sql — ASSERTING test for the backend-local reverse vid cache
-- (advisor plan 034 / DEV-1345, PERF-03). Two oracles:
--   (1) PARITY   — gph_neighbors_ext_cached is byte-identical to the gph_neighbors_ext shim
--                  over every mapped source vertex (sorted neighbor multiset digest).
--   (2) HOOK     — the relcache-invalidation callback flushes the cache on a gph_vid_map
--                  reload: TRUNCATE fires a relcache invalidation, so the next cached probe
--                  rebuilds from the fresh map and surfaces the NEW external ids. If the hook
--                  did NOT fire, the cached probe would keep serving the OLD ids and diverge
--                  from the shim — a silently-stale id cache (plan 034 STOP condition).
--
-- Run: bash scripts/graph_test.sh tridb/msvbase:dev test/graph_vid_cache_test.sql
-- (graph_test.sh installs graph_store_am; this file runs as superuser.) Single backend/session,
-- so the process-local cache + its invalidation are exercised on one connection.

\set ON_ERROR_STOP on

CREATE DATABASE vidcache;
\connect vidcache
CREATE EXTENSION graph_store_am;

-- Deterministic edge set over SPARSE external ids (base 1000000, stride 7) via the Stage-A
-- compat surface (add_edge -> gph_upsert_vertex): a hub of degree 300 plus a pseudo-random
-- tail, so the reverse vid -> ext_id translation is genuinely non-identity (what the cache is
-- for). Pure integer arithmetic, no setseed.
SELECT count(*) FROM (
    SELECT graph_store.add_edge(1000000, 1000000 + 7 * g) FROM generate_series(1, 300) g
) s;
SELECT count(*) FROM (
    SELECT graph_store.add_edge(1000000 + 7 * ((i::bigint * 2654435761) % 500),
                                1000000 + 7 * ((i::bigint * 40503 + 12345) % 500))
    FROM generate_series(1, 400) i
) s;

-- (1) PARITY: cached vs uncached, sorted neighbor multiset digested over every mapped vertex.
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS shim_digest FROM (
    SELECT m.ext_id AS v,
           m.ext_id || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                        FROM graph_store.gph_neighbors_ext(m.ext_id) d), '') AS line
    FROM graph_store.gph_vid_map m
) s \gset
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS cached_digest FROM (
    SELECT m.ext_id AS v,
           m.ext_id || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                        FROM graph_store.gph_neighbors_ext_cached(m.ext_id) d), '') AS line
    FROM graph_store.gph_vid_map m
) s \gset

-- (2) INVALIDATION: bulk-reload gph_vid_map with shifted external ids. The native edge store
-- (vids) is UNTOUCHED, so the identical topology must now surface the NEW external ids. TRUNCATE
-- fires a relcache invalidation on gph_vid_map -> gph_vid_cache_invalidate flushes the cache ->
-- the next probe rebuilds from the fresh map.
CREATE TEMP TABLE remap AS
    SELECT vid, ext_id + 9000000 AS new_ext FROM graph_store.gph_vid_map;
TRUNCATE graph_store.gph_vid_map;
INSERT INTO graph_store.gph_vid_map (ext_id, vid) SELECT new_ext, vid FROM remap;

SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS shim_digest2 FROM (
    SELECT m.ext_id AS v,
           m.ext_id || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                        FROM graph_store.gph_neighbors_ext(m.ext_id) d), '') AS line
    FROM graph_store.gph_vid_map m
) s \gset
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS cached_digest2 FROM (
    SELECT m.ext_id AS v,
           m.ext_id || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                        FROM graph_store.gph_neighbors_ext_cached(m.ext_id) d), '') AS line
    FROM graph_store.gph_vid_map m
) s \gset

SELECT set_config('vc.shim',    :'shim_digest',    false),
       set_config('vc.cached',  :'cached_digest',  false),
       set_config('vc.shim2',   :'shim_digest2',   false),
       set_config('vc.cached2', :'cached_digest2', false);

DO $$
DECLARE
    shim    text := current_setting('vc.shim');
    cached  text := current_setting('vc.cached');
    shim2   text := current_setting('vc.shim2');
    cached2 text := current_setting('vc.cached2');
BEGIN
    IF cached <> shim THEN
        RAISE EXCEPTION 'CACHED != UNCACHED (plan 034): shim % vs cached % — reverse cache diverges from the shim',
            shim, cached;
    END IF;
    RAISE NOTICE 'PASS 1: cached translation byte-identical to gph_neighbors_ext: %', cached;

    IF cached2 = cached THEN
        RAISE EXCEPTION 'INVALIDATION TEST INERT (plan 034): cached digest unchanged after remap (%) — the reload did not shift ids, cannot prove the hook fires',
            cached2;
    END IF;
    IF cached2 <> shim2 THEN
        RAISE EXCEPTION 'STALE CACHE (plan 034 STOP): after gph_vid_map reload, shim % vs cached % — invalidation hook did NOT fire',
            shim2, cached2;
    END IF;
    RAISE NOTICE 'PASS 2: relcache-invalidation hook flushed the cache on gph_vid_map reload; fresh digest %', cached2;
END $$;

\echo === graph vid cache (plan 034): cached==uncached + invalidation hook ALL PASS ===

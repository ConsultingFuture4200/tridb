-- graph_vid_cache_test.sql — ASSERTING test for the backend-local reverse vid cache
-- (advisor plan 034 / DEV-1345, PERF-03) and its identity_mode fast-path (plan 047). Oracles:
--   (1) PARITY   — gph_neighbors_ext_cached is byte-identical to the gph_neighbors_ext shim
--                  over every mapped source vertex (sorted neighbor multiset digest).
--   (2) HOOK     — the relcache-invalidation callback flushes the cache on a gph_vid_map
--                  reload: TRUNCATE fires a relcache invalidation, so the next cached probe
--                  rebuilds from the fresh map and surfaces the NEW external ids. If the hook
--                  did NOT fire, the cached probe would keep serving the OLD ids and diverge
--                  from the shim — a silently-stale id cache (plan 034 STOP condition).
--   (3) IDENTITY — identity_mode ON with an EMPTY gph_vid_map: gph_neighbors_ext_cached must
--                  match gph_neighbors_ext (both take the identity fast-path), not silently
--                  return empty (plan 047's bug: the C twin used to always SPI-probe the map).
--   (4) IDENTITY — identity_mode ON with the map POPULATED with identity rows (ext_id == vid):
--                  same equality, and the result must be independent of the map's contents.
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

-- (3)+(4) IDENTITY-MODE PARITY (advisor plan 047): gph_neighbors_ext_cached must honor
-- gph_am_meta.identity_mode exactly like the gph_neighbors_ext SQL shim (plan 033's CASE
-- guards). Pre-047 the C twin ALWAYS SPI-probed gph_vid_map regardless of identity_mode, so
-- under identity ON with an empty/incomplete map it silently returned EMPTY where the shim
-- returned full adjacency (the bug plan 047 fixes). A SEPARATE fresh database/extension so
-- native vids start dense at 0 with an EMPTY gph_vid_map.
CREATE DATABASE vidcache_identity;
\connect vidcache_identity
CREATE EXTENSION graph_store_am;

-- 501 native vertices (vid 0..500, dense from a fresh install). Hub (vid 0) fans out to every
-- vertex in [1,300]; a pseudo-random tail wires up the rest -- same non-trivial shape as the
-- sparse-map test above, addressed by native vid directly (no gph_vid_map involved yet).
CREATE TEMP TABLE ident_vids (vid bigint);
INSERT INTO ident_vids SELECT graph_store.gph_insert_vertex() FROM generate_series(1, 501);

SELECT count(*) FROM (
    SELECT graph_store.gph_insert_edge(0, g) FROM generate_series(1, 300) g
) s;
SELECT count(*) FROM (
    SELECT graph_store.gph_insert_edge(1 + (i % 500),
                                       1 + ((i::bigint * 2654435761 + 40503) % 500))
    FROM generate_series(1, 400) i
) s;

-- gph_vid_map is EMPTY here -- the exact scenario the bug lived in. Allowed by the
-- gph_set_identity_mode guard (DEV-1352) because an empty map has no ext_id <> vid row.
SELECT graph_store.gph_set_identity_mode(true);

SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS ident_shim_digest FROM (
    SELECT vid AS v,
           vid || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors_ext(vid) d), '') AS line
    FROM ident_vids
) s \gset
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS ident_cached_digest FROM (
    SELECT vid AS v,
           vid || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors_ext_cached(vid) d), '') AS line
    FROM ident_vids
) s \gset

-- (4) Same topology, but NOW populate gph_vid_map with identity rows (ext_id = vid) for every
-- vertex -- plan 047 test 3: identity ON with map rows PRESENT must still equal, proving the
-- cached path truly ignores the map under identity_mode rather than happening to agree only
-- because the map was empty.
INSERT INTO graph_store.gph_vid_map (ext_id, vid) SELECT vid, vid FROM ident_vids;

SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS ident_shim_digest2 FROM (
    SELECT vid AS v,
           vid || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors_ext(vid) d), '') AS line
    FROM ident_vids
) s \gset
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS ident_cached_digest2 FROM (
    SELECT vid AS v,
           vid || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors_ext_cached(vid) d), '') AS line
    FROM ident_vids
) s \gset

SELECT set_config('vc.ident_shim',    :'ident_shim_digest',    false),
       set_config('vc.ident_cached',  :'ident_cached_digest',  false),
       set_config('vc.ident_shim2',   :'ident_shim_digest2',   false),
       set_config('vc.ident_cached2', :'ident_cached_digest2', false);

DO $$
DECLARE
    shim    text := current_setting('vc.ident_shim');
    cached  text := current_setting('vc.ident_cached');
    shim2   text := current_setting('vc.ident_shim2');
    cached2 text := current_setting('vc.ident_cached2');
BEGIN
    IF cached <> shim THEN
        RAISE EXCEPTION 'CACHED != UNCACHED under identity_mode ON + EMPTY map (plan 047): shim % vs cached % -- gph_neighbors_ext_cached does not honor identity_mode',
            shim, cached;
    END IF;
    RAISE NOTICE 'PASS 3: identity ON + empty map -- cached matches shim: %', cached;

    IF cached2 <> shim2 THEN
        RAISE EXCEPTION 'CACHED != UNCACHED under identity_mode ON + POPULATED identity map (plan 047): shim % vs cached %',
            shim2, cached2;
    END IF;
    RAISE NOTICE 'PASS 4: identity ON + populated identity map -- cached matches shim: %', cached2;

    IF cached2 <> cached THEN
        RAISE EXCEPTION 'IDENTITY RESULT CHANGED after populating gph_vid_map (plan 047): empty-map digest % vs populated-map digest % -- the cached path is not ignoring the map under identity_mode',
            cached, cached2;
    END IF;
    RAISE NOTICE 'PASS 5: identity-mode result independent of gph_vid_map contents: %', cached2;
END $$;

\echo === graph vid cache (plan 034) + identity_mode parity (plan 047): ALL PASS ===

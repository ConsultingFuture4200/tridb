-- graph_dense_open_test.sql — ASSERTING parity for the dense O(1) locate on traversal Open
-- (advisor plan 048, ADR-0013 rider 1: "O(1) arithmetic vid addressing" landed for the OPEN path,
-- not just the batched-edge INSERT path gph_insert_edges already had).
--
-- gs_open now has two locate strategies gated by the session GUC graph_store.assume_dense_open
-- (default off): OFF takes the pre-048 linear gph_locate_vertex chain walk; ON tries the O(1)
-- gph_locate_vertex_dense fast path first. This test proves the two strategies are
-- OBSERVATIONALLY IDENTICAL over the SAME dense-in-order store:
--   (1) every vertex's out-neighbor multiset digests the same with the GUC off vs on;
--   (2) a never-assigned vid and a tombstoned vid are both "absent" (empty result) under both
--       settings — the dense path's benign-miss case matches the linear path's, not a hard ERROR.
--
-- Run: bash scripts/graph_test.sh tridb/msvbase:dev test/graph_dense_open_test.sql
-- (graph_test.sh installs graph_store_am; this file runs as superuser against db postgres.)

\set ON_ERROR_STOP on

CREATE DATABASE dense_open_test;
\connect dense_open_test
CREATE EXTENSION graph_store_am;

-- Dense-in-order native load (the plan-048 precondition): all vertices via gph_insert_vertex
-- BEFORE any edge, so vertex pages are contiguous and gph_locate_vertex_dense's arithmetic
-- computation is valid for every vid in [0, 2000). Hub (vid 0, degree 1500, >= 1 chained
-- adjacency page) + a pseudo-random tail. Pure integer arithmetic, no setseed.
SELECT count(*) FROM (SELECT graph_store.gph_insert_vertex() FROM generate_series(1, 2000) s) t;
SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(0, 1 + g) FROM generate_series(1, 1500) g) s;
SELECT count(*) FROM (
    SELECT graph_store.gph_insert_edge((i::bigint * 2654435761) % 2000,
                                       (i::bigint * 40503 + 12345) % 2000)
    FROM generate_series(1, 1000) i
) s;

-- Tombstone one vertex (plan 037) so the dense path's "visible tombstone => benign miss" case is
-- exercised, not just "vid never assigned".
SELECT graph_store.gph_tombstone_vertex(5);

-- (1) PARITY: linear (GUC off, the default) vs dense (GUC on), digested over every assigned vid
-- PLUS one never-assigned vid (2000, one past gm_next_vid) so the out-of-range benign-miss case
-- is covered too.
SET graph_store.assume_dense_open = off;
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS linear_digest FROM (
    SELECT p.v,
           p.v || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors(p.v) d), '') AS line
    FROM generate_series(0, 2000) p(v)
) s
\gset

SET graph_store.assume_dense_open = on;
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS dense_digest FROM (
    SELECT p.v,
           p.v || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors(p.v) d), '') AS line
    FROM generate_series(0, 2000) p(v)
) s
\gset
RESET graph_store.assume_dense_open;

SELECT set_config('dot.linear', :'linear_digest', false),
       set_config('dot.dense',  :'dense_digest',  false);

DO $$
DECLARE
    linear text := current_setting('dot.linear');
    dense  text := current_setting('dot.dense');
BEGIN
    IF dense <> linear THEN
        RAISE EXCEPTION 'DENSE OPEN != LINEAR OPEN (plan 048 STOP): linear % vs dense % — dense locate on gs_open diverges from the linear path',
            linear, dense;
    END IF;
    RAISE NOTICE 'PASS: dense-open (graph_store.assume_dense_open=on) byte-identical to linear open over every vid incl. tombstone + out-of-range: %', dense;
END $$;

\echo === graph dense-open parity (plan 048): linear==dense incl. tombstone/out-of-range ALL PASS ===

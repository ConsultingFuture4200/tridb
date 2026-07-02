-- graph_v0v1_bench.sql — measured v0-vs-v1 graph-store microbench (advisor plan 016, spike).
--
-- Loads the SAME deterministic synthetic graph (50k vertices / 500k directed edges, one
-- degree-5000 hub + a pseudo-random tail) into ONE store and measures: bulk-load wall clock,
-- neighbors() latency over 100 fixed vertices (hub + tail mix), and page reads. Which store is
-- exercised is selected by the psql variable :STORE ('v0' | 'v1'); the two stores collide on the
-- graph_store schema (both extensions are relocatable=false, schema=graph_store), so the driver
-- (scripts/graph_v0v1_bench.sh) runs this file TWICE, once per store in its own database.
--
-- Metric lines are emitted with a stable "METRIC <store> <name> <value>" prefix the driver greps.
-- The v1 ingest number is EXPECTED to look bad until O(1) vid addressing lands (rider 1,
-- graph_am.c:198 gph_locate_vertex walks the vertex-page chain per lookup) — that number IS the
-- evidence for the rider; it is recorded here, NOT fixed.

\set ON_ERROR_STOP on

-- Identity id mapping (spike simplification, documented in docs/graph_rewire_design_v0.1.0.md):
-- the synthetic external ids are exactly [0, 50000), and v1 assigns dense vids 0..49999 in
-- insertion order, so external_id == vid. The real migration must supply an ext_id->vid mapping
-- layer (add_edge takes arbitrary bigint ids; gph_insert_edge takes dense vids) — the spike loads
-- an id space chosen so the identity map holds, precisely to isolate the storage cost from the
-- unresolved id-strategy question.

SELECT :'STORE' = 'v0' AS is_v0 \gset

\if :is_v0
    CREATE EXTENSION graph_store;
\else
    CREATE EXTENSION graph_store_am;
    SET search_path TO graph_store, public;
\endif

-- ---------------------------------------------------------------------------
-- Bulk load (wall clock)
-- ---------------------------------------------------------------------------
SELECT extract(epoch FROM clock_timestamp()) AS load_t0 \gset

\if :is_v0
    -- v0: add_edge(src, dst) upserts into the heap adjacency array. Hub then tail.
    SELECT graph_store.add_edge(0, g) FROM generate_series(1, 5000) g;
    SELECT graph_store.add_edge((i::bigint * 2654435761) % 50000,
                                (i::bigint * 40503 + 12345) % 50000)
    FROM generate_series(1, 495000) i;
\else
    -- v1: vertices first (dense vids 0..49999), then edges over the identity map.
    SELECT gph_insert_vertex() FROM generate_series(1, 50000);
    SELECT gph_insert_edge(0, g) FROM generate_series(1, 5000) g;
    SELECT gph_insert_edge((i::bigint * 2654435761) % 50000,
                           (i::bigint * 40503 + 12345) % 50000)
    FROM generate_series(1, 495000) i;
\endif

SELECT extract(epoch FROM clock_timestamp()) AS load_t1 \gset
\echo METRIC :STORE load_ms
SELECT round((:load_t1 - :load_t0) * 1000)::text AS v \gset
\echo :v

-- The 100 probe vertices: the hub (0) + 99 fixed pseudo-random tails in [1, 50000).
CREATE TEMP TABLE probe AS
    SELECT 0::bigint AS v
    UNION ALL
    SELECT (i::bigint * 2654435761) % 50000 FROM generate_series(1, 99) i;

-- ---------------------------------------------------------------------------
-- neighbors() latency over the 100 probes (full adjacency read each)
-- ---------------------------------------------------------------------------
\if :is_v0
    SELECT extract(epoch FROM clock_timestamp()) AS neigh_t0 \gset
    SELECT sum(c) FROM (
        SELECT (SELECT count(*) FROM graph_store.neighbors(p.v)) AS c FROM probe p
    ) s;
    SELECT extract(epoch FROM clock_timestamp()) AS neigh_t1 \gset
\else
    SELECT gph_page_reads() AS pr0 \gset
    SELECT extract(epoch FROM clock_timestamp()) AS neigh_t0 \gset
    SELECT sum(c) FROM (
        SELECT (SELECT count(*) FROM gph_neighbors(p.v)) AS c FROM probe p
    ) s;
    SELECT extract(epoch FROM clock_timestamp()) AS neigh_t1 \gset
    SELECT gph_page_reads() AS pr1 \gset
\endif

\echo METRIC :STORE neighbors_ms_per_100
SELECT round((:neigh_t1 - :neigh_t0) * 1000)::text AS v \gset
\echo :v

-- ---------------------------------------------------------------------------
-- Page reads over the same 100 probes
--   v1: gph_page_reads() delta (adjacency pages read).
--   v0: pg_statio_user_tables heap blks (read+hit) delta on graph_store.adjacency — best effort,
--       subject to stats-collector timing; reported as a heap-block count, not a like-for-like
--       native-page count (the two stores page differently — this is a rough comparator, called
--       out in the design doc).
-- ---------------------------------------------------------------------------
\if :is_v0
    \echo METRIC v0 heap_blks_note
    \echo see_pg_statio_below
    SELECT coalesce(heap_blks_read + heap_blks_hit, 0)::text AS v
    FROM pg_statio_user_tables WHERE relname = 'adjacency' \gset
    \echo METRIC v0 adjacency_blks_touched_cumulative
    \echo :v
\else
    \echo METRIC v1 adjacency_page_reads_per_100
    SELECT (:pr1 - :pr0)::text AS v \gset
    \echo :v
\endif

\echo METRIC :STORE done 1

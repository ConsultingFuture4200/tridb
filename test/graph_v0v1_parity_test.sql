-- graph_v0v1_parity_test.sql — ASSERTING v0-vs-v1 parity oracle (advisor plan 025, ADR-0013).
--
-- Loads the SAME deterministic edge set into BOTH graph stores — the v0 heap-backed extension
-- (graph_store, src/graph_store_ext/) and the v1 native access method (graph_store_am,
-- src/graph_store/) — and ASSERTS equal sorted neighbor sets for EVERY probe vertex plus equal
-- edge counts. The two extensions collide on the graph_store schema (both relocatable=false),
-- so each store loads in its own database; psql variables carry the digests across \connect.
--
-- Edge-set shape (reuses scripts/graph_v0v1_bench.sh's): one hub (vertex 0) with degree 3300 —
-- >= 3 chained 32KB adjacency pages at 1022 GphEdgeSlots/page — plus a 4000-edge pseudo-random
-- tail over 5000 vertices. Deterministic (pure integer arithmetic, no setseed).
--
-- Section 2 (identity map): dense external ids [0,5000) so v1's native dense-vid surface loads
-- the set directly (gph_insert_vertex x N, then gph_insert_edge) — isolates the STORAGE parity.
-- Section 3 (ext-id map): SPARSE external ids (v*7+1000000) through the Stage-A compat surface
-- (graph_store.add_edge -> gph_upsert_vertex -> gph_insert_edge) — proves the id-mapping layer
-- returns byte-identical neighbor sets. Runs only once the compat surface exists (Stage A);
-- before Stage A it is skipped with a NOTICE.
--
-- Run: bash scripts/graph_test.sh tridb/msvbase:dev test/graph_v0v1_parity_test.sql
-- (graph_test.sh installs BOTH extensions; this file runs as superuser against db postgres.)

\set ON_ERROR_STOP on

CREATE DATABASE parity_v0;
CREATE DATABASE parity_v1;

-- ===========================================================================
-- Section 1: v0 load + digest (dense ids 0..4999)
-- ===========================================================================
\connect parity_v0
CREATE EXTENSION graph_store;

-- hub: degree 3300 (>= 3 chained v1 adjacency pages), dst in [1000, 4300]
SELECT count(*) FROM (SELECT graph_store.add_edge(0, 1000 + g) FROM generate_series(1, 3300) g) s;
-- pseudo-random tail: 4000 edges over [0, 5000)
SELECT count(*) FROM (
    SELECT graph_store.add_edge((i::bigint * 2654435761) % 5000,
                                (i::bigint * 40503 + 12345) % 5000)
    FROM generate_series(1, 4000) i
) s;

-- digest of the sorted neighbor multiset of EVERY probe vertex, via the public surface
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS v0_digest FROM (
    SELECT p.v,
           p.v || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.neighbors(p.v) d), '') AS line
    FROM generate_series(0, 4999) p(v)
) s
\gset
SELECT count(*) AS v0_edges
FROM generate_series(0, 4999) p(v), graph_store.neighbors(p.v) d
\gset

-- ===========================================================================
-- Section 2: v1 native load + digest (identity map: dense vids 0..4999)
-- ===========================================================================
\connect parity_v1
CREATE EXTENSION graph_store_am;

SELECT count(*) FROM (SELECT graph_store.gph_insert_vertex() FROM generate_series(1, 5000) s) t;
SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(0, 1000 + g) FROM generate_series(1, 3300) g) s;
SELECT count(*) FROM (
    SELECT graph_store.gph_insert_edge((i::bigint * 2654435761) % 5000,
                                       (i::bigint * 40503 + 12345) % 5000)
    FROM generate_series(1, 4000) i
) s;

SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS v1_digest FROM (
    SELECT p.v,
           p.v || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.gph_neighbors(p.v) d), '') AS line
    FROM generate_series(0, 4999) p(v)
) s
\gset
SELECT count(*) AS v1_probe_edges
FROM generate_series(0, 4999) p(v), graph_store.gph_neighbors(p.v) d
\gset
SELECT graph_store.gph_edge_count() AS v1_edge_count \gset

-- Stage-A compat surface present? (add_edge ported into the v1 extension)
SELECT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'graph_store' AND p.proname = 'add_edge'
) AS has_compat \gset

-- ===========================================================================
-- Section 3 (Stage A+): ext-id mapping layer parity under SPARSE external ids.
-- Same edge shape, external id space ext(v) = v*7 + 1000000 — NOT dense, NOT
-- vid-aligned — loaded through the compat front door in BOTH stores.
-- ===========================================================================
\if :has_compat
\connect postgres
CREATE DATABASE parity_v0c;
CREATE DATABASE parity_v1c;

\connect parity_v0c
CREATE EXTENSION graph_store;
SELECT count(*) FROM (SELECT graph_store.add_edge(1000000, (1000 + g)::bigint * 7 + 1000000)
                      FROM generate_series(1, 3300) g) s;
SELECT count(*) FROM (
    SELECT graph_store.add_edge(((i::bigint * 2654435761) % 5000) * 7 + 1000000,
                                ((i::bigint * 40503 + 12345) % 5000) * 7 + 1000000)
    FROM generate_series(1, 4000) i
) s;
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS v0c_digest FROM (
    SELECT p.v,
           p.v || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.neighbors(p.v) d), '') AS line
    FROM (SELECT g::bigint * 7 + 1000000 FROM generate_series(0, 4999) g) p(v)
) s
\gset

\connect parity_v1c
CREATE EXTENSION graph_store_am;
SELECT count(*) FROM (SELECT graph_store.add_edge(1000000, (1000 + g)::bigint * 7 + 1000000)
                      FROM generate_series(1, 3300) g) s;
SELECT count(*) FROM (
    SELECT graph_store.add_edge(((i::bigint * 2654435761) % 5000) * 7 + 1000000,
                                ((i::bigint * 40503 + 12345) % 5000) * 7 + 1000000)
    FROM generate_series(1, 4000) i
) s;
-- probe through the SAME compat surface text as v0 (graph_store.neighbors)
SELECT md5(string_agg(line, E'\n' ORDER BY v)) AS v1c_digest FROM (
    SELECT p.v,
           p.v || ':' || coalesce((SELECT string_agg(d::text, ',' ORDER BY d)
                                   FROM graph_store.neighbors(p.v) d), '') AS line
    FROM (SELECT g::bigint * 7 + 1000000 FROM generate_series(0, 4999) g) p(v)
) s
\gset
SELECT graph_store.gph_edge_count() AS v1c_edge_count \gset
\else
SELECT 'skipped' AS v0c_digest \gset
SELECT 'skipped' AS v1c_digest \gset
SELECT -1 AS v1c_edge_count \gset
\endif

-- ===========================================================================
-- Assertions (in db postgres; psql vars survive \connect). psql does NOT
-- interpolate variables inside dollar-quoted bodies, so carry them into the
-- DO block via session GUCs (set_config/current_setting).
-- ===========================================================================
\connect postgres

SELECT set_config('parity.v0_digest',     :'v0_digest',      false),
       set_config('parity.v1_digest',     :'v1_digest',      false),
       set_config('parity.v0_edges',      :'v0_edges',       false),
       set_config('parity.v1_probe',      :'v1_probe_edges', false),
       set_config('parity.v1_count',      :'v1_edge_count',  false),
       set_config('parity.has_compat',    :'has_compat',     false),
       set_config('parity.v0c_digest',    :'v0c_digest',     false),
       set_config('parity.v1c_digest',    :'v1c_digest',     false),
       set_config('parity.v1c_count',     :'v1c_edge_count', false);

DO $$
DECLARE
    v0_digest     text   := current_setting('parity.v0_digest');
    v1_digest     text   := current_setting('parity.v1_digest');
    v0_edges      bigint := current_setting('parity.v0_edges')::bigint;
    v1_probe      bigint := current_setting('parity.v1_probe')::bigint;
    v1_count      bigint := current_setting('parity.v1_count')::bigint;
    has_compat    bool   := current_setting('parity.has_compat')::bool;
    v0c_digest    text   := current_setting('parity.v0c_digest');
    v1c_digest    text   := current_setting('parity.v1c_digest');
    v1c_count     bigint := current_setting('parity.v1c_count')::bigint;
BEGIN
    -- expected totals: 3300 hub + 4000 tail
    IF v0_edges <> 7300 THEN
        RAISE EXCEPTION 'v0 traversal edge total: expected 7300, got %', v0_edges;
    END IF;
    IF v1_probe <> 7300 THEN
        RAISE EXCEPTION 'v1 traversal edge total: expected 7300, got %', v1_probe;
    END IF;
    IF v1_count <> 7300 THEN
        RAISE EXCEPTION 'v1 gph_edge_count(): expected 7300, got %', v1_count;
    END IF;
    RAISE NOTICE 'PASS 1: edge counts equal (v0 traversal % = v1 traversal % = gph_edge_count %)',
        v0_edges, v1_probe, v1_count;

    -- neighbor-set parity: sorted neighbor multiset of every probe vertex, digested
    IF v0_digest <> v1_digest THEN
        RAISE EXCEPTION 'NEIGHBOR-SET DIVERGENCE (dense/native load): v0 % vs v1 % — ADR-0013 STOP condition',
            v0_digest, v1_digest;
    END IF;
    RAISE NOTICE 'PASS 2: neighbor-set digests equal over 5000 probe vertices (dense ids): %', v0_digest;

    IF has_compat THEN
        IF v1c_count <> 7300 THEN
            RAISE EXCEPTION 'v1 compat-loaded gph_edge_count(): expected 7300, got %', v1c_count;
        END IF;
        IF v0c_digest <> v1c_digest THEN
            RAISE EXCEPTION 'NEIGHBOR-SET DIVERGENCE (ext-id map, sparse ids): v0 % vs v1 % — ADR-0013 STOP condition',
                v0c_digest, v1c_digest;
        END IF;
        RAISE NOTICE 'PASS 3: ext-id mapping layer parity over sparse external ids: %', v1c_digest;
    ELSE
        RAISE NOTICE 'SKIP 3: Stage-A compat surface (add_edge in graph_store_am) not present yet — sections 1+2 only';
    END IF;
END $$;

\echo === graph v0/v1 parity oracle (plan 025): ALL PASS ===

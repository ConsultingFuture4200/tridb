-- hnsw_stale_index.sql — repro for the process-global HNSW in-RAM index-map
-- cache-invalidation gap (advisor plan 023; relates to DEV-1259 Phase C, UPCORE-02).
--
-- MECHANISM UNDER TEST
--   MSVBASE caches each HNSW index's in-RAM graph in a process-global std::map
--   (src/hnswindex_scan.cpp:27-28, `vector_index_map`), populated once per backend
--   on a LoadIndex cache-MISS (guard at ~line 113: `if (find(p_path) != end) return;`)
--   and keyed on DataDir/DatabasePath/RelationGetRelationName(index)
--   (src/hnswindex.cpp:208-210). No entry is EVER erased. So within one long-lived
--   (pooled) backend, DROP+CREATE (same name) / REINDEX / recreate-at-new-dimension
--   all leave the FIRST graph cached and served on the next scan.
--
-- WHY ONE SESSION: the map is a static class member (process-global PER BACKEND).
--   Every scenario below MUST run on the SAME connection/backend, or a fresh backend
--   would cache-miss and (correctly) rebuild, hiding the bug. The driver feeds this
--   whole file to a SINGLE psql invocation for exactly that reason.
--
-- HOW TO READ THE OUTPUT: each scenario prints the id the scan RETURNED next to the
--   id a FRESH (correctly-invalidated) index would return. RETURNED != FRESH ==> the
--   stale cached graph was served (the bug). Scenario D is expected to return garbage
--   or crash the backend (dim-4 cached graph probed with a dim-8 query = the OOB read
--   from plan 019 / UPCORE-01).

\set ON_ERROR_STOP off
SET enable_seqscan = off;   -- force the HNSW index scan path (populate/serve the cache)

CREATE EXTENSION IF NOT EXISTS vectordb;
DROP TABLE IF EXISTS t;
CREATE TABLE t(id int, embedding float8[4]);

-- ============================================================================
-- Scenario A: first build populates the process-global cache for name `t_hnsw`.
--   Data: id in 1..10, embedding = [id,0,0,0]. Query [1,0,0,0] -> nearest is id=1.
-- ============================================================================
\echo '=== Scenario A: initial build populates the cache (expect returned=1) ==='
INSERT INTO t SELECT k, ARRAY[k::float8,0,0,0] FROM generate_series(1,10) k;
CREATE INDEX t_hnsw ON t USING hnsw(embedding) WITH (dimension=4, distmethod=l2_distance);
SELECT 'A' AS scenario, id AS returned_id, 1 AS fresh_id
FROM t ORDER BY embedding <-> ARRAY[1.0,0,0,0] LIMIT 1;

-- ============================================================================
-- Scenario B: DROP + CREATE the SAME index name with DIFFERENT data.
--   New data makes id=42 the exact match for the query [1,0,0,0]; the OLD graph
--   had no id=42. A correctly-invalidated index returns 42; the stale cached
--   graph (still holding the scenario-A points/labels) returns something else.
-- ============================================================================
\echo '=== Scenario B: DROP+CREATE same name, different data (fresh=42; !=42 => STALE) ==='
DROP INDEX t_hnsw;
DELETE FROM t;
INSERT INTO t VALUES (42, ARRAY[1.0,0,0,0]);              -- exact match to the query
INSERT INTO t SELECT k, ARRAY[(k+50)::float8,0,0,0] FROM generate_series(1,10) k; -- all far
CREATE INDEX t_hnsw ON t USING hnsw(embedding) WITH (dimension=4, distmethod=l2_distance);
SELECT 'B' AS scenario, id AS returned_id, 42 AS fresh_id
FROM t ORDER BY embedding <-> ARRAY[1.0,0,0,0] LIMIT 1;

-- ============================================================================
-- Scenario C: REINDEX after the heap changed.
--   Move the exact match onto id=99, REINDEX (rebuilds the flat file but the
--   in-RAM cached graph is keyed on name and never re-read). Fresh=99.
-- ============================================================================
\echo '=== Scenario C: REINDEX after data change (fresh=99; !=99 => STALE) ==='
DELETE FROM t;
INSERT INTO t VALUES (99, ARRAY[1.0,0,0,0]);
INSERT INTO t SELECT k, ARRAY[(k+50)::float8,0,0,0] FROM generate_series(1,10) k;
REINDEX INDEX t_hnsw;
SELECT 'C' AS scenario, id AS returned_id, 99 AS fresh_id
FROM t ORDER BY embedding <-> ARRAY[1.0,0,0,0] LIMIT 1;

-- ============================================================================
-- Scenario D: recreate the SAME index name at a DIFFERENT dimension.
--   The cached graph is still dim=4; the new index/query is dim=8. On a cache
--   HIT the dim-8 query vector is read against a dim-4 space => out-of-bounds
--   read (plan 019 / UPCORE-01) — garbage distance or a backend crash. If the
--   backend segfaults here, THAT is the demonstrated OOB (this is the last
--   scenario precisely because it may terminate the session).
--   float8[] is unsized in PG, so the same column holds 8-element arrays.
-- ============================================================================
\echo '=== Scenario D: recreate same name at dimension=8 (dim-4 cache HIT => OOB/crash) ==='
DROP INDEX t_hnsw;
DELETE FROM t;
INSERT INTO t VALUES (7, ARRAY[1.0,0,0,0,0,0,0,0]);
INSERT INTO t SELECT k, ARRAY[(k+50)::float8,0,0,0,0,0,0,0] FROM generate_series(1,10) k;
CREATE INDEX t_hnsw ON t USING hnsw(embedding) WITH (dimension=8, distmethod=l2_distance);
SELECT 'D' AS scenario, id AS returned_id, 7 AS fresh_id
FROM t ORDER BY embedding <-> ARRAY[1.0,0,0,0,0,0,0,0] LIMIT 1;

\echo '=== repro complete: compare returned_id vs fresh_id per scenario above ==='

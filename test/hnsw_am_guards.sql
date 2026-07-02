-- hnsw_am_guards.sql — advisor plan 019
--
-- Negative-path regression for the HNSW access-method entry guards. Before plan 019 the
-- scan / vacuum / range-distance entry points trusted caller-supplied arrays the insert/build
-- paths distrust, so an unprivileged SQL query could OOB-read the distance kernel or NULL-deref
-- VACUUM. These asserts prove the guards turn a crash/OOB into a clean ERROR (or a correct
-- empty/complete result). If any guard is missing, the backend crashes and psql loses its
-- connection under ON_ERROR_STOP=1 -> the harness fails loud.
--
-- Three regression classes:
--   (1) wrong-dimension query vector -> clean ERROR, not OOB read / crash
--   (2) LIMIT 0 vector scan          -> 0 rows, no teardown crash
--   (3) VACUUM of an HNSW-indexed table with dead tuples -> completes (BulkDelete NULL-stats + TID)
-- plus the array-content (convert_array_to_vector) and range-distance length guards.

CREATE EXTENSION vectordb;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

ANALYZE entities;

SET enable_seqscan = off;  -- force the planner onto the HNSW index for ORDER BY <-> scans

-- (1) Wrong-dimension ORDER BY query vector: 3-dim query against an 8-dim index. hnswlib would
-- read 8 floats from a 3-float buffer (OOB). The Step-1 guard must ERROR cleanly instead.
\echo '--- (1) ASSERT: wrong-dimension ORDER BY query errors cleanly (no OOB/crash) ---'
DO $$
DECLARE dummy bigint;
BEGIN
    BEGIN
        SELECT id INTO dummy FROM entities
            ORDER BY embedding <-> '{1,2,3}' LIMIT 5;
        RAISE EXCEPTION 'plan019 FAIL: wrong-dim query returned instead of erroring';
    EXCEPTION
        WHEN sqlstate '22000' THEN  -- ERRCODE_DATA_EXCEPTION (length mismatch guard)
            RAISE NOTICE 'plan019 OK: wrong-dim ORDER BY query errored cleanly';
    END;
END $$;

-- (2) LIMIT 0 vector scan: beginscan -> endscan with (possibly) no gettuple fetch. The endscan
-- NULL-workSpace guard (present since DEV-1236) must not crash. Assert exactly 0 rows.
\echo '--- (2) ASSERT: LIMIT 0 vector scan returns 0 rows, no crash ---'
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM (
        SELECT id FROM entities ORDER BY embedding <-> '{5,0,0,0,0,0,0,0}' LIMIT 0
    ) s;
    IF n <> 0 THEN
        RAISE EXCEPTION 'plan019 FAIL: LIMIT 0 scan returned % rows (expected 0)', n;
    END IF;
    RAISE NOTICE 'plan019 OK: LIMIT 0 vector scan returned 0 rows without crashing';
END $$;

-- Sanity: a correct 8-dim ANN scan still works (guards must not reject legitimate vectors).
\echo '--- (2b) ASSERT: correct-dimension ANN scan still returns rows ---'
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM (
        SELECT id FROM entities ORDER BY embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 5
    ) s;
    IF n <> 5 THEN
        RAISE EXCEPTION 'plan019 FAIL: correct ANN scan returned % rows (expected 5)', n;
    END IF;
    RAISE NOTICE 'plan019 OK: correct-dimension ANN scan returned 5 rows';
END $$;

-- (3) VACUUM with dead tuples: DELETE rows to create dead index entries, then VACUUM. This drives
-- ambulkdelete -> HNSWIndexScan::BulkDelete with stats==NULL on the first call. The Step-4 guard
-- palloc0s stats and builds a well-formed TID; without it VACUUM NULL-derefs / corrupts.
\echo '--- (3) ASSERT: VACUUM of the HNSW-indexed table completes ---'
DELETE FROM entities WHERE id % 3 = 0;
VACUUM entities;
DO $$
BEGIN
    RAISE NOTICE 'plan019 OK: VACUUM completed on the HNSW-indexed table';
END $$;

-- (4) Array-content guard (convert_array_to_vector): a NULL-containing float8[] must ERROR, not
-- be reinterpreted as packed data.
\echo '--- (4) ASSERT: NULL-containing vector array errors cleanly ---'
DO $$
DECLARE d float8;
BEGIN
    BEGIN
        SELECT l2_distance(ARRAY[1,2,3]::float8[], ARRAY[1,NULL,3]::float8[]) INTO d;
        RAISE EXCEPTION 'plan019 FAIL: NULL-containing array did not error';
    EXCEPTION
        WHEN sqlstate '22004' THEN  -- ERRCODE_NULL_VALUE_NOT_ALLOWED
            RAISE NOTICE 'plan019 OK: NULL-containing vector array errored cleanly';
    END;
END $$;

-- (5) Range-distance length guard: rhs packs threshold + vector, so lhs.size() must equal
-- rhs.size()-1. A mismatched length must ERROR instead of reading past rhs.
\echo '--- (5) ASSERT: mismatched range-distance array length errors cleanly ---'
DO $$
DECLARE b bool;
BEGIN
    BEGIN
        -- lhs dim 3; rhs = threshold + 4-dim vector -> rhs.size()-1 = 4 != 3
        SELECT range_l2_distance(ARRAY[1,2,3]::float8[], ARRAY[0.5,1,2,3,4]::float8[]) INTO b;
        RAISE EXCEPTION 'plan019 FAIL: mismatched range array length did not error';
    EXCEPTION
        WHEN sqlstate '22000' THEN  -- ERRCODE_DATA_EXCEPTION
            RAISE NOTICE 'plan019 OK: mismatched range-distance length errored cleanly';
    END;
    -- a well-formed range call (rhs.size()-1 == lhs.size()) still evaluates
    SELECT range_l2_distance(ARRAY[1,2,3]::float8[], ARRAY[100,1,2,3]::float8[]) INTO b;
    RAISE NOTICE 'plan019 OK: well-formed range-distance call evaluated (result=%)', b;
END $$;

\echo '--- plan019 hnsw_am_guards: all asserts passed ---'

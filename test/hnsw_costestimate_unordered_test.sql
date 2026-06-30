-- hnsw_costestimate_unordered_test.sql — DEV-1248
--
-- The HNSW index can ONLY serve an ORDER BY <-> distance scan. DEV-1236 made
-- hnsw_gettuple ereport(ERROR) when the planner chose the HNSW index for an
-- unordered/no-key scan (e.g. count(*) / SELECT without ORDER BY when
-- enable_seqscan is off), turning a wrong-answer-then-crash into a clean error.
--
-- DEV-1248 closes the loop in the PLANNER: hnsw_costestimate now charges
-- disable_cost when the index path carries no order-by-distance pathkey
-- (path->indexorderbys == NIL), so the planner NEVER chooses that path. This
-- test asserts both directions:
--   (1) an unordered scan (count(*)) with enable_seqscan=off does NOT use the
--       HNSW index — it falls back to a seqscan / other path, and returns the
--       CORRECT row count without erroring;
--   (2) an ordered ANN scan (ORDER BY embedding <-> q) STILL picks the HNSW
--       index — the penalty must not break the index's one real job.
--
-- Runs cleanly under -v ON_ERROR_STOP=1: with the cost penalty in place there is
-- no DEV-1236 runtime ERROR to trip over (the planner sidesteps the index).

CREATE EXTENSION vectordb;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

ANALYZE entities;

SET enable_seqscan = off;  -- pre-DEV-1248 this forced the planner onto the HNSW index

-- Capture EXPLAIN plans server-side so we can assert on the chosen access path.
-- explain_text(query) returns the whole plan as one text blob.
CREATE FUNCTION explain_text(q text) RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE r record; out text := '';
BEGIN
    FOR r IN EXECUTE 'EXPLAIN (COSTS OFF) ' || q LOOP
        out := out || r."QUERY PLAN" || E'\n';
    END LOOP;
    RETURN out;
END $fn$;

\echo '--- (1) Unordered count(*) plan (must NOT contain entities_hnsw) ---'
EXPLAIN (COSTS OFF) SELECT count(*) FROM entities;

\echo '--- (1b) ASSERT: unordered scan does not pick the HNSW index ---'
DO $$
DECLARE plan text;
BEGIN
    plan := explain_text('SELECT count(*) FROM entities');
    IF position('entities_hnsw' in plan) > 0 THEN
        RAISE EXCEPTION 'DEV-1248 FAIL: planner chose HNSW index for an unordered scan. Plan:%', E'\n' || plan;
    END IF;
    RAISE NOTICE 'DEV-1248 OK: unordered count(*) avoids the HNSW index';
END $$;

\echo '--- (1c) count(*) returns the CORRECT answer (2000) ---'
DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM entities;
    IF n <> 2000 THEN
        RAISE EXCEPTION 'DEV-1248 FAIL: count(*) returned % (expected 2000)', n;
    END IF;
    RAISE NOTICE 'DEV-1248 OK: count(*) = 2000';
END $$;

\echo '--- (2) Ordered ANN scan plan (MUST contain entities_hnsw) ---'
EXPLAIN (COSTS OFF) SELECT id FROM entities
    ORDER BY embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 5;

\echo '--- (2b) ASSERT: ordered ANN scan still picks the HNSW index ---'
DO $$
DECLARE plan text;
BEGIN
    plan := explain_text(
        'SELECT id FROM entities ORDER BY embedding <-> ''{19,0,0,0,0,0,0,0}'' LIMIT 5');
    IF position('entities_hnsw' in plan) = 0 THEN
        RAISE EXCEPTION 'DEV-1248 FAIL: planner did NOT use HNSW index for an ordered ANN scan. Plan:%', E'\n' || plan;
    END IF;
    RAISE NOTICE 'DEV-1248 OK: ordered ANN scan uses the HNSW index';
END $$;

\echo '--- (2c) ANN scan returns rows ---'
SELECT id FROM entities ORDER BY embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 5;

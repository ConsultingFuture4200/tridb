-- hnsw_abort_stress_test.sql — Oracle B (abort-stress) for DEV-1235 Defect B.
--
-- DEV-1235 Defect B was reported as an HNSW crash under repeated ABORTED incremental
-- inserts: an aborted aminsert leaving the in-memory HNSW graph in a torn state, so a
-- later scan (or the next insert) segfaults the backend. This regression exercises that
-- path at volume and asserts the backend stays up and answers a query correctly.
--
-- Driven by scripts/hnsw_abort_stress_test.sh (sets up a live cluster). The patterns:
--   P1 single-statement abort  : INSERT ... that fails mid-statement (constraint/cast),
--                                 so the HNSW aminsert is rolled back inside one stmt.
--   P2 single-session abort     : BEGIN; INSERT (valid HNSW aminsert runs); ROLLBACK;
--                                 repeated in one long-lived backend (no reconnect).
--   P3 abort + query interleave : abort a batch, then run an HNSW <-> scan, repeat.
--
-- After hundreds of cumulative aborted inserts, a final HNSW scan must return the
-- committed nearest neighbour and the backend must still be alive.

-- Assumes table s + HNSW index s_hnsw + committed anchor 7777 already exist
-- (the driving harness scripts/hnsw_abort_stress_test.sh seeds them and runs P1 first).
\set ON_ERROR_STOP 0
\echo '--- P2: single-session BEGIN/INSERT/ROLLBACK x N (one backend, no reconnect) ---'
-- Each aborted txn runs a real HNSW aminsert then rolls it back. If the abort path
-- corrupts the in-memory graph, a subsequent insert or scan crashes the backend.
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..120 LOOP
    BEGIN
      INSERT INTO s VALUES (900000 + i, ARRAY[(900000 + i)::float8, 1, 2, 3]);
      RAISE EXCEPTION 'forced abort %', i;  -- rolls back the aminsert just performed
    EXCEPTION WHEN OTHERS THEN
      NULL;  -- swallow; loop continues in the SAME backend
    END;
  END LOOP;
END $$;

\echo '--- P3: abort-then-query interleave x N (same session) ---'
DO $$
DECLARE i int; cnt int;
BEGIN
  FOR i IN 1..120 LOOP
    BEGIN
      INSERT INTO s VALUES (800000 + i, ARRAY[(800000 + i)::float8, 4, 5, 6]);
      RAISE EXCEPTION 'forced abort %', i;
    EXCEPTION WHEN OTHERS THEN
      NULL;
    END;
    -- interleave a real HNSW scan against the (rolled-back) graph state
    SET enable_seqscan = off;
    SELECT count(*) INTO cnt FROM (
      SELECT id FROM s ORDER BY embedding <-> ARRAY[7777.0, 0, 0, 0] LIMIT 5
    ) q;
  END LOOP;
END $$;

\echo '--- final assert: backend alive + HNSW scan returns committed anchor 7777 ---'
\set ON_ERROR_STOP 1
SET enable_seqscan = off;
SELECT id AS nearest
FROM s
ORDER BY embedding <-> ARRAY[7777.0, 0, 0, 0]::float8[]
LIMIT 1;
-- Expected: 7777

-- Row count via seqscan (HNSW cannot serve an unordered count; re-enable seqscan).
SET enable_seqscan = on;
SELECT count(*) AS committed_rows FROM s;
-- Expected: 51 (50 seed + anchor; all aborted inserts rolled back)

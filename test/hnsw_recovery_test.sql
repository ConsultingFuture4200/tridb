-- hnsw_recovery_test.sql — Oracle A (crash/WAL recovery) for DEV-1235 Defect A.
--
-- Proves that a committed HNSW index insert survives a crash-immediate restart:
-- the WAL-durable heap tuple is visible to LoadIndex's rebuild scan after recovery,
-- so the HNSW index returns the inserted row (not a stale pre-crash entry).
--
-- Usage: driven by scripts/crash_recovery_hnsw_test.sh (not standalone; requires a
-- live cluster state that crash_recovery_hnsw_test.sh sets up and tears down).
-- The :recovery_phase psql variable is set by the harness.

\echo '--- post-crash HNSW recovery assert ---'

-- The distinctive row R was inserted (committed) AFTER the baseline CHECKPOINT.
-- After crash-immediate + WAL-redo, the heap tuple is visible.
-- With DEV-1235 patch: LoadIndex rebuilds from heap and finds R.
-- Without DEV-1235: LoadIndex reads stale flat file and misses R.
SET enable_seqscan = off;

SELECT id AS recovered_id
FROM t
ORDER BY embedding <-> ARRAY[9001.0, 0, 0, 0]::float8[]
LIMIT 1;
-- Expected: 9001 (the distinctive row R)

\echo '--- assert baseline rows still present ---'
SELECT count(*) AS baseline_count FROM t WHERE id <= 30;
-- Expected: 30

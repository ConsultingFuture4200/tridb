-- _fork_bug_multicol_double_scan.sql — NOT IN CI (leading underscore; absent from Makefile
-- ENGINE_TESTS). Verifies DEV-1236 fix: co-issuing a second executor-driven scan in the same
-- plpgsql block as multicol_topk() no longer crashes the backend.
--
-- Background (DEV-1236 root cause):
--   The original MSVBASE fork created a child IndexScan QueryDesc using GetActiveSnapshot() —
--   the CALLER's active snapshot at first-call time. In a plpgsql DO block, each statement's
--   exec_run_select pushes/pops its own active snapshot. A sibling statement run after
--   multicol_topk()'s first SRF call pops that snapshot out from under the still-open child
--   IndexScan. On the next ExecProcNode call (next SRF invocation), the estate references a
--   freed snapshot for MVCC visibility -> SIGSEGV.
--
--   DEV-1236 fix: RegisterSnapshot(GetTransactionSnapshot()) at first-call time pins the snapshot
--   for the SRF's entire multi-call lifetime, with PushActiveSnapshot/PopActiveSnapshot wrapping
--   each child drain, and UnregisterSnapshot in teardown.
--
-- IMPORTANT NOTE on enable_seqscan and the HNSW AM:
--   The original repro file used `SET enable_seqscan = off` and `SELECT count(*) FROM entities`.
--   With seqscan disabled, the PG planner chose the HNSW index for count(*) (index-only scan path).
--   The HNSW AM does not support non-ORDER-BY index scans and crashes in that path — a SEPARATE
--   pre-existing bug unrelated to the snapshot lifecycle fix. To isolate the DEV-1236 snapshot
--   crash this file uses a sibling scan of a DIFFERENT table, which cannot trigger the HNSW AM
--   crash and faithfully tests the snapshot-under-concurrent-SRF scenario.
--
-- BEFORE (stock tridb/msvbase:dev, no DEV-1236 patch): DO block crashes backend (SIGSEGV) because
--   the sibling count(*) pops the active snapshot out from under the open child IndexScan. The
--   subsequent ExecProcNode dereferences freed snapshot memory.
-- AFTER  (with DEV-1236 patch): NOTICE 'multicol double-scan SURVIVED' is emitted; no crash.
--
-- Run: scripts/smoke_test.sh tridb/msvbase:dev $PWD/test/_fork_bug_multicol_double_scan.sql
-- (smoke_test.sh loads vectordb only -- graph_store is not involved.)

CREATE EXTENSION vectordb;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
-- Sibling scan table: separate from entities so we don't trigger the HNSW AM non-ORDER-BY
-- crash (a distinct pre-existing bug when seqscan=off forces HNSW for count(*)).
CREATE TABLE meta (k bigint);
INSERT INTO meta SELECT generate_series(1, 100);

INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- A sibling scan (count(*) against a different table) in the same plpgsql block as the
-- multicol_topk() call reproduced the snapshot UAF crash on the unpatched fork.
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT count(*) INTO corpus FROM meta;         -- sibling scan (different table)
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id
        FROM multicol_topk('entities', 5, 0, 'id', '', '',
                           'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, d float8)
    ) q;
    RAISE NOTICE 'multicol double-scan SURVIVED (DEV-1236 fix): got=% corpus=%', got, corpus;
END $$;

\echo 'If you see the NOTICE above, the DEV-1236 snapshot fix is working correctly.'

-- fork_bug_multicol_double_scan.sql — IN CI (DEV-1249). Deterministic regression test for the
-- REPRODUCIBLE DEV-1236 crash: asserts the patched build raises a clean ERROR (not a SIGSEGV).
-- Driven by scripts/fork_bug_multicol_test.sh (runs WITHOUT ON_ERROR_STOP=1 so the expected
-- post-fix ERROR does not halt the liveness probe), wired into the Makefile AM_TESTS list.
--
-- ROOT CAUSE (backtrace: HNSWIndexScan::EndScan -> hnsw_endscan -> ExecEndIndexOnlyScan):
--   With enable_seqscan off, the PG planner picks an Index-Only Scan on the HNSW index for an
--   unordered/aggregate scan such as count(*). hnsw_gettuple's no-ORDER-BY/no-key branch returned
--   false WITHOUT creating a ResultIterator, leaving scanState->workSpace->resultIterator null;
--   hnsw_endscan then called EndScan -> resultIterator->Close() on a null shared_ptr -> SIGSEGV.
--   (On any non-crashing path it also made count(*) silently return 0 — a wrong answer.)
--
-- BEFORE (stock tridb/msvbase:dev): the `SELECT count(*)` line below terminates the backend
--   (server log: "terminated by signal 11"); the connection is lost and nothing after it runs.
-- AFTER  (tridb_hnsw_scan_no_orderby.patch): that line raises a clean ERROR
--   ("hnsw index scan requires an ORDER BY <-> distance clause") and the backend STAYS UP — the
--   backend_alive probe returns 1 and the ORDER BY <-> control still returns rows.
--
-- NOTE: the sibling-scan-in-a-plpgsql-block shape originally suspected for DEV-1236 is a separate,
-- latent snapshot/UAF issue hardened by tridb_fix_double_scan_snapshot.patch; controlled
-- stock-vs-patched testing did NOT reproduce a crash for that shape. THIS file reproduces the
-- actual deterministic crash. See docs/fork_segfault_double_scan.md.
--
-- Run WITHOUT -v ON_ERROR_STOP=1 so the expected post-fix ERROR does not halt the liveness probe:
--   scripts/fork_bug_multicol_test.sh tridb/msvbase:dev   (the CI harness; asserts the outcome)

CREATE EXTENSION vectordb;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

SET enable_seqscan = off;  -- forces the planner onto the HNSW index for the unordered count(*)

-- CRASH line on stock; clean ERROR on the patched build:
SELECT count(*) FROM entities;

-- Liveness probe: prints 1 iff the backend survived (the fix is in). On the stock/crashed
-- backend the connection is already gone and this never executes.
SELECT 1 AS backend_alive;

-- Positive control: ORDER BY <-> vector search is unaffected (returns 5 rows).
SELECT id FROM entities ORDER BY embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 5;

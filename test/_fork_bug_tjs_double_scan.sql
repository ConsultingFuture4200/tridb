-- _fork_bug_tjs_double_scan.sql — NOT IN CI (leading underscore; absent from the Makefile
-- ENGINE_TESTS list). This file DELIBERATELY CRASHES THE BACKEND (SIGSEGV). Run it by hand only.
--
-- DEV-1236 (DIAGNOSE): minimal repro for the "second-scan-in-the-same-plpgsql-block" segfault.
-- Co-issuing ANY second table scan in the SAME plpgsql block as topk()/multicol_topk()/tjs()
-- terminates the backend with signal 11. This file captures the FAILING SHAPE across all three
-- operators so the root-cause analysis (docs/fork_segfault_double_scan.md) and the draft fix
-- (scripts/patches/tridb_fix_double_scan_snapshot.patch, UNBUILT) have a single executable witness.
--
-- Companion: test/_fork_bug_multicol_double_scan.sql (DEV-1169 attribution, multicol_topk only).
-- This file generalizes it to all three operators AND isolates the two independent triggers below.
--
-- STATUS (DEV-1236, x86 standin, static-verify run): UNBUILT-HERE. No docker image was built or run
-- in this session, so the crash was NOT re-observed here; the shapes below are derived from the
-- vendored C lifecycle (topk.cpp / multicol_topk.cpp / tjs_operator.cpp). The prior on-image
-- observation is recorded in test/_fork_bug_multicol_double_scan.sql (VERIFIED 2026-06-25 on
-- tridb/msvbase:dev). Do NOT add this file to CI; it crashes the backend by design.
--
-- Run (per failing block, one at a time — each crashes the backend):
--   scripts/smoke_test.sh tridb/msvbase:dev test/_fork_bug_tjs_double_scan.sql

CREATE EXTENSION IF NOT EXISTS vectordb;
CREATE EXTENSION IF NOT EXISTS graph_store;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- A small graph so the tjs() graph leg has something to probe.
SELECT graph_store.add_edge(1, 2);
SELECT graph_store.add_edge(1, 3);

SET enable_seqscan = off;

-- ===========================================================================================
-- SHAPE A — sibling scan of the OPERATOR'S OWN table, BEFORE the operator call (the classic case
-- recorded in fork_findings / ADR-0007). Each DECLARE..BEGIN..END is a separate witness; run ONE
-- at a time, because the first one crashes the backend and the rest never execute.
-- ===========================================================================================

-- A1: multicol_topk + prior count(*) of entities
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT count(*) INTO corpus FROM entities;                 -- sibling scan of the same table
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id FROM multicol_topk('entities', 5, 0, 'id', '', '',
                       'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, d float8)
    ) q;
    RAISE NOTICE 'A1 SURVIVED (unexpected): got=% corpus=%', got, corpus;
END $$;

-- A2: tjs (vector+relational+graph) + prior count(*) of entities
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT count(*) INTO corpus FROM entities;                 -- sibling scan of the same table
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id FROM tjs('entities', 5, 0, 1, 'id', 'ts < 500',
                       'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    RAISE NOTICE 'A2 SURVIVED (unexpected): got=% corpus=%', got, corpus;
END $$;

-- ===========================================================================================
-- SHAPE B — sibling scan of a DIFFERENT, unrelated table in the same block. If B also crashes,
-- the trigger is NOT "second scan of the operator's own relation" (lock/relcache aliasing) but the
-- general "any executor-driven query ran in this block before the operator drives its SPI plan"
-- (active-snapshot / SPI-stack state). This DISCRIMINATES the two candidate root causes.
-- ===========================================================================================

CREATE TABLE other_tbl (k int);
INSERT INTO other_tbl SELECT generate_series(1, 10);

-- B1: multicol_topk + prior scan of an UNRELATED table
DO $$
DECLARE got bigint[]; n bigint;
BEGIN
    SELECT count(*) INTO n FROM other_tbl;                     -- scan of a DIFFERENT table
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id FROM multicol_topk('entities', 5, 0, 'id', '', '',
                       'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, d float8)
    ) q;
    RAISE NOTICE 'B1 SURVIVED: got=% n=%  (=> trigger is the OWN-table scan, not any scan)', got, n;
END $$;

-- ===========================================================================================
-- SHAPE C — sibling scan AFTER the operator call (operator first, then the second scan), still in
-- one block. If C survives but A crashes, ordering matters: the operator must run before a sibling
-- scan disturbs the snapshot/SPI it captured. (Note: A already crashes, so C may be unreachable in
-- one file — kept as a documented hypothesis to test in isolation.)
-- ===========================================================================================

-- C1: tjs first, THEN a sibling count(*) of the same table.
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id FROM tjs('entities', 5, 0, 1, 'id', '',
                       'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    SELECT count(*) INTO corpus FROM entities;                 -- sibling scan AFTER the operator
    RAISE NOTICE 'C1 SURVIVED: got=% corpus=%', got, corpus;
END $$;

\echo 'If you saw any "SURVIVED" NOTICE, that shape did NOT crash — record which, it discriminates the root cause.'

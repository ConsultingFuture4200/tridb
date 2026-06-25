-- _fork_bug_multicol_double_scan.sql — NOT IN CI (leading underscore; absent from Makefile
-- ENGINE_TESTS). This file DELIBERATELY CRASHES THE BACKEND. Run it by hand only.
--
-- Purpose (DEV-1169 / Linus review #9): PROVE that the "double-scan segfault" — issuing another
-- query against the operator's own target table in the SAME plpgsql block as the operator call —
-- is a PRE-EXISTING MSVBASE fork bug in the topk/multicol_topk SPI-driven-executor lifecycle, NOT
-- introduced by the TJS operator (DEV-1169). It uses UNMODIFIED multicol_topk only: no tjs(), no
-- graph_store extension, no graph leg.
--
-- VERIFIED 2026-06-25 on tridb/msvbase:dev: the DO block below terminates the backend with
--   "server process (PID …) was terminated by signal 11: Segmentation fault"
-- (postgres server log). Because tjs() forks the same execFagins lifecycle, it inherits the bug;
-- the canonical e2e test (test/canonical_e2e_test.sql) sidesteps it by NOT co-issuing a second
-- scan of the operator's table in the early-termination block. The real fix belongs to the fork's
-- executor-driving lifecycle (a separate hardening task). See docs/decisions/0007-tjs-operator.md.
--
-- Run: scripts/smoke_test.sh tridb/msvbase:dev test/_fork_bug_multicol_double_scan.sql
-- (smoke_test.sh loads vectordb only — proving graph_store is not involved.)

CREATE EXTENSION vectordb;

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);
SET enable_seqscan = off;

-- A sibling scan of the SAME table (SELECT count(*) FROM entities) in the SAME plpgsql block as the
-- multicol_topk() call segfaults the backend. No tjs, no graph_store.
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT count(*) INTO corpus FROM entities;     -- sibling scan of multicol_topk's own table
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id
        FROM multicol_topk('entities', 5, 0, 'id', '', '',
                           'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, d float8)
    ) q;
    RAISE NOTICE 'multicol double-scan SURVIVED (unexpected): got=% corpus=%', got, corpus;
END $$;

\echo 'If you see this line, the fork bug did NOT reproduce — investigate (TJS may then be the cause).'

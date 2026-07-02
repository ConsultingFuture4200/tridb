-- relaxed_order_guard.sql — advisor plan 022 regression.
--
-- TriDB inherits MSVBASE's VBASE relaxed-monotonicity executor hunk (patch/Postgres.patch):
-- a new IndexScanDescData.xs_inorder flag is copied into a single per-query EState bool
-- (is_index_inorder) which arms an early BREAK of a bounded Sort in nodeSort.c
-- (`if (is_index_inorder && tuplesort_heapfull(...)) break;`). Two inherited weaknesses:
--   1. xs_inorder was never zero-initialized (RelationGetIndexScan did not touch it), so for a
--      stock (non-relaxed) AM it held garbage — a bounded Sort (LIMIT n) over an ORDINARY btree
--      index scan could read a stale/garbage xs_inorder, arm the early-stop, and TRUNCATE its
--      input -> wrong top-N for queries unrelated to vector search.
--   2. The `if (cmp < 0) elog(ERROR, "index returned tuples in wrong order")` safety net was
--      commented out for ALL order-by-op scans, not just relaxed ones.
--
-- advisor plan 022 (scripts/patches/tridb_relaxed_order_executor_guard.patch):
--   - genam.c RelationGetIndexScan zero-inits scan->xs_inorder = false;
--   - nodeIndexscan.c gates is_index_inorder on the driving AM's amcanrelaxedorderbyop, so the
--     early-stop fires ONLY for a genuinely-relaxed (HNSW) scan;
--   - nodeIndexscan.c restores the wrong-order ERROR for NON-relaxed AMs.
--
-- This regression proves both directions:
--   (a) a bounded Sort (ORDER BY <non-indexed, non-vector col> LIMIT n) over an ORDINARY btree
--       index scan returns the EXACT correct top-n — the early-stop no longer truncates it;
--   (b) an HNSW `ORDER BY v <-> q LIMIT k` scan still returns the expected near set — the relaxed
--       path is unbroken.
--
-- Runs under psql -v ON_ERROR_STOP=1 (scripts/graph_test.sh): any RAISE EXCEPTION aborts the suite
-- with a nonzero exit. A PASS is reaching the final \echo with all NOTICEs emitted.

-- =====================================================================================
-- (a) NON-RELAXED bounded Sort exactness — the xs_inorder-garbage / early-stop-truncation guard.
-- =====================================================================================
-- 200 rows. a = id (distinct), so the top-10 by ascending a is exactly ids 1..10. b is an
-- unrelated shuffled column carrying the ONLY btree index. Selecting `id`/`a` (not in the index)
-- forbids an Index-Only Scan, and enable_seqscan=off forces a plain Index Scan on b feeding a
-- bounded Sort by a — the exact IndexScan -> bounded-Sort shape the early-stop could truncate.
CREATE TEMP TABLE rog_t (id bigint, a int, b int);
INSERT INTO rog_t SELECT g, g, (g * 7) % 200 FROM generate_series(1, 200) AS g;
CREATE INDEX rog_b_idx ON rog_t (b);
ANALYZE rog_t;

SET enable_seqscan = off;
SET enable_bitmapscan = off;

DO $$
DECLARE
    got bigint[];
    expected bigint[] := ARRAY[1,2,3,4,5,6,7,8,9,10];
BEGIN
    SELECT array_agg(id ORDER BY id) INTO got
    FROM (SELECT id FROM rog_t WHERE b >= 0 ORDER BY a LIMIT 10) s;

    IF got IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'plan022 (a) FAILED: bounded Sort over an ordinary btree index scan returned % (expected exact top-10 by a = %) — a stale/garbage xs_inorder armed the early-stop and truncated a NON-relaxed scan', got, expected;
    END IF;
    RAISE NOTICE 'PASS (a): bounded Sort over ordinary btree index scan returns the EXACT top-10 (early-stop does not truncate a non-relaxed scan)';
END $$;

-- =====================================================================================
-- (b) RELAXED HNSW path preserved — the early-stop STILL works for a genuinely-relaxed scan.
-- =====================================================================================
CREATE EXTENSION vectordb;

CREATE TABLE rog_vec (id bigint PRIMARY KEY, embedding float8[8]);
-- dim0 = i (dominant); other dims tiny — so "near q={1000,0,...}" ~ id near 1000.
INSERT INTO rog_vec
SELECT i, ARRAY[i, i%10, i%10, i%10, i%10, i%10, i%10, i%10]::float8[]
FROM generate_series(1, 2000) AS i;

CREATE INDEX rog_vec_hnsw ON rog_vec USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- NB: the query vector is inlined as a literal (psql \set does not expand inside $$ bodies).
DO $$
DECLARE
    got bigint[];
    n int;
    near int;
BEGIN
    SELECT array_agg(id) INTO got FROM (
        SELECT id FROM rog_vec
        ORDER BY embedding <-> '{1000,0,0,0,0,0,0,0}'::float8[]
        LIMIT 5
    ) s;

    n := coalesce(array_length(got, 1), 0);
    IF n <> 5 THEN
        RAISE EXCEPTION 'plan022 (b) FAILED: HNSW ORDER BY <-> LIMIT 5 returned % rows, expected 5 — relaxed bounded-Sort early-stop broken', n;
    END IF;

    -- dim0 dominance makes the 5 true nearest neighbours all cluster tightly around id=1000.
    SELECT count(*) INTO near FROM unnest(got) AS g WHERE g BETWEEN 975 AND 1025;
    IF near < 4 THEN
        RAISE EXCEPTION 'plan022 (b) FAILED: only % of 5 HNSW neighbours fall near id=1000 (relaxed path degraded)', near;
    END IF;
    RAISE NOTICE 'PASS (b): HNSW ORDER BY <-> LIMIT 5 returns the expected near set (% of 5 within +/-25 of id=1000; relaxed early-stop intact)', near;
END $$;

RESET enable_seqscan;
RESET enable_bitmapscan;

\echo '============ relaxed-monotonicity executor guard (advisor plan 022): VERIFIED ============'

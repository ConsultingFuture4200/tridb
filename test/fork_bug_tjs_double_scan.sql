-- fork_bug_tjs_double_scan.sql — IN CI (advisor plan 012). PASSING regression for the tjs() shape
-- of the double-scan snapshot/UAF bug (DEV-1236): a sibling scan of the operator's OWN target table
-- issued in the SAME plpgsql block as a tjs() call. Pre-fix this shape was the classic
-- segfault trigger (see test/_fork_bug_tjs_double_scan.sql SHAPE A and the fork-limitation note in
-- test/canonical_e2e_test.sql:132-139); the fix (scripts/patches/tridb_fix_double_scan_snapshot.patch,
-- applied + sentinel-verified in scripts/lib/msvbase_patches.sh) hardens the active-snapshot / SPI
-- lifecycle so the block now COMPLETES cleanly. This test asserts the fixed behavior.
--
-- Driven by scripts/fork_bug_tjs_double_scan_test.sh (in the Makefile AM_TESTS list). A backend
-- crash (signal 11 / lost connection / missing PASS+backend_alive) FAILS LOUD in the harness.
--
-- Companion crash-witnesses (NOT in CI; crash by design): test/_fork_bug_tjs_double_scan.sql and
-- test/_fork_bug_multicol_double_scan.sql. The deterministic unordered-scan crash has its own
-- passing regression in test/fork_bug_multicol_double_scan.sql.

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store_am;  -- v1 native AM (v0-compat surface, ADR-0013 Stage B)

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);
INSERT INTO entities
SELECT k, 'c' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- A small graph so the tjs() graph leg reaches {10,20,30} from src 1.
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);

SET enable_seqscan = off;  -- forces the HNSW index scan inside tjs()

-- SHAPE A (pre-fix crash trigger): a sibling scan of `entities` BEFORE the tjs() call, both in one
-- plpgsql block. Post-fix this must COMPLETE and return the graph-restricted top-k.
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT count(*) INTO corpus FROM entities;                 -- sibling scan of the operator's own table
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id
        FROM tjs('entities', 5, 0, 1::bigint, 'id', '',
                 'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    -- src 1 reaches exactly {10,20,30}; k=5 so all three survivors are returned (order by distance
    -- to q=[19] is 20,10,30, but this asserts the set — the point is completion, not a crash).
    IF NOT (got @> ARRAY[10,20,30]::bigint[] AND ARRAY[10,20,30]::bigint[] @> got) THEN
        RAISE EXCEPTION 'tjs double-scan regression: got % (expected set {10,20,30})', got;
    END IF;
    RAISE NOTICE 'PASS tjs double-scan: sibling count(*)=% + tjs() completed, top-k = %', corpus, got;
END $$;

-- Liveness probe: prints 1 iff the backend survived the block (the snapshot fix is in).
SELECT 1 AS backend_alive;

-- Early-terminating tri-modal composition (DEV-1169 functional shape).
--
-- Canonical shape: rank (src)->(dst) by SOURCE embedding distance to the
-- question, filter on DST timestamp, take top-k. Drive from the HNSW index scan
-- (early-terminating); expand graph + apply relational filter per candidate.
--
-- FORK CONSTRAINT (recorded, important): MSVBASE exposes NO working scalar vector
-- distance — l2_distance() / `<->` return 0 outside an index scan (even for
-- integer vectors). Real distances exist ONLY inside the HNSW index scan's
-- internal computation. Consequences:
--   * Exact top-k CANNOT be produced by a SQL over-fetch+re-rank, and exact
--     ground truth CANNOT be computed by a seq-scan. The relaxed-monotonicity
--     finalize (DEV-1168) MUST be a C operator reading the index's internal
--     distances — it cannot lean on a scalar `<->`. This constrains its design.
--   * Therefore this test verifies the composition's STRUCTURE and efficiency,
--     not exact top-k ranking: (1) all three legs engage, (2) the relational
--     filter is load-bearing, (3) the ANN scan early-terminates (examines
--     << corpus). Exact-ranking verification is deferred to DEV-1168.

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store_am;  -- v1 native AM (v0-compat surface, ADR-0013 Stage B)

CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]);

-- Source i: dim0 = i (dominant), other dims tiny — so "near q=[1000,...]" ~ i near 1000.
-- Dst (10000+i): embedding far (dim0 = 1e6+i). Dst is STALE (ts 999) for odd i, fresh
-- (ts 100) for even i, so the relational filter on dst.ts is non-trivial.
INSERT INTO entities
SELECT i, 'src ' || i, 100,
       ARRAY[i, i%10, i%10, i%10, i%10, i%10, i%10, i%10]::float8[]
FROM generate_series(1, 2000) AS i;
INSERT INTO entities
SELECT 10000 + i, 'dst ' || i,
       CASE WHEN i % 2 = 0 THEN 100 ELSE 999 END,
       ARRAY[1000000+i, i%10, i%10, i%10, i%10, i%10, i%10, i%10]::float8[]
FROM generate_series(1, 2000) AS i;

CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

SELECT graph_store.add_edge(i, 10000 + i) FROM generate_series(1, 2000) AS i;

SET enable_seqscan = off;   -- force the ANN index-scan driver

-- 1) Three legs engage + relational filter load-bearing -----------------------
DO $$
DECLARE n_results int; fresh int; stale int;
BEGIN
    -- Three legs engage: the ANN-driven composition (graph traversal -> ts filter
    -- -> vector order) yields 5 results. (Result RANKING is approximate per the
    -- relaxed-monotonicity finding; we assert the legs compose, not exact order.)
    CREATE TEMP TABLE r_filtered AS
        SELECT d.id, d.ts
        FROM entities src
        JOIN LATERAL graph_store.neighbors(src.id) AS nb(id) ON true
        JOIN entities d ON d.id = nb.id
        WHERE d.ts < 500
        ORDER BY src.embedding <-> '{1000,0,0,0,0,0,0,0}'
        LIMIT 5;
    SELECT count(*) INTO n_results FROM r_filtered;
    IF n_results <> 5 THEN
        RAISE EXCEPTION 'three legs FAILED: % results (graph+filter+vector did not yield 5)', n_results;
    END IF;
    RAISE NOTICE 'PASS three legs engage: graph->filter->vector yields % results', n_results;

    -- Filter load-bearing — DETERMINISTIC, independent of ANN ordering and the
    -- scalar `<->` (which is unreliable per fork finding #2). Over a FIXED source
    -- range, exactly half the dst are stale, and the ts<500 filter removes them.
    SELECT count(*) FILTER (WHERE d.ts < 500), count(*) FILTER (WHERE d.ts >= 500)
    INTO fresh, stale
    FROM entities src
    JOIN LATERAL graph_store.neighbors(src.id) AS nb(id) ON true
    JOIN entities d ON d.id = nb.id
    WHERE src.id BETWEEN 991 AND 1010;
    IF fresh <> 10 OR stale <> 10 THEN
        RAISE EXCEPTION 'filter load-bearing FAILED: src 991..1010 expected fresh=10 stale=10, got %/%', fresh, stale;
    END IF;
    RAISE NOTICE 'PASS filter load-bearing: of 20 dst (src 991..1010), % fresh / % stale -> ts<500 removes the stale', fresh, stale;
END $$;

-- 2) Early termination: the HNSW ANN scan emits << the 2000-source corpus. ------
-- CAVEAT (per Linus review): this is PIPELINE-level early termination — the Limit
-- stops the nested-loop driver after the index scan EMITS enough source rows
-- (here 8: 5 fresh kept + 3 stale rejected by the filter). It is (a) the rows
-- EMITTED by the index-scan node, not the HNSW algorithm's internal candidate
-- count, and (b) dependent on filter selectivity: a 1-in-N-fresh filter forces
-- ~5N emissions and degrades to a full scan once 5N >= corpus. We assert a bound
-- (<=400) that holds for this regime; the general guarantee is DEV-1168's job.
DO $$
DECLARE rec record; plan text := ''; ann_rows int := -1; m text[];
BEGIN
    FOR rec IN EXECUTE
        'EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) '
        'SELECT d.id FROM entities src '
        'JOIN LATERAL graph_store.neighbors(src.id) AS nb(id) ON true '
        'JOIN entities d ON d.id = nb.id '
        'WHERE d.ts < 500 '
        'ORDER BY src.embedding <-> ''{1000,0,0,0,0,0,0,0}'' LIMIT 5'
    LOOP
        plan := plan || rec."QUERY PLAN" || E'\n';
        -- anchor to the index-scan NODE line, not any line mentioning the index
        IF rec."QUERY PLAN" ~ 'Index Scan using entities_hnsw' THEN
            m := regexp_match(rec."QUERY PLAN", 'actual rows=([0-9]+)');
            IF m IS NOT NULL THEN ann_rows := m[1]::int; END IF;
        END IF;
    END LOOP;

    RAISE NOTICE E'plan:\n%', plan;
    IF plan NOT LIKE '%Index Scan using entities_hnsw%' THEN
        RAISE EXCEPTION 'NOT ANN-driven: no HNSW index scan node in plan';
    END IF;
    IF ann_rows < 0 THEN
        RAISE EXCEPTION 'could not read actual rows from the entities_hnsw index-scan node';
    END IF;
    IF ann_rows > 400 THEN
        RAISE EXCEPTION 'early-termination FAILED: ANN scan emitted % of 2000 sources', ann_rows;
    END IF;
    RAISE NOTICE 'PASS early termination: ANN index-scan emitted % source rows of 2000 (pipeline-level, this filter regime)', ann_rows;
END $$;

\echo '============ early-terminating tri-modal composition: STRUCTURE VERIFIED ============'

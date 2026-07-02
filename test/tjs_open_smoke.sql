-- tjs_open_smoke.sql — smoke test for the seedless multi-seed tjs_open operator (ADR-0012 B).
--
-- Proves: (1) it returns sensible top-k vector-ranked, (2) seedless seeding (no caller src), (3)
-- multi-source graph expansion + bridge injection: a graph-reachable bridge that is PAST the vector
-- frontier is admitted into the top-k it would otherwise miss, (4) early termination (<< corpus).

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store;

CREATE TABLE paragraphs (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding float8[8]
);

-- entity k has embedding [k,0,...]; query is near 19. 2000 rows so early-termination is meaningful.
INSERT INTO paragraphs
SELECT k, 'chunk ' || k, 100, ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;

CREATE INDEX paragraphs_hnsw ON paragraphs USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

SET enable_seqscan = off;

-- ===========================================================================
-- ASSERTION 1: pure top-k by vector (no useful graph). With q=[19,...], the nearest ids are
-- {19,18,20,17,21,...}. m_seeds=3, hops=1, but no edges -> bridges = {seeds} only -> the answer is
-- vector top-k. This checks the seedless vector ranking path and the SRF lifecycle.
-- ===========================================================================
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(id ORDER BY id) INTO got FROM (
        SELECT t.id
        FROM tjs_open('paragraphs', 5, 0, 3, 1, 'id', '',
                      'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    RAISE NOTICE 'vector-only tjs_open top-5 (sorted) = %', got;
    -- The 5 nearest to 19 are {17,18,19,20,21}.
    IF got IS DISTINCT FROM ARRAY[17,18,19,20,21]::bigint[] THEN
        RAISE EXCEPTION 'tjs_open vector-only FAILED: got % (expected {17,18,19,20,21})', got;
    END IF;
    RAISE NOTICE 'PASS seedless vector ranking: %', got;
END $$;

-- ===========================================================================
-- ASSERTION 2: BRIDGE INJECTION past the vector frontier.
-- Seeds (ANN top-3 of q=19) = {19,18,20}. Wire a graph edge 19 -> 1500 (a FAR vector node).
-- With hops>=1, 1500 becomes a bridge. A pure vector top-5 would never include 1500 (distance
-- |1500-19| is huge). tjs_open must INJECT 1500 into the result because it is graph-reachable from a
-- seed, even though it is far past the vector frontier. We assert 1500 appears in a top-k that is
-- large enough to hold it after the near neighbors.
-- ===========================================================================
SELECT graph_store.add_edge(19, 1500);
SELECT graph_store.add_edge(18, 1400);

DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(id ORDER BY id) INTO got FROM (
        SELECT t.id
        FROM tjs_open('paragraphs', 10, 0, 3, 1, 'id', '',
                      'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    RAISE NOTICE 'tjs_open top-10 with bridges 1500,1400 (sorted) = %', got;
    IF NOT (1500 = ANY(got)) THEN
        RAISE EXCEPTION 'tjs_open BRIDGE INJECTION FAILED: 1500 (graph bridge from seed 19) not in top-10 %', got;
    END IF;
    IF NOT (1400 = ANY(got)) THEN
        RAISE EXCEPTION 'tjs_open BRIDGE INJECTION FAILED: 1400 (graph bridge from seed 18) not in top-10 %', got;
    END IF;
    RAISE NOTICE 'PASS bridge injection: 1500 & 1400 admitted past the vector frontier into top-10 = %', got;
    RAISE NOTICE 'bridges_injected = %', tjs_open_bridges_injected();
END $$;

-- ===========================================================================
-- ASSERTION 3: EARLY TERMINATION (TR-1). With a tight term_cond the scan must examine far fewer than
-- the 2000-row corpus (the bridge injection must NOT defeat termination — bridges don't reset drops).
-- ===========================================================================
DO $$
DECLARE ex bigint;
BEGIN
    PERFORM id FROM tjs_open('paragraphs', 5, 10, 3, 1, 'id', '',
                             'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
    ex := tjs_open_candidates_examined();
    RAISE NOTICE 'tjs_open candidates_examined = % (corpus 2000)', ex;
    IF ex >= 2000 THEN
        RAISE EXCEPTION 'tjs_open EARLY TERMINATION FAILED: examined % >= corpus 2000 (blocking!)', ex;
    END IF;
    RAISE NOTICE 'PASS early termination (TR-1): examined % << 2000', ex;
END $$;

-- ===========================================================================
-- ASSERTION 4: multi-hop expansion. edge 19->1500->777. hops=2 makes 777 a bridge; hops=1 does not.
-- ===========================================================================
SELECT graph_store.add_edge(1500, 777);

DO $$
DECLARE got1 bigint[];
DECLARE got2 bigint[];
BEGIN
    SELECT array_agg(id) INTO got1 FROM (
        SELECT t.id FROM tjs_open('paragraphs', 12, 0, 3, 1, 'id', '',
                      'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)) q;
    SELECT array_agg(id) INTO got2 FROM (
        SELECT t.id FROM tjs_open('paragraphs', 12, 0, 3, 2, 'id', '',
                      'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint)) q;
    RAISE NOTICE 'hops=1 -> %, hops=2 -> %', got1, got2;
    IF (777 = ANY(got1)) THEN
        RAISE EXCEPTION 'tjs_open MULTI-HOP FAILED: 777 reachable at hops=1 (should need 2)';
    END IF;
    IF NOT (777 = ANY(got2)) THEN
        RAISE EXCEPTION 'tjs_open MULTI-HOP FAILED: 777 NOT reachable at hops=2 %', got2;
    END IF;
    RAISE NOTICE 'PASS multi-hop expansion: 777 admitted only at hops=2';
END $$;

-- ===========================================================================
-- ASSERTION 5: STRICT NULL guard. tjs_open is STRICT, so a NULL argument yields a clean
-- zero-row result (the SRF is never entered), not a backend crash (previously: NULL table_name
-- dereferenced -> segfault, an unprivileged single-statement DoS).
-- ===========================================================================
DO $$
DECLARE n bigint;
BEGIN
    -- STRICT: NULL arg returns no rows (previously: backend segfault)
    SELECT count(*) INTO n
    FROM tjs_open(NULL, 5, 0, 3, 1, 'id', '',
                  'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
    IF n <> 0 THEN
        RAISE EXCEPTION 'tjs_open NULL-arg guard FAILED: expected 0 rows, got %', n;
    END IF;
    RAISE NOTICE 'PASS STRICT NULL guard: tjs_open(NULL,...) returned 0 rows (no crash)';
END $$;

-- ===========================================================================
-- ASSERTION 6: UNIFIED-METRIC EMISSION ORDER. A bridge and a vector winner interleave in the top-k
-- with true distances that INVERT under the old mixed units. Tiny deterministic corpus, query at
-- the origin: id 10 (true dist 1.0, single seed), 13 (2.0), 11 (4.5, pure vector winner),
-- 12 (5.0, bridge via edge 10->12). k=4, m_seeds=1 -> bridge_cap = k/2 = 2, so bridge 12 is
-- carried into the merge as a BRIDGE item (its heap key is the bug site). Old code keyed the bridge
-- on sqrt'd Euclidean (5.0) but vector winners on squared L2 (11 -> 20.25), so it emitted 12 BEFORE
-- 11 -> {10,13,12,11}. The fix squares the bridge key (25.0 > 20.25) so 12 sorts after 11.
-- ===========================================================================
CREATE TABLE ord_paragraphs (id bigint PRIMARY KEY, embedding float8[8]);
INSERT INTO ord_paragraphs VALUES
    (10, ARRAY[1.0,0,0,0,0,0,0,0]::float8[]),
    (11, ARRAY[4.5,0,0,0,0,0,0,0]::float8[]),
    (12, ARRAY[5.0,0,0,0,0,0,0,0]::float8[]),
    (13, ARRAY[2.0,0,0,0,0,0,0,0]::float8[]);
CREATE INDEX ord_paragraphs_hnsw ON ord_paragraphs USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);
SELECT graph_store.add_edge(10, 12);

DO $$
DECLARE got bigint[];
BEGIN
    WITH r AS (
        SELECT t.id
        FROM tjs_open('ord_paragraphs', 4, 0, 1, 1, 'id', '',
                      'embedding <-> ''{0,0,0,0,0,0,0,0}''') AS t(id bigint)
    )
    SELECT array_agg(id) INTO got FROM r;   -- no ORDER BY: preserves emission order
    RAISE NOTICE 'tjs_open ordering emission = % (expected {10,13,11,12})', got;
    IF got IS DISTINCT FROM ARRAY[10,13,11,12]::bigint[] THEN
        RAISE EXCEPTION 'tjs_open ORDERING FAILED: got % (expected {10,13,11,12}; old mixed-unit bug gives {10,13,12,11})', got;
    END IF;
    RAISE NOTICE 'PASS unified-metric ordering: bridge 12 emitted after nearer vector winner 11';
END $$;

-- ===========================================================================
-- ASSERTION 7: BOUNDED BRIDGE SHARE (blend policy). A hub seed (id 300) has 10 far bridges (edges
-- 300->310..319), so the bridge set (11 incl. the seed) is >= k. id 301 is the 2nd-nearest vector
-- winner and is NOT graph-reachable from the seed (no incoming edge, and m_seeds=1 makes only 300 a
-- seed). Old bridges-take-all filled all k slots with bridges -> 301 evicted (vector modality gone).
-- The k/2 cap reserves slots for vector winners, so the pure vector winner 301 survives.
-- ===========================================================================
CREATE TABLE hub_paragraphs (id bigint PRIMARY KEY, embedding float8[8]);
INSERT INTO hub_paragraphs VALUES
    (300, ARRAY[1.0,0,0,0,0,0,0,0]::float8[]),
    (301, ARRAY[2.0,0,0,0,0,0,0,0]::float8[]);
INSERT INTO hub_paragraphs
SELECT g, ARRAY[g::float8,0,0,0,0,0,0,0]::float8[] FROM generate_series(310, 319) AS g;
CREATE INDEX hub_paragraphs_hnsw ON hub_paragraphs USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);
SELECT graph_store.add_edge(300, g) FROM generate_series(310, 319) AS g;

DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(id) INTO got FROM (
        SELECT t.id
        FROM tjs_open('hub_paragraphs', 6, 0, 1, 1, 'id', '',
                      'embedding <-> ''{0,0,0,0,0,0,0,0}''') AS t(id bigint)
    ) q;
    RAISE NOTICE 'tjs_open hub top-6 = % (11 bridges available, k=6)', got;
    IF NOT (301 = ANY(got)) THEN
        RAISE EXCEPTION 'tjs_open BLEND POLICY FAILED: pure vector winner 301 evicted by bridges (got %)', got;
    END IF;
    RAISE NOTICE 'PASS bounded bridge share: pure vector winner 301 survived a >= k bridge set';
END $$;

SELECT 'tjs_open smoke: ALL ASSERTIONS PASSED' AS result;

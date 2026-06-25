-- parse_canonical.sql — DEV-1167 SQL/PGQ canonical surface end-to-end test.
--
-- Proves the FRONT DOOR (graph_store.graph_query) lowers the ONE canonical query (spec §5)
-- into a single tjs() call (DEV-1169) and returns the SAME top-k as the direct tjs() call —
-- FR-4 "one plan". Same corpus as test/canonical_e2e_test.sql + test/trimodal_compose.sql:
-- entity k = embedding [k,0,...]; entity 40 is stale (ts 999); src vertex 1 -> {10,20,30,40}.
--
-- Asserts:
--   (1) the canonical query VIA the surface returns {20,10} (= the direct tjs() answer = FR-4),
--   (2) the relational filter is load-bearing (q={40,...}: filtered window -> 30, wide window -> 40),
--   (3) the scope guard: 5 off-template variants each RAISE EXCEPTION with no rows.
--
-- Runs under psql -v ON_ERROR_STOP=1: any RAISE EXCEPTION fails the suite (nonzero exit).

CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store;

-- Identical corpus to canonical_e2e_test.sql. INSERTs precede CREATE INDEX (the MSVBASE fork's
-- HNSW AM crashes on incremental inserts into a built index — see canonical_e2e_test.sql:33-36).
CREATE TABLE entities (
    id        bigint PRIMARY KEY,
    chunk     text,
    ts        int,
    embedding float8[8]
);

INSERT INTO entities
SELECT k,
       'chunk ' || k,
       CASE WHEN k = 40 THEN 999 ELSE 100 END,
       ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;

CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);

-- graph: source vertex 1 relates to {10, 20, 30, 40}.
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);

SET enable_seqscan = off;

-- ===========================================================================
-- ASSERTION 1: the canonical query through the GRAPH_TABLE surface returns {20,10}.
-- This is the SAME top-k as the direct tjs() call in canonical_e2e_test.sql ASSERTION 1
-- (one plan, FR-4). The surface returns the projected `chunk` ('chunk 20','chunk 10'); we
-- map back to ids to compare against the tjs() oracle's {20,10}.
-- ===========================================================================
DO $$
DECLARE got bigint[];
BEGIN
    SELECT array_agg(replace(c, 'chunk ', '')::bigint) INTO got
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'
        LIMIT 2
    $q$) AS c;

    IF got IS DISTINCT FROM ARRAY[20,10]::bigint[] THEN
        RAISE EXCEPTION 'canonical surface FAILED: got % (expected {20,10} = the direct tjs() answer)', got;
    END IF;
    RAISE NOTICE 'PASS one plan (FR-4): canonical query via GRAPH_TABLE surface top-2 = % (= direct tjs())', got;
END $$;

-- ===========================================================================
-- ASSERTION 2: the relational filter is load-bearing (the 40-vs-30 construction).
-- q = [40,...]: among reachable {10,20,30,40}, 40 is the exact match (dist 0). The timestamp
-- window IN (100) EXCLUDES the stale 40 (ts 999) -> closest survivor is 30. A wide window
-- IN (100, 999) INCLUDES 40 -> 40. Both stay on-template (the filter is exercised, not removed),
-- proving the predicate is load-bearing. Mirrors canonical_e2e_test.sql ASSERTION 2.
-- ===========================================================================
DO $$
DECLARE with_filter bigint; wide_window bigint;
BEGIN
    SELECT replace(c, 'chunk ', '')::bigint INTO with_filter
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100)
        ORDER BY src_embedding <-> '{40,0,0,0,0,0,0,0}'
        LIMIT 1
    $q$) AS c;

    SELECT replace(c, 'chunk ', '')::bigint INTO wide_window
    FROM graph_store.graph_query($q$
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
        WHERE src.id = 1 AND timestamp IN (100, 999)
        ORDER BY src_embedding <-> '{40,0,0,0,0,0,0,0}'
        LIMIT 1
    $q$) AS c;

    IF with_filter <> 30 OR wide_window <> 40 THEN
        RAISE EXCEPTION 'filter not load-bearing: window(100)=% window(100,999)=% (expected 30, 40)',
            with_filter, wide_window;
    END IF;
    RAISE NOTICE 'PASS filter load-bearing: window IN(100) drops the closest (40) -> 30; IN(100,999) -> 40';
END $$;

-- ===========================================================================
-- ASSERTION 3: the SCOPE GUARD. Each off-template variant must RAISE EXCEPTION and return
-- no rows. We assert each one raises by catching it; a variant that does NOT raise is a
-- scope-guard failure (the surface generalized beyond the one canonical query — golden rule 4).
-- ===========================================================================
DO $$
DECLARE
    -- 5 off-template variants, each violating exactly one template constraint.
    variants text[] := ARRAY[
        -- (a) wrong edge label (knows_about != related_to)
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:knows_about]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 2$v$,
        -- (b) two hops (extra ->(mid)-> ) — v1 is single-hop only
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(mid:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 2$v$,
        -- (c) wrong COLUMNS projection (dst.embedding instead of dst.chunk)
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.embedding AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}' LIMIT 2$v$,
        -- (d) missing the <-> vector ORDER BY (ordinary column order-by)
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY timestamp LIMIT 2$v$,
        -- (e) missing LIMIT (no top-k bound)
        $v$SELECT chunk FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
             COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
           WHERE src.id = 1 AND timestamp IN (100) ORDER BY src_embedding <-> '{19,0,0,0,0,0,0,0}'$v$
    ];
    v       text;
    n       int;
    raised  boolean;
BEGIN
    FOREACH v IN ARRAY variants LOOP
        raised := false;
        BEGIN
            PERFORM graph_store.graph_query(v);
        EXCEPTION WHEN others THEN
            raised := true;
        END;
        IF NOT raised THEN
            RAISE EXCEPTION 'scope guard FAILED: off-template variant was ACCEPTED: %', v;
        END IF;
    END LOOP;
    RAISE NOTICE 'PASS scope guard: all % off-template variants rejected (RAISE, no rows)',
        array_length(variants, 1);
END $$;

\echo '================ DEV-1167 canonical surface: ALL TESTS PASSED ================'

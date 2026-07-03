-- tjs_filter_first_test.sql — DEV-1290: the tjs() filter-first physical body, operator-level.
--
-- Both bodies on the canonical corpus in one session: answer parity, the join-order companion,
-- examined-count divergence (SM-3), error guards, alternating-call SPI lifecycle. Requires only
-- vectordb + graph_store (runs under scripts/graph_test.sh / ENGINE_TESTS).
CREATE EXTENSION vectordb;
CREATE EXTENSION graph_store;

CREATE TABLE entities (
    id bigint PRIMARY KEY, chunk text, ts int, embedding float8[8]
);
INSERT INTO entities
SELECT k, 'chunk ' || k, CASE WHEN k = 40 THEN 999 ELSE 100 END,
       ARRAY[k,0,0,0,0,0,0,0]::float8[]
FROM generate_series(1, 2000) AS k;
CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)
    WITH (dimension = 8, distmethod = l2_distance);
SELECT graph_store.add_edge(1, 10);
SELECT graph_store.add_edge(1, 20);
SELECT graph_store.add_edge(1, 30);
SELECT graph_store.add_edge(1, 40);
SET enable_seqscan = off;

-- (1) companion NULL before any call
DO $$ BEGIN
    ASSERT tjs_last_join_order() IS NULL, 'must be NULL before first call';
    RAISE NOTICE 'PASS 1: tjs_last_join_order NULL before first call';
END $$;

-- (2) 7-arg legacy call: unchanged behavior, records vector_first
DO $$ DECLARE got bigint[];
BEGIN
    SELECT array_agg(t.id) INTO got FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, chunk text);
    ASSERT got = ARRAY[20,10]::bigint[], format('7-arg tjs answer changed: %s', got);
    ASSERT tjs_last_join_order() = 'vector_first', 'legacy call must record vector_first';
    RAISE NOTICE 'PASS 2: 7-arg legacy tjs -> {20,10}, records vector_first';
END $$;

-- (3) 8-arg vector_first: identical to legacy
DO $$ DECLARE got bigint[];
BEGIN
    SELECT array_agg(t.id) INTO got FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'vector_first') AS t(id bigint, chunk text);
    ASSERT got = ARRAY[20,10]::bigint[], format('8-arg vector_first answer: %s', got);
    ASSERT tjs_last_join_order() = 'vector_first', 'must record vector_first';
    RAISE NOTICE 'PASS 3: 8-arg vector_first -> {20,10}';
END $$;

-- (4) filter_first: same answers as vector_first, records itself, examined = qualifying count
DO $$ DECLARE got bigint[]; ex bigint;
BEGIN
    SELECT array_agg(t.id) INTO got FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
    ASSERT got = ARRAY[20,10]::bigint[], format('filter_first answer: %s (expected {20,10})', got);
    ASSERT tjs_last_join_order() = 'filter_first', 'must record filter_first';
    ex := tjs_candidates_examined();
    -- qualifying = reachable {10,20,30,40} with ts<500 -> {10,20,30} (40 has ts 999)
    ASSERT ex = 3, format('filter_first examined %s (expected 3 = qualifying count)', ex);
    RAISE NOTICE 'PASS 4: filter_first -> {20,10}, examined=3 (the drain length)';
END $$;

-- (5) filter-first exactness on the 40-vs-30 construction (filter load-bearing)
DO $$ DECLARE with_f bigint; wide bigint;
BEGIN
    SELECT t.id INTO with_f FROM tjs('entities', 1, 0, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{40,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
    SELECT t.id INTO wide FROM tjs('entities', 1, 0, 1::bigint, 'id, chunk',
        '', 'embedding <-> ''{40,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
    ASSERT with_f = 30 AND wide = 40, format('filter load-bearing: got %s / %s (expected 30 / 40)', with_f, wide);
    RAISE NOTICE 'PASS 5: filter_first honors the relational filter (30 vs 40), empty filter OK';
END $$;

-- (6) SM-1/SM-3 divergence: on a selective predicate the two bodies examine materially
-- different candidate counts (the FR-6 "decision changes execution" evidence).
DO $$ DECLARE vf_ex bigint; ff_ex bigint; g1 bigint[]; g2 bigint[];
BEGIN
    SELECT array_agg(t.id) INTO g1 FROM tjs('entities', 2, 10000, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'vector_first') AS t(id bigint, chunk text);
    vf_ex := tjs_candidates_examined();
    SELECT array_agg(t.id) INTO g2 FROM tjs('entities', 2, 10000, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
    ff_ex := tjs_candidates_examined();
    ASSERT g1 = g2, format('bodies disagree: %s vs %s', g1, g2);
    ASSERT ff_ex < vf_ex, format('expected ff examined (%s) << vf examined (%s)', ff_ex, vf_ex);
    RAISE NOTICE 'PASS 6: same answers, examined vf=% ff=% (peak work diverges as FR-6 predicts)', vf_ex, ff_ex;
END $$;

-- (7) error cases: bad join_order; filter_first without a src
DO $$ BEGIN
    BEGIN
        PERFORM t.id FROM tjs('entities', 1, 0, 1::bigint, 'id, chunk', '',
            'embedding <-> ''{1,0,0,0,0,0,0,0}''', 'bogus') AS t(id bigint, chunk text);
        RAISE EXCEPTION 'bogus join_order was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs('entities', 1, 0, -1::bigint, 'id, chunk', '',
            'embedding <-> ''{1,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
        RAISE EXCEPTION 'filter_first without src was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    RAISE NOTICE 'PASS 7: bad join_order and srcless filter_first both rejected';
END $$;

-- (8) repeated same-session calls alternate bodies cleanly (SPI lifecycle both paths)
DO $$ DECLARE g bigint[];
BEGIN
    FOR i IN 1..3 LOOP
        SELECT array_agg(t.id) INTO g FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
            'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
        ASSERT g = ARRAY[20,10]::bigint[], format('iter % filter_first: %s', i, g);
        SELECT array_agg(t.id) INTO g FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
            'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'vector_first') AS t(id bigint, chunk text);
        ASSERT g = ARRAY[20,10]::bigint[], format('iter % vector_first: %s', i, g);
    END LOOP;
    RAISE NOTICE 'PASS 8: 3x alternating filter/vector calls stable (SPI lifecycle clean)';
END $$;

\echo === DEV-1290 filter-first smoke: ALL PASS ===

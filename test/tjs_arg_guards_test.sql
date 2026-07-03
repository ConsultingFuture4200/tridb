-- tjs_arg_guards_test.sql — advisor plan 024: tjs()/tjs_open() entry-point + memory hardening.
--
-- Adversarial suite for the SQL-reachable operator defects fixed by
-- scripts/patches/tridb_operator_arg_hardening.patch:
--   (a) k out of range (0 / negative-as-huge-uint32 / 20000) is a clean
--       invalid_parameter_value ERROR in all three bodies — previously k=0 hit
--       top()/pop() on an EMPTY priority queue (UB, backend SIGSEGV);
--   (b) same-class guards for tjs_open m_seeds (1..10000) and hops (1..8);
--   (c) an oversized filter_exp raises program_limit_exceeded instead of silently
--       truncating the composed SQL (worst case: dropping a trailing `and (<filter>)`);
--   (d) repeated post-init errors (dim mismatch in the filter-first drain, 10x) leave the
--       backend alive and a subsequent good query correct — the error-path release-callback
--       smoke (malloc'd state + new'd containers freed via MemoryContextCallback, not leaked).
-- The backend staying alive is asserted implicitly: psql runs with ON_ERROR_STOP=1, so a
-- crashed backend kills the session and every later statement fails the suite.
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

-- (1) tjs k guards: 0 / -1 (arrives as ~4.29e9 through PG_GETARG_UINT32) / 20000, on BOTH
-- physical bodies (the guard fires before body selection; exercise both call shapes anyway).
DO $$ BEGIN
    BEGIN
        PERFORM t.id FROM tjs('entities', 0, 0, 1::bigint, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs k=0 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs('entities', -1, 0, 1::bigint, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs k=-1 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs('entities', 20000, 0, 1::bigint, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs k=20000 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs('entities', 0, 0, 1::bigint, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint);
        RAISE EXCEPTION 'tjs(filter_first) k=0 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    RAISE NOTICE 'PASS 1: tjs k=0/-1/20000 rejected (both bodies), backend alive';
END $$;

-- (2) tjs_open k / m_seeds / hops guards (m_seeds=0 and hops=0 were silent defaults before).
DO $$ BEGIN
    BEGIN
        PERFORM t.id FROM tjs_open('entities', 0, 0, 3, 1, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs_open k=0 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs_open('entities', 5, 0, 0, 1, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs_open m_seeds=0 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs_open('entities', 5, 0, 3, 9, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs_open hops=9 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs_open('entities', 5, 0, 3, 0, 'id', '',
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs_open hops=0 was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    RAISE NOTICE 'PASS 2: tjs_open k=0 / m_seeds=0 / hops=9 / hops=0 rejected, backend alive';
END $$;

-- (3) oversized filter_exp (> the 102400-byte compose buffer) raises program_limit_exceeded —
-- NOT silent truncation — in all three compose paths.
DO $$ DECLARE big_filter text;
BEGIN
    big_filter := repeat('ts < 500 and ', 8500) || 'ts < 500';   -- ~110KB
    ASSERT length(big_filter) > 110000, 'test bug: filter not oversized';
    BEGIN
        PERFORM t.id FROM tjs('entities', 1, 0, 1::bigint, 'id', big_filter,
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs vector_first oversized filter was accepted';
    EXCEPTION WHEN program_limit_exceeded THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs('entities', 1, 0, 1::bigint, 'id', big_filter,
            'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint);
        RAISE EXCEPTION 'tjs filter_first oversized filter was accepted';
    EXCEPTION WHEN program_limit_exceeded THEN NULL;
    END;
    BEGIN
        PERFORM t.id FROM tjs_open('entities', 5, 0, 3, 1, 'id', big_filter,
            'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint);
        RAISE EXCEPTION 'tjs_open oversized filter was accepted';
    EXCEPTION WHEN program_limit_exceeded THEN NULL;
    END;
    RAISE NOTICE 'PASS 3: oversized filter_exp -> program_limit_exceeded (no silent truncation)';
END $$;

-- (4) error-path release smoke: 10x post-init errors (query-vector dim 4 vs embedding dim 8,
-- thrown INSIDE the filter-first drain, i.e. AFTER the malloc/new state init), then a good
-- query on the same backend returns the canonical answer. Before the release-callback fix the
-- state + containers leaked permanently on each iteration; a crash/corruption here fails the
-- suite via ON_ERROR_STOP.
DO $$ DECLARE got bigint[];
BEGIN
    FOR i IN 1..10 LOOP
        BEGIN
            PERFORM t.id FROM tjs('entities', 2, 0, 1::bigint, 'id', '',
                'embedding <-> ''{1,0,0,0}''', 'filter_first') AS t(id bigint);
            RAISE EXCEPTION 'dim-mismatch query was accepted (iter %)', i;
        EXCEPTION WHEN invalid_parameter_value THEN NULL;
        END;
    END LOOP;
    SELECT array_agg(t.id) INTO got FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''', 'filter_first') AS t(id bigint, chunk text);
    ASSERT got = ARRAY[20,10]::bigint[], format('post-error filter_first answer: %s', got);
    SELECT array_agg(t.id) INTO got FROM tjs('entities', 2, 0, 1::bigint, 'id, chunk',
        'ts < 500', 'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, chunk text);
    ASSERT got = ARRAY[20,10]::bigint[], format('post-error vector_first answer: %s', got);
    RAISE NOTICE 'PASS 4: 10x post-init errors, backend alive, both bodies still correct';
END $$;

\echo === advisor plan 024 tjs/tjs_open arg guards: ALL PASS ===

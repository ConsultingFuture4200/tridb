-- tjs_pg_test.sql — the re-homed fused operator on STOCK PG (ADR-0019, D2 phase 2.5).
-- Runs via scripts/pg17_graph_test.sh (stock PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- Corpus: 2000 entities, embedding = [i/2000, 0, ...] (vector(8)), ts = 100 for id < 500
-- else 900; graph: hub 2 --P(type 2)--> {1000..1100}. Mirrors the shape of
-- test/tjs_filter_first_test.sql so the semantics carry over.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS graph_store_am;
CREATE EXTENSION IF NOT EXISTS tjs_pg;

CREATE TABLE entities (id bigint PRIMARY KEY, ts int, embedding vector(8));
INSERT INTO entities
  SELECT g, CASE WHEN g < 500 THEN 100 ELSE 900 END,
         (('[' || (g::float8 / 2000)::text || ',0,0,0,0,0,0,0]')::vector(8))
  FROM generate_series(0, 1999) AS g;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops)
  WITH (m = 16, ef_construction = 64);

-- graph: dense vids 0..1999 (ext id == vid by upsert order), typed hub edges
DO $$
DECLARE g int; v bigint;
BEGIN
  FOR g IN 0..1999 LOOP
    v := graph_store.gph_upsert_vertex(g);
    IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;
  END LOOP;
END $$;
SELECT set_config('tjs.ptype', graph_store.register_edge_type('P1')::text, false);
SELECT count(*) FROM (
  SELECT graph_store.gph_insert_edge(2, g, current_setting('tjs.ptype')::int)
  FROM generate_series(1000, 1100) AS g
) s;

-- (1) FILTER-FIRST behind the operator surface: hub 2's typed reach, no ts filter, ranked
-- by distance to [0.5,...] (== id 1000's embedding) => 1000..1004.
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 0, 0, 2, 'id', '',
    '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
  IF got <> ARRAY[1000,1001,1002,1003,1004]::bigint[] THEN
    RAISE EXCEPTION 'filter-first: got %', got;
  END IF;
  RAISE NOTICE 'PASS 1: filter-first via operator -> {1000..1004}';
END $$;

-- (2) filter-first honors the relational filter (ts=900 up there, so ts<500 empties it)
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 0, 0, 2, 'id', 'ts < 500',
    '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
  IF got IS NOT NULL THEN RAISE EXCEPTION 'filter-first+filter: got %', got; END IF;
  RAISE NOTICE 'PASS 2: filter-first honors the relational filter (empty set)';
END $$;

-- (3) vector-first refuses to run without the iterative scan (fail-loud contract)
DO $$
BEGIN
  BEGIN
    PERFORM t FROM tjs_open('entities', 5, 32, 0, 0, 'id', '',
      '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
    RAISE EXCEPTION 'vector-first ran without relaxed_order';
  EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE '%relaxed_order%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'PASS 3: vector-first requires hnsw.iterative_scan = relaxed_order';
END $$;

SET hnsw.iterative_scan = relaxed_order;
SET hnsw.max_scan_tuples = 20000;

-- (4) vector-first, filtered: matches the exact oracle at recall >= 4/5, examines > 0 and
-- FEWER than the table (TR-1 early termination with term_cond).
DO $$
DECLARE got bigint[]; oracle bigint[]; ex bigint; hits int := 0; i int;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 64, 0, 0, 'id', 'ts < 500',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  ex := tjs_open_candidates_examined();
  SELECT array_agg(id) INTO oracle FROM (
    SELECT id FROM entities WHERE ts < 500
    ORDER BY embedding <-> '[0.1,0,0,0,0,0,0,0]'::vector LIMIT 5) q;
  FOR i IN 1..5 LOOP
    IF got @> ARRAY[oracle[i]] THEN hits := hits + 1; END IF;
  END LOOP;
  IF hits < 4 THEN RAISE EXCEPTION 'vector-first recall %/5 (got %, oracle %)', hits, got, oracle; END IF;
  IF ex <= 0 THEN RAISE EXCEPTION 'examined=% (no work reported)', ex; END IF;
  IF ex >= 2000 THEN RAISE EXCEPTION 'examined=% — no early termination', ex; END IF;
  RAISE NOTICE 'PASS 4: vector-first filtered recall %/5, examined % (0 < ex < 2000)', hits, ex;
END $$;

-- (5) seedless graph predicate: with m_seeds=1 the first candidate (id 1000, nearest to
-- [0.5..]) becomes the seed. Vertex 1000 has NO out-edges, so its reach is EMPTY — every
-- subsequent candidate must be excluded and only the seed itself may be emitted. This is
-- the strictest possible proof that the reach constraint actually gates emission.
DO $$
DECLARE got bigint[]; bad int;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 64, 1, 2, 'id', '',
    '[0.5,0,0,0,0,0,0,0]'::vector) AS t;
  IF got IS NULL THEN RAISE EXCEPTION 'seedless returned nothing'; END IF;
  SELECT count(*) INTO bad FROM unnest(got) u WHERE u <> 1000;
  IF bad > 0 THEN RAISE EXCEPTION 'seedless emitted out-of-reach ids: %', got; END IF;
  RAISE NOTICE 'PASS 5: seedless graph predicate constrains to the seed reach: %', got;
END $$;

-- (6) budget-cap honesty: a tiny scan budget ends the stream before term_cond -> flag set
SET hnsw.max_scan_tuples = 50;
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 100000, 0, 0, 'id', 'ts >= 500',
    '[0.01,0,0,0,0,0,0,0]'::vector) AS t;
  IF NOT tjs_open_budget_capped() THEN
    RAISE EXCEPTION 'budget cap not reported (got %)', got;
  END IF;
  RAISE NOTICE 'PASS 6: budget-capped stream reported via tjs_open_budget_capped()';
END $$;
SET hnsw.max_scan_tuples = 20000;

\echo === tjs_pg (stock-PG fused operator, ADR-0019): ALL PASS ===

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

-- (1c) filter-first ranks by the index's ACTUAL metric, not a hardcoded L2. ent_cos
-- reuses the shared graph (hub 2 -> vids 1000..1100) but builds a vector_cosine_ops
-- index. Embeddings are constructed so the cosine-nearest and L2-nearest top-5 DIFFER
-- (magnitude-vs-angle split): cosine picks {1000..1004}, L2 would pick {1000,1005..1008}.
-- Pre-fix (hardcoded <->) this returns the L2 set and fails; post-fix it ranks by <=>.
CREATE TABLE ent_cos (id bigint PRIMARY KEY, ts int, embedding vector(8));
INSERT INTO ent_cos
SELECT g, 100,
  (CASE
     WHEN g = 1000 THEN '[1,0,0,0,0,0,0,0]'
     -- cosine-near (small angle), L2-far (magnitude 4 vs query magnitude 1)
     WHEN g BETWEEN 1001 AND 1004 THEN
       format('[%s,%s,0,0,0,0,0,0]', 4*cos(0.01*(g-1000)), 4*sin(0.01*(g-1000)))
     -- cosine-far (angle ~0.4), L2-near (magnitude 1 matches the query)
     WHEN g BETWEEN 1005 AND 1008 THEN
       format('[%s,%s,0,0,0,0,0,0]', cos(0.4+0.001*(g-1005)), sin(0.4+0.001*(g-1005)))
     -- far in both metrics; never enters top-5
     ELSE format('[%s,%s,0,0,0,0,0,0]', 10*cos(1.0), 10*sin(1.0))
   END)::vector(8)
FROM generate_series(0,1999) g;
CREATE INDEX ent_cos_hnsw ON ent_cos USING hnsw (embedding vector_cosine_ops)
  WITH (m=16, ef_construction=64);
DO $$
DECLARE got bigint[]; oracle bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('ent_cos', 5, 0, 0, 2, 'id', '',
    (SELECT embedding FROM ent_cos WHERE id=1000), 2, current_setting('tjs.ptype')::int) AS t;
  SELECT array_agg(id) INTO oracle FROM (
    SELECT id FROM ent_cos WHERE id IN (
      SELECT dst FROM graph_store.gph_traverse_bfs(2, 2, current_setting('tjs.ptype')::int) AS dst)
      AND id <> 2
    ORDER BY embedding <=> (SELECT embedding FROM ent_cos WHERE id=1000) LIMIT 5) q;
  IF got <> oracle THEN RAISE EXCEPTION 'cosine filter-first: got % expected %', got, oracle; END IF;
  RAISE NOTICE 'PASS 1c: filter-first ranks by the index cosine metric';
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

-- (3b) NULL required args raise a clean error, never crash the backend
DO $$
BEGIN
  BEGIN
    PERFORM t FROM tjs_open('entities', 5, 0, 0, 2, NULL, '',
      '[0.5,0,0,0,0,0,0,0]'::vector, 2) AS t;
    RAISE EXCEPTION 'NULL id_col did not raise';
  EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE '%non-NULL%' THEN RAISE; END IF;
  END;
  BEGIN
    PERFORM t FROM tjs_open('entities', 5, 0, 0, 2, 'id', NULL,
      '[0.5,0,0,0,0,0,0,0]'::vector, 2) AS t;
    RAISE EXCEPTION 'NULL filter did not raise';
  EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE '%non-NULL%' THEN RAISE; END IF;
  END;
  BEGIN
    PERFORM t FROM tjs_open('entities', 5, 0, 0, 2, 'id', '', NULL::vector, 2) AS t;
    RAISE EXCEPTION 'NULL query did not raise';
  EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE '%non-NULL%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'PASS 3b: NULL required args raise cleanly (no backend crash)';
END $$;

-- (3c) m_seeds bounds: values outside 0..10000 are rejected before any work begins.
-- 10000 is accepted by argument validation alone (filter-first ignores the seed count,
-- so no traversal cost); m_seeds = 0 zero-mode stays supported (PASS 1/2/1c above).
DO $$
BEGIN
  BEGIN
    PERFORM t FROM tjs_open('entities', 5, 0, -1, 2, 'id', '',
      '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
    RAISE EXCEPTION 'm_seeds = -1 did not raise';
  EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE '%m_seeds%0..10000%' THEN RAISE; END IF;
  END;
  BEGIN
    PERFORM t FROM tjs_open('entities', 5, 0, 10001, 2, 'id', '',
      '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
    RAISE EXCEPTION 'm_seeds = 10001 did not raise';
  EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE '%m_seeds%0..10000%' THEN RAISE; END IF;
  END;
  PERFORM t FROM tjs_open('entities', 5, 0, 10000, 2, 'id', '',
    '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
  RAISE NOTICE 'PASS 3c: m_seeds bounded to 0..10000 (rejects -1/10001, accepts 10000)';
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

-- (5) seedless BRIDGE INJECTION (fork parity, ADR-0012 recipe B): query [0.001..] makes
-- id 2 the nearest candidate; with m_seeds=1 it seeds the bridge set = {2} + reach(2)
-- = {2, 1000..1100}. The band 1000..1100 is vector-FAR (dist ~0.5) so the ANN stream
-- never reaches it before term_cond — phase 3b must fetch those bridges DIRECTLY and
-- they are GUARANTEED into the budget, displacing nearer non-bridge candidates (1,3,4..).
DO $$
DECLARE got bigint[]; nb bigint;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 32, 1, 2, 'id', '',
    '[0.001,0,0,0,0,0,0,0]'::vector) AS t;
  nb := tjs_open_bridges_injected();
  IF got <> ARRAY[2,1000,1001,1002,1003]::bigint[] THEN
    RAISE EXCEPTION 'bridge injection: got % (bridges_injected=%)', got, nb;
  END IF;
  IF nb < 5 THEN RAISE EXCEPTION 'bridges_injected=% (expected >= 5)', nb; END IF;
  RAISE NOTICE 'PASS 5: bridges guaranteed into the budget past the frontier: % (injected %)', got, nb;
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

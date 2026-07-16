-- tjs_pg_tr1_test.sql — plan 077 Step 1: TR-1 NEGATIVE CONTROL for the stock TJS graph leg.
-- Runs via scripts/pg17_graph_test.sh (stock PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- PURPOSE (read before "fixing" a red run): this suite characterizes the graph work the
-- stock operator does and then asserts the TR-1 bound "small k consumes strictly less
-- graph work than the full reachable set". Against the CURRENT implementation (whole-BFS
-- materialized at Open via graph_store.gph_traverse_bfs, both operator paths) the final
-- NEGATIVE CONTROL block MUST FAIL — that failure is the Step 1 evidence. It goes green
-- only when plan 077 Step 3+ lands a bounded pull-based graph leg.
--
-- Probe: graph_store.gph_visits() — per-backend edge-step counter, +1 per edge emitted by
-- gs_getnext (graph_am.c). Read as deltas. No operator C is instrumented for this test.
--
-- Corpus: 4500 entities, embedding = [i/4500, 0 x7] (monotone in id, no distance ties),
-- dense vids 0..4499. Deterministic typed graph, multi-page + multi-hop + high-degree:
--   hub A:  0 -> {1..1000}          (degree 1000; ~254 EdgeSlots per 8KB adjacency page
--                                    => the hub chain spans >= 4 adjacency pages)
--   hop 2:  g -> 1000+g, g in 1..200 (=> reach(0, 2 hops) = 1200 vertices, 1200 edge-steps)
--   hub B:  2000 -> {2001..4400}     (degree 2400 = 2x hub A's full reach, for the
--                                    work-scales-with-graph-size observation)

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS graph_store_am;
CREATE EXTENSION IF NOT EXISTS tjs_pg;

CREATE TABLE entities (id bigint PRIMARY KEY, ts int, embedding vector(8));
INSERT INTO entities
  SELECT g, CASE WHEN g < 500 THEN 100 ELSE 900 END,
         (('[' || (g::float8 / 4500)::text || ',0,0,0,0,0,0,0]')::vector(8))
  FROM generate_series(0, 4499) AS g;
CREATE INDEX entities_hnsw ON entities USING hnsw (embedding vector_l2_ops)
  WITH (m = 16, ef_construction = 64);

DO $$
DECLARE g int; v bigint;
BEGIN
  FOR g IN 0..4499 LOOP
    v := graph_store.gph_upsert_vertex(g);
    IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;
  END LOOP;
END $$;
SELECT set_config('tjs.ptype', graph_store.register_edge_type('P1')::text, false);
SELECT count(*) FROM (
  SELECT graph_store.gph_insert_edge(0, g, current_setting('tjs.ptype')::int)
  FROM generate_series(1, 1000) AS g) s;
SELECT count(*) FROM (
  SELECT graph_store.gph_insert_edge(g, 1000 + g, current_setting('tjs.ptype')::int)
  FROM generate_series(1, 200) AS g) s;
SELECT count(*) FROM (
  SELECT graph_store.gph_insert_edge(2000, g, current_setting('tjs.ptype')::int)
  FROM generate_series(2001, 4400) AS g) s;

-- Observations are stashed here so the final negative-control block can assert on ALL of
-- them at once (and report every number in one exception) instead of dying piecemeal.
CREATE TEMP TABLE tr1_obs (name text PRIMARY KEY, val bigint NOT NULL);

-- ---------------------------------------------------------------------------------------
-- (C1) Baselines: reach sizes and the full-BFS edge-step cost (the "denominator").
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int;
        v0 bigint; reach_a bigint; work_a bigint; reach_b bigint; work_b bigint;
BEGIN
  v0 := graph_store.gph_visits();
  SELECT count(*) INTO reach_a FROM graph_store.gph_traverse_bfs(0, 2, tp);
  work_a := graph_store.gph_visits() - v0;
  IF reach_a <> 1200 THEN RAISE EXCEPTION 'topology broken: reach(0)=% (expected 1200)', reach_a; END IF;

  v0 := graph_store.gph_visits();
  SELECT count(*) INTO reach_b FROM graph_store.gph_traverse_bfs(2000, 2, tp);
  work_b := graph_store.gph_visits() - v0;
  IF reach_b <> 2400 THEN RAISE EXCEPTION 'topology broken: reach(2000)=% (expected 2400)', reach_b; END IF;

  INSERT INTO tr1_obs VALUES ('reach_a', reach_a), ('full_bfs_work_a', work_a),
                             ('reach_b', reach_b), ('full_bfs_work_b', work_b);
  RAISE NOTICE 'C1 baseline: reach(hub A, 2 hops) = % (% edge-steps); reach(hub B) = % (% edge-steps)',
    reach_a, work_a, reach_b, work_b;
END $$;

-- (C2) The BFS SRF materializes at Open: a pull-position (target-list) LIMIT 1 still pays
-- the whole traversal. Contrast: the single-hop typed traversal SRF under the same LIMIT is
-- genuinely incremental (the DEV-1165 engine, one edge per Next) — proof the AM layer can
-- already pull; only the multi-hop helper and the operator on top of it materialize.
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; v0 bigint; d_bfs bigint; d_step bigint;
BEGIN
  v0 := graph_store.gph_visits();
  PERFORM graph_store.gph_traverse_bfs(0, 2, tp) LIMIT 1;
  d_bfs := graph_store.gph_visits() - v0;

  v0 := graph_store.gph_visits();
  PERFORM graph_store.gph_traverse_typed(0, tp, 0, -1) LIMIT 1;
  d_step := graph_store.gph_visits() - v0;
  IF d_step <> 1 THEN
    RAISE EXCEPTION 'contrast probe broken: gph_traverse_typed LIMIT 1 did % edge-steps (expected 1)', d_step;
  END IF;

  INSERT INTO tr1_obs VALUES ('bfs_limit1_work', d_bfs), ('single_hop_limit1_work', d_step);
  RAISE NOTICE 'C2 SRF: gph_traverse_bfs LIMIT 1 = % edge-steps (whole reach); gph_traverse_typed LIMIT 1 = % edge-step (pull works at the AM layer)',
    d_bfs, d_step;
END $$;

-- (C3) FILTER-FIRST at k=1: the operator asks for ONE row anchored at hub A, but the fused
-- statement runs gph_traverse_bfs in a FROM clause — the entire 1200-vertex reach is walked
-- and joined before the LIMIT 1 applies. Same at hub B: 2x the reach => 2x the graph work
-- at identical k (work scales with |reach|, not with k — the TR-1 violation).
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; v0 bigint; d_a bigint; d_b bigint; got bigint;
BEGIN
  v0 := graph_store.gph_visits();
  SELECT t INTO got FROM tjs_open('entities', 1, 0, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 0, tp) AS t;
  d_a := graph_store.gph_visits() - v0;
  IF got <> 1 THEN RAISE EXCEPTION 'filter-first k=1 (hub A): got % (expected 1, the nearest reach member)', got; END IF;

  v0 := graph_store.gph_visits();
  SELECT t INTO got FROM tjs_open('entities', 1, 0, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 2000, tp) AS t;
  d_b := graph_store.gph_visits() - v0;
  IF got <> 2001 THEN RAISE EXCEPTION 'filter-first k=1 (hub B): got % (expected 2001)', got; END IF;

  INSERT INTO tr1_obs VALUES ('ff_k1_work_a', d_a), ('ff_k1_work_b', d_b);
  RAISE NOTICE 'C3 filter-first k=1: hub A graph work = % edge-steps (reach 1200); hub B = % (reach 2400); scale ratio = %',
    d_a, d_b, round(d_b::numeric / d_a, 2);
END $$;

-- (C4) SEEDLESS at m_seeds=1: query [0,..] makes id 0 the unique nearest candidate, so the
-- 33-candidate seed window seeds from vertex 0 and reach_add_from_seed runs the FULL BFS
-- (1200 edge-steps) and copies every id into the reach hash; phase 3b then direct-fetches
-- every never-streamed reach member by id (visible in tjs_open_candidates_examined).
SET hnsw.iterative_scan = relaxed_order;
SET hnsw.max_scan_tuples = 20000;
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; v0 bigint; d bigint; ex bigint;
BEGIN
  v0 := graph_store.gph_visits();
  PERFORM t FROM tjs_open('entities', 5, 32, 1, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector) AS t;
  d := graph_store.gph_visits() - v0;
  ex := tjs_open_candidates_examined();
  INSERT INTO tr1_obs VALUES ('seedless_work', d), ('seedless_examined', ex);
  RAISE NOTICE 'C4 seedless m_seeds=1, k=5: graph work = % edge-steps (seed reach 1200); candidates examined = %',
    d, ex;
END $$;

-- ---------------------------------------------------------------------------------------
-- NEGATIVE CONTROL (the Step 1 gate): TR-1 requires that a k=1 / LIMIT 1 consumer pays
-- STRICTLY LESS graph work than the full reachable set. Every violated bound is listed in
-- one exception. EXPECTED TO FAIL against the current whole-BFS implementation; a PASS
-- here before any Step 3+ implementation means the test is not observing graph work.
DO $$
DECLARE full_a bigint; ff_a bigint; ff_b bigint; bfs1 bigint; sl bigint; reach_a bigint;
        viol text := '';
BEGIN
  SELECT val INTO reach_a FROM tr1_obs WHERE name = 'reach_a';
  SELECT val INTO full_a  FROM tr1_obs WHERE name = 'full_bfs_work_a';
  SELECT val INTO ff_a    FROM tr1_obs WHERE name = 'ff_k1_work_a';
  SELECT val INTO ff_b    FROM tr1_obs WHERE name = 'ff_k1_work_b';
  SELECT val INTO bfs1    FROM tr1_obs WHERE name = 'bfs_limit1_work';
  SELECT val INTO sl      FROM tr1_obs WHERE name = 'seedless_work';

  IF ff_a >= full_a THEN
    viol := viol || format(' [filter-first k=1 graph work %s >= full-reach work %s]', ff_a, full_a);
  END IF;
  IF sl >= full_a THEN
    viol := viol || format(' [seedless m_seeds=1 graph work %s >= full-reach work %s]', sl, full_a);
  END IF;
  IF bfs1 >= reach_a THEN
    viol := viol || format(' [gph_traverse_bfs LIMIT 1 did %s edge-steps >= reach %s]', bfs1, reach_a);
  END IF;

  IF viol <> '' THEN
    RAISE EXCEPTION 'TR-1 NEGATIVE CONTROL FAILED (expected pre-fix): k=1/LIMIT 1 must consume strictly less than the reachable graph.% Scaling: hub B k=1 graph work % vs hub A % = %x at identical k.',
      viol, ff_b, ff_a, round(ff_b::numeric / ff_a, 2);
  END IF;
  RAISE NOTICE 'PASS negative control: bounded graph leg — k=1 work (ff=%, seedless=%, bfs LIMIT 1=%) < full reach work %',
    ff_a, sl, bfs1, full_a;
END $$;

\echo '============ tjs_pg TR-1 bounded graph leg (plan 077): ALL TESTS PASSED ============'

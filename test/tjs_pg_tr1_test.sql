-- tjs_pg_tr1_test.sql — plan 077 Step 1 NEGATIVE CONTROL, FLIPPED GREEN by Step 3-4
-- (ADR-0020: bounded pull-based graph leg). Runs via scripts/pg17_graph_test.sh (stock
-- PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- HISTORY: against the pre-077 whole-BFS implementation (graph_store.gph_traverse_bfs, both
-- operator paths) the negative-control block at the bottom of this file FAILED by design —
-- that failure was the Step 1 evidence a bounded graph leg was missing. Step 3 added
-- graph_store.gph_traverse_bounded (a genuine Open/Next/Close pull iterator, budget-bounded);
-- Step 4 rewired both tjs_open physical paths onto it. This file now asserts the POSITIVE
-- contract instead: filter-first's opt-in early termination (term_cond > 0, ADR-0007's
-- consecutive-drops rule reused) and the tjs.graph_work_budget cap (both paths) actually
-- bound graph work independent of |V|/|E| — while term_cond = 0 (every PRE-077 caller's
-- value, unchanged in test/tjs_pg_test.sql) stays byte-identical to the old full-reach
-- contract. See docs/decisions/0020-stock-tjs-incremental-graph-leg.md.
--
-- Probe: graph_store.gph_visits() — per-backend edge-step counter, +1 per edge emitted by
-- gs_getnext (graph_am.c). Read as deltas. tjs_open_graph_examined()/tjs_open_graph_censored()
-- (plan 077) probe the SAME quantity at the operator level.
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

-- Observations are stashed here so the final gate block can assert on ALL of them at once
-- (and report every number in one exception) instead of dying piecemeal.
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

-- (C2) INFORMATIONAL ONLY (not part of the gate below): gph_traverse_bfs remains the
-- documented materializing TEST/ORACLE helper (ADR-0020 decision 4, deliberately unchanged
-- and banned from the operator path by the Step 5 static guard) — a pull-position LIMIT 1
-- on it still pays the whole traversal. Contrast: gph_traverse_bounded's LIMIT 1 (the
-- production iterator, Step 3) is genuinely incremental, same as the single-hop
-- gph_traverse_typed underneath it.
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int;
        v0 bigint; d_bfs bigint; d_bounded bigint; d_step bigint;
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

  v0 := graph_store.gph_visits();
  PERFORM graph_store.gph_traverse_bounded(0, 2, tp, 65536) LIMIT 1;
  d_bounded := graph_store.gph_visits() - v0;
  IF d_bounded <> 1 THEN
    RAISE EXCEPTION 'gph_traverse_bounded LIMIT 1 did % edge-steps (expected 1 -- the Step 3 iterator '
      'must cost the same as the single-hop primitive under LIMIT 1)', d_bounded;
  END IF;

  INSERT INTO tr1_obs VALUES ('bfs_limit1_work', d_bfs), ('bounded_limit1_work', d_bounded);
  RAISE NOTICE 'C2 SRF (informational): gph_traverse_bfs LIMIT 1 = % edge-steps (whole reach, '
    'unchanged oracle helper); gph_traverse_bounded LIMIT 1 = % edge-step (the production '
    'iterator pulls incrementally, same cost as the single-hop primitive)', d_bfs, d_bounded;
END $$;

-- (C3) FILTER-FIRST, term_cond = 0 (every pre-077 caller's value): UNCHANGED contract.
-- Disabling early termination means the WHOLE bounded reach is examined and ranked, exactly
-- as the old fused statement did -- an uncensored call is byte-identical to the pre-077
-- contract (ADR-0020 §3). This is the "nothing broke" half of the story.
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; v0 bigint; got bigint;
BEGIN
  v0 := graph_store.gph_visits();
  SELECT t INTO got FROM tjs_open('entities', 1, 0, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 0, tp) AS t;
  IF got <> 1 THEN RAISE EXCEPTION 'term_cond=0 filter-first (hub A): got % (expected 1)', got; END IF;
  IF graph_store.gph_visits() - v0 <> 1200 THEN
    RAISE EXCEPTION 'term_cond=0 must examine the FULL reach (1200), got %',
      graph_store.gph_visits() - v0;
  END IF;
  IF tjs_open_graph_censored() IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'term_cond=0, default budget: expected uncensored, got censored=%',
      tjs_open_graph_censored();
  END IF;
  RAISE NOTICE 'C3 term_cond=0: filter-first (hub A) examines the full reach (1200), uncensored '
    '-- byte-identical to the pre-077 contract';
END $$;

-- (C3b) FILTER-FIRST, term_cond = 8 (opt-in, ADR-0007's consecutive-drops rule reused for the
-- graph leg): k=1 at hub A must now consume STRICTLY LESS graph work than the full reachable
-- set, AND hub B (2x hub A's reach) must show FLAT work -- independent of graph size, not the
-- old linear scaling. Query [0,...] is monotone-nearest-by-id, so the true nearest reach
-- member is the FIRST one BFS visits; the run of farther candidates that follow are drops.
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; v0 bigint; got bigint; ff_a bigint; ff_b bigint;
BEGIN
  v0 := graph_store.gph_visits();
  SELECT t INTO got FROM tjs_open('entities', 1, 8, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 0, tp) AS t;
  ff_a := graph_store.gph_visits() - v0;
  IF got <> 1 THEN RAISE EXCEPTION 'term_cond=8 filter-first (hub A): got % (expected 1)', got; END IF;

  v0 := graph_store.gph_visits();
  SELECT t INTO got FROM tjs_open('entities', 1, 8, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 2000, tp) AS t;
  ff_b := graph_store.gph_visits() - v0;
  IF got <> 2001 THEN RAISE EXCEPTION 'term_cond=8 filter-first (hub B): got % (expected 2001)', got; END IF;

  INSERT INTO tr1_obs VALUES ('ff_k1_tc8_work_a', ff_a), ('ff_k1_tc8_work_b', ff_b);
  RAISE NOTICE 'C3b term_cond=8: hub A graph work = % edge-steps (reach 1200); hub B = % '
    '(reach 2400) -- flat, not the old 2.00x scale', ff_a, ff_b;
END $$;

-- (C4) SEEDLESS at m_seeds=1, default budget, term_cond=32 (vector-side; unrelated to the
-- graph leg): the reach is the SEED'S FULL union-of-out-reach by construction (bridge
-- injection needs full membership, unlike filter-first's rank-early-stop), so at a default
-- budget far above the reach (1200) this is UNCHANGED from the pre-077 contract: uncensored,
-- full reach examined. The TR-1 win for seedless is the BUDGET bound (C4b), not term_cond.
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
  IF d <> 1200 THEN
    RAISE EXCEPTION 'seedless default-budget graph work=% (expected 1200 -- the full seed reach, '
      'uncensored, byte-identical to the pre-077 contract)', d;
  END IF;
  IF tjs_open_graph_censored() IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'seedless default-budget: expected uncensored, got censored=%',
      tjs_open_graph_censored();
  END IF;
  INSERT INTO tr1_obs VALUES ('seedless_default_work', d), ('seedless_examined', ex);
  RAISE NOTICE 'C4 seedless m_seeds=1, k=5, default budget: graph work = % edge-steps (full '
    'seed reach 1200, uncensored); candidates examined = %', d, ex;
END $$;

-- (C4b) SEEDLESS bounded by a SMALL tjs.graph_work_budget: the graph leg now costs EXACTLY
-- the budget (strictly less than the 1200 full reach), disclosed via
-- tjs_open_graph_censored() = true and tjs_open_graph_examined() = the budget -- the honest,
-- bounded-independent-of-graph-size contract (ADR-0020 §2/§3), never silently exact.
SET tjs.graph_work_budget = 300;
DO $$
DECLARE v0 bigint; d bigint;
BEGIN
  v0 := graph_store.gph_visits();
  PERFORM t FROM tjs_open('entities', 5, 32, 1, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector) AS t;
  d := graph_store.gph_visits() - v0;
  IF d <> 300 THEN
    RAISE EXCEPTION 'seedless budget=300: graph work=% (expected exactly 300)', d;
  END IF;
  IF tjs_open_graph_examined() <> 300 THEN
    RAISE EXCEPTION 'tjs_open_graph_examined()=% (expected 300)', tjs_open_graph_examined();
  END IF;
  IF tjs_open_graph_censored() IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'seedless budget=300 (<< reach 1200): expected censored=true, got %',
      tjs_open_graph_censored();
  END IF;
  INSERT INTO tr1_obs VALUES ('seedless_bounded_work', d);
  RAISE NOTICE 'C4b seedless budget=300: graph work = % edge-steps (exactly the budget, << the '
    '1200 full reach), censored=true, examined()=300', d;
END $$;
RESET tjs.graph_work_budget;
SET hnsw.max_scan_tuples = 20000;

-- (C5) CENSORING HONESTY, FILTER-FIRST: a budget below the reach stops at a deterministic
-- prefix (depth-ascending, adjacency-slot order) and discloses it -- never silently exact.
SET tjs.graph_work_budget = 128;
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 0, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 0, tp) AS t;
  IF got <> ARRAY[1,2,3,4,5]::bigint[] THEN
    RAISE EXCEPTION 'censored filter-first: got % (expected deterministic prefix top-5 {1..5})', got;
  END IF;
  IF tjs_open_graph_examined() <> 128 THEN
    RAISE EXCEPTION 'tjs_open_graph_examined()=% (expected 128, the budget)', tjs_open_graph_examined();
  END IF;
  IF tjs_open_graph_censored() IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'budget=128 (<< reach 1200): expected censored=true, got %',
      tjs_open_graph_censored();
  END IF;
  RAISE NOTICE 'C5 censored filter-first: budget=128 -> deterministic prefix top-5=%, '
    'examined=128, censored=true', got;
END $$;
RESET tjs.graph_work_budget;

-- (C6) CENSOR-FLAG RESET: a fresh uncensored call in the SAME backend must fully overwrite
-- tjs_open_graph_censored()/tjs_open_graph_examined() -- no leak from the (C5) censored call.
DO $$
DECLARE tp int := current_setting('tjs.ptype')::int; got bigint;
BEGIN
  SELECT t INTO got FROM tjs_open('entities', 1, 0, 0, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector, 0, tp) AS t;
  IF got <> 1 THEN RAISE EXCEPTION 'reset-check call: got % (expected 1)', got; END IF;
  IF tjs_open_graph_examined() <> 1200 THEN
    RAISE EXCEPTION 'graph_examined()=% after a full uncensored drain (expected 1200 -- no leak '
      'from the prior censored call)', tjs_open_graph_examined();
  END IF;
  IF tjs_open_graph_censored() IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'graph_censored()=% (expected false -- no leak from the prior censored call)',
      tjs_open_graph_censored();
  END IF;
  RAISE NOTICE 'C6 PASS: graph_examined/graph_censored reset per call -- no leak from (C5)';
END $$;

-- (C7) EARLY-ABANDON / LIMIT LEAK CHECK: pulling gph_traverse_bounded through a LIMIT that
-- stops mid-hub (hub A has 1000 direct out-edges; LIMIT 3 abandons the scan after 3) must not
-- warn about a leaked relcache reference (the plan 049/061 trap this file's own iterator is
-- built to avoid -- see graph_am.c's gph_traverse_bounded comment). ON_ERROR_STOP does not
-- catch a WARNING, so this asserts on the RESULT (correct prefix, correct edge-step count)
-- as the reachable proxy; the harness driving this suite also greps server logs separately
-- (scripts/pg17_graph_test.sh) where a relcache leak WARNING would surface.
DO $$
DECLARE v0 bigint; got bigint[]; d bigint;
BEGIN
  v0 := graph_store.gph_visits();
  SELECT array_agg(t) INTO got FROM (
    SELECT graph_store.gph_traverse_bounded(0, 2, 0, 65536) AS t LIMIT 3) sub;
  d := graph_store.gph_visits() - v0;
  IF got <> ARRAY[1,2,3]::bigint[] THEN
    RAISE EXCEPTION 'early-abandon LIMIT 3: got % (expected {1,2,3})', got;
  END IF;
  IF d <> 3 THEN RAISE EXCEPTION 'early-abandon LIMIT 3: graph work=% (expected 3)', d; END IF;
  -- prove the backend is still healthy after the abandoned scan (no error-in-abort, no
  -- refcount underflow): a trivial follow-up query in the SAME session.
  PERFORM 1;
  RAISE NOTICE 'C7 PASS: LIMIT 3 mid-hub abandon -> got=%, graph work=3, backend healthy after', got;
END $$;

-- ---------------------------------------------------------------------------------------
-- THE GATE (flipped from the Step 1 negative control): every bound below MUST hold against
-- the bounded pull-based graph leg (ADR-0020). Every violated bound is listed in one
-- exception, in the same style as the original Step 1 negative control.
DO $$
DECLARE full_a bigint; ff_a bigint; ff_b bigint; sl_bounded bigint; reach_a bigint;
        viol text := '';
BEGIN
  SELECT val INTO reach_a    FROM tr1_obs WHERE name = 'reach_a';
  SELECT val INTO full_a     FROM tr1_obs WHERE name = 'full_bfs_work_a';
  SELECT val INTO ff_a       FROM tr1_obs WHERE name = 'ff_k1_tc8_work_a';
  SELECT val INTO ff_b       FROM tr1_obs WHERE name = 'ff_k1_tc8_work_b';
  SELECT val INTO sl_bounded FROM tr1_obs WHERE name = 'seedless_bounded_work';

  IF ff_a >= full_a THEN
    viol := viol || format(' [filter-first term_cond=8 k=1 graph work %s >= full-reach work %s]', ff_a, full_a);
  END IF;
  IF ff_b > ff_a * 2 THEN
    viol := viol || format(' [filter-first hub B work %s is NOT flat vs hub A work %s (2x reach, >2x work)]', ff_b, ff_a);
  END IF;
  IF sl_bounded >= full_a THEN
    viol := viol || format(' [seedless budget=300 graph work %s >= full-reach work %s]', sl_bounded, full_a);
  END IF;

  IF viol <> '' THEN
    RAISE EXCEPTION 'TR-1 GATE FAILED: the bounded pull-based graph leg must consume strictly '
      'less work than the full reachable set once bounded (term_cond or budget) and stay flat '
      'across graph size.%', viol;
  END IF;
  RAISE NOTICE 'PASS TR-1 gate: bounded graph leg -- filter-first term_cond=8 work (hub A=%, hub '
    'B=%) << full reach %; seedless budget=300 work=% << full reach %',
    ff_a, ff_b, full_a, sl_bounded, full_a;
END $$;

-- (C8) SEEDLESS budget-sharing, nearest-seed-first (ADR-0020 decision 2): with m_seeds=2 the
-- SAME shared budget=300 is consumed by BOTH seeds in nearest-first order -- the first
-- (nearest) seed's pull can spend up to the whole 300, leaving the second seed whatever
-- remains (possibly zero). Total graph work across both seeds must still equal exactly the
-- shared budget (not 2x it) -- proof the pool is SHARED, not doubled.
SET tjs.graph_work_budget = 300;
DO $$
DECLARE v0 bigint; d bigint;
BEGIN
  v0 := graph_store.gph_visits();
  PERFORM t FROM tjs_open('entities', 5, 32, 2, 2, 'id', '',
    '[0,0,0,0,0,0,0,0]'::vector) AS t;
  d := graph_store.gph_visits() - v0;
  IF d <> 300 THEN
    RAISE EXCEPTION 'seedless m_seeds=2, shared budget=300: total graph work=% (expected exactly '
      '300 -- the pool is SHARED across seeds, not one budget per seed)', d;
  END IF;
  RAISE NOTICE 'C8 PASS: seedless m_seeds=2 shares ONE budget=300 across both seeds (total work=%)', d;
END $$;
RESET tjs.graph_work_budget;
SET hnsw.iterative_scan = DEFAULT;
SET hnsw.max_scan_tuples = DEFAULT;

-- (C9) graph_query() CANONICAL PATH (ADR-0020 decision 5, deferred lowering): the lowering
-- itself is NOT changed to surface tjs_open_graph_censored() alongside last_join_order(), but
-- the function stays directly callable after a graph_query() call in the SAME backend --
-- it is backend-local state (tjs_open_pg's own static counters), not lowering plumbing.
-- graph_store.graph_query()'s v1 lowering is PINNED to a table literally named "entities"
-- (ADR-0008/plan 075), so this reuses the suite's own `entities` table rather than a second
-- fixture: add the `chunk` projection column the canonical COLUMNS clause needs, and one
-- `related_to`-typed edge (the canonical label, distinct from this file's P1 typed graph)
-- between two vertices untouched by the hub A/B fixtures above.
ALTER TABLE entities ADD COLUMN chunk text;
UPDATE entities SET chunk = 'chunk ' || id WHERE id IN (3000, 3001, 3002);
SELECT graph_store.add_edge(3000, 3001);
SELECT graph_store.add_edge(3000, 3002);
DO $$
DECLARE got text[]; censored boolean;
BEGIN
  SELECT array_agg(c ORDER BY ord) INTO got
  FROM graph_store.graph_query($q$
      SELECT chunk
      FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
        COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
      WHERE src.id = 3000 AND timestamp IN (900)
      ORDER BY src_embedding <-> '{0.6669,0,0,0,0,0,0,0}'
      LIMIT 2
  $q$) WITH ORDINALITY AS t(c, ord);
  IF got <> ARRAY['chunk 3001','chunk 3002'] THEN
    RAISE EXCEPTION 'canonical query: got % (expected {chunk 3001,chunk 3002})', got;
  END IF;
  -- decision 5: this call must not error, and must return a real (non-NULL) boolean.
  censored := tjs_open_graph_censored();
  IF censored IS NULL THEN
    RAISE EXCEPTION 'tjs_open_graph_censored() returned NULL after a graph_query() call '
      '(expected a real boolean -- it is backend-local state, not lowering plumbing)';
  END IF;
  IF censored IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'tjs_open_graph_censored() after an uncapped canonical query = % (expected false)', censored;
  END IF;
  RAISE NOTICE 'C9 PASS: graph_query() -> %, tjs_open_graph_censored() callable after = %', got, censored;
END $$;

\echo '============ tjs_pg TR-1 bounded graph leg (plan 077): ALL TESTS PASSED ============'

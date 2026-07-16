-- tjs_pg_test.sql — the re-homed fused operator on STOCK PG (ADR-0019, D2 phase 2.5).
-- Runs via scripts/pg17_graph_test.sh (stock PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- Corpus: 2000 entities, embedding = [i/2000, 0, ...] (vector(8)), ts = 100 for id < 500
-- else 900; graph: hub 2 --P(type 2)--> {1000..1100}. Mirrors the shape of
-- test/tjs_filter_first_test.sql so the semantics carry over. Tests 8-10 (plan 087)
-- add P2 bridge-dense hubs 900 --> {901..940} and 500 --> {400..600}\{500}.

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
-- by distance to [0.5,...] (== id 1000's embedding) => 1000..1004. The reach has 101
-- qualifying rows (1000..1100): examined must report the FULL qualifying count, not k
-- (plan 074 — a counter capped at k carries no information). Reason is 'filter_first',
-- capped is false (single fused statement; no candidate stream, no budget in play).
DO $$
DECLARE got bigint[]; ex bigint; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 0, 0, 2, 'id', '',
    '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
  ex := tjs_open_candidates_examined();
  IF got <> ARRAY[1000,1001,1002,1003,1004]::bigint[] THEN
    RAISE EXCEPTION 'filter-first: got %', got;
  END IF;
  IF ex <> 101 THEN
    RAISE EXCEPTION 'filter-first examined=% (expected 101 qualifying rows, not LIMIT k)', ex;
  END IF;
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF reason <> 'filter_first' THEN
    RAISE EXCEPTION 'filter-first reason=% (expected filter_first)', reason;
  END IF;
  IF capped IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'filter-first capped=% (expected false)', capped;
  END IF;
  RAISE NOTICE 'PASS 1: filter-first -> {1000..1004}, examined=101 (> k), reason=filter_first';
END $$;

-- (2) filter-first honors the relational filter (ts=900 up there, so ts<500 empties it).
-- Zero qualifying rows: examined = 0 (no window row exists to carry the count — the
-- zero case must be handled explicitly), reason 'filter_first', capped false.
DO $$
DECLARE got bigint[]; ex bigint; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 0, 0, 2, 'id', 'ts < 500',
    '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF got IS NOT NULL THEN RAISE EXCEPTION 'filter-first+filter: got %', got; END IF;
  IF ex <> 0 THEN RAISE EXCEPTION 'empty filter-first examined=% (expected 0)', ex; END IF;
  IF reason <> 'filter_first' THEN
    RAISE EXCEPTION 'empty filter-first reason=% (expected filter_first)', reason;
  END IF;
  IF capped IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'empty filter-first capped=% (expected false)', capped;
  END IF;
  RAISE NOTICE 'PASS 2: filter-first honors the relational filter (empty set, examined=0)';
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
-- FEWER than the table (TR-1 early termination with term_cond). term_cond fired the stop,
-- so reason is 'term_cond' and capped is false — a known non-budget ending.
DO $$
DECLARE got bigint[]; oracle bigint[]; ex bigint; hits int := 0; i int;
        reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 64, 0, 0, 'id', 'ts < 500',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  SELECT array_agg(id) INTO oracle FROM (
    SELECT id FROM entities WHERE ts < 500
    ORDER BY embedding <-> '[0.1,0,0,0,0,0,0,0]'::vector LIMIT 5) q;
  FOR i IN 1..5 LOOP
    IF got @> ARRAY[oracle[i]] THEN hits := hits + 1; END IF;
  END LOOP;
  IF hits < 4 THEN RAISE EXCEPTION 'vector-first recall %/5 (got %, oracle %)', hits, got, oracle; END IF;
  IF ex <= 0 THEN RAISE EXCEPTION 'examined=% (no work reported)', ex; END IF;
  IF ex >= 2000 THEN RAISE EXCEPTION 'examined=% — no early termination', ex; END IF;
  IF reason <> 'term_cond' THEN
    RAISE EXCEPTION 'term_cond stop reason=% (expected term_cond)', reason;
  END IF;
  IF capped IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'term_cond stop capped=% (expected false)', capped;
  END IF;
  RAISE NOTICE 'PASS 4: vector-first filtered recall %/5, examined % (0 < ex < 2000), reason=term_cond', hits, ex;
END $$;

-- (5) seedless BRIDGE INJECTION (fork parity, ADR-0012 recipe B + plan 087): query
-- [0.0011..] (~ id 2.2, no distance ties) makes id 2 the nearest candidate; with
-- m_seeds=1 the 33-candidate seed window buffers the near ids and the NEAREST one (2)
-- seeds the bridge set = {2} + reach(2) = {2, 1000..1100}. The band 1000..1100 is
-- vector-FAR (dist ~0.5) so the ANN stream never reaches it before term_cond — phase 3b
-- must fetch those bridges DIRECTLY and they are GUARANTEED into the budget, but the
-- bridge share is CAPPED at k/2 = 2 (fork rule, plan 087): {2, 1000} take the bridge
-- slots, the vector winners 3,1,4 keep the rest. bridges_injected counts every
-- filter-passing reach member offered to the bridge budget exactly once (the fork's
-- meaning): |{2} u {1000..1100}| = 102.
DO $$
DECLARE got bigint[]; nb bigint;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 32, 1, 2, 'id', '',
    '[0.0011,0,0,0,0,0,0,0]'::vector) AS t;
  nb := tjs_open_bridges_injected();
  IF got <> ARRAY[2,3,1,4,1000]::bigint[] THEN
    RAISE EXCEPTION 'bridge injection: got % (bridges_injected=%)', got, nb;
  END IF;
  IF nb <> 102 THEN RAISE EXCEPTION 'bridges_injected=% (expected 102 = |reach|)', nb; END IF;
  RAISE NOTICE 'PASS 5: bridges guaranteed past the frontier, capped at k/2: % (injected %)', got, nb;
END $$;

-- (6) censored-ending honesty (plan 074): a tiny scan budget ends the stream before
-- term_cond fires. pgvector does NOT disclose whether hnsw.max_scan_tuples or natural
-- index exhaustion ended its stream, so the operator must NOT claim 'budget-capped':
-- reason is 'stream_end_unknown' and the compat boolean is SQL NULL (right-censored).
SET hnsw.max_scan_tuples = 50;
DO $$
DECLARE got bigint[]; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 100000, 0, 0, 'id', 'ts >= 500',
    '[0.01,0,0,0,0,0,0,0]'::vector) AS t;
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF reason <> 'stream_end_unknown' THEN
    RAISE EXCEPTION 'budgeted stream end reason=% (expected stream_end_unknown, got %)', reason, got;
  END IF;
  IF capped IS NOT NULL THEN
    RAISE EXCEPTION 'budgeted stream end capped=% (expected SQL NULL — no observable budget signal)', capped;
  END IF;
  RAISE NOTICE 'PASS 6: possibly-capped stream end reported as stream_end_unknown / NULL';
END $$;
SET hnsw.max_scan_tuples = 20000;

-- (6b) natural-exhaustion negative case: budget (20000) far exceeds the table (2000) and
-- term_cond never fires (huge), so the stream ends by plain index exhaustion. The old
-- contract labeled this 'budget-capped' — a lie. Same censored reporting as (6): pgvector
-- cannot distinguish the two endings, so reason 'stream_end_unknown', boolean SQL NULL —
-- never true without a real upstream signal.
DO $$
DECLARE got bigint[]; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 1000000, 0, 0, 'id', 'ts >= 500',
    '[0.01,0,0,0,0,0,0,0]'::vector) AS t;
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF reason <> 'stream_end_unknown' THEN
    RAISE EXCEPTION 'natural exhaustion reason=% (expected stream_end_unknown, got %)', reason, got;
  END IF;
  IF capped IS NOT NULL THEN
    RAISE EXCEPTION 'natural exhaustion capped=% (expected SQL NULL, never true)', capped;
  END IF;
  RAISE NOTICE 'PASS 6b: natural index exhaustion no longer misreported as budget-capped';
END $$;

-- (7) per-call metric reset: after (6b) left reason=stream_end_unknown/examined>0, a
-- fresh filter-first call in the SAME backend must fully overwrite all three metrics —
-- no state leaks between consecutive calls.
DO $$
DECLARE got bigint[]; ex bigint; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 0, 0, 2, 'id', 'ts < 500',
    '[0.5,0,0,0,0,0,0,0]'::vector, 2, current_setting('tjs.ptype')::int) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF ex <> 0 OR reason <> 'filter_first' OR capped IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'metric leak across calls: examined=%, reason=%, capped=%', ex, reason, capped;
  END IF;
  RAISE NOTICE 'PASS 7: per-call metrics reset — no leak from the previous call';
END $$;

-- (8) BRIDGE CAP (plan 087 fork parity): bridge-DENSE fixture — hub 900 --P2--> {901..940}
-- sits ON the query's near band (q = [0.44990,..] ~ id 899.8, no ties), so the pre-087
-- "bridges first, uncapped" finalize would hand ALL k slots to bridges {900,901,902,...}
-- and silently delete the vector modality. The fork caps the reserved bridge share at
-- k/2, min 1 when any bridge exists (patch: bridge_cap = k / 2; min-1 rule):
--   k=5 -> cap 2: bridges {900,1st-nearest 901} + vector winners {899,898,902}
--   k=3 -> cap 1: [900,899,901]   k=2 -> cap 1: [900,899]
--   k=1 -> cap 0 -> min 1: the nearest bridge (= the seed = the global nearest) holds
--          the only slot. (With m_seeds >= 1 the seed is always the nearest candidate
--          AND a bridge, so k=1 locks the min-1 outcome rather than discriminating it.)
SELECT set_config('tjs.ptype2', graph_store.register_edge_type('P2')::text, false);
SELECT count(*) FROM (
  SELECT graph_store.gph_insert_edge(900, g, current_setting('tjs.ptype2')::int)
  FROM generate_series(901, 940) AS g
) s;
DO $$
DECLARE got bigint[];
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 32, 1, 2, 'id', '',
    '[0.44990,0,0,0,0,0,0,0]'::vector, NULL, current_setting('tjs.ptype2')::int) AS t;
  IF got <> ARRAY[900,899,901,898,902]::bigint[] THEN
    RAISE EXCEPTION 'bridge cap k=5: got % (expected [900,899,901,898,902])', got;
  END IF;
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 3, 32, 1, 2, 'id', '',
    '[0.44990,0,0,0,0,0,0,0]'::vector, NULL, current_setting('tjs.ptype2')::int) AS t;
  IF got <> ARRAY[900,899,901]::bigint[] THEN
    RAISE EXCEPTION 'bridge cap k=3: got % (expected [900,899,901])', got;
  END IF;
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 2, 32, 1, 2, 'id', '',
    '[0.44990,0,0,0,0,0,0,0]'::vector, NULL, current_setting('tjs.ptype2')::int) AS t;
  IF got <> ARRAY[900,899]::bigint[] THEN
    RAISE EXCEPTION 'bridge cap k=2: got % (expected [900,899])', got;
  END IF;
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 1, 32, 1, 2, 'id', '',
    '[0.44990,0,0,0,0,0,0,0]'::vector, NULL, current_setting('tjs.ptype2')::int) AS t;
  IF got <> ARRAY[900]::bigint[] THEN
    RAISE EXCEPTION 'bridge cap k=1 (min-1 rule): got % (expected [900])', got;
  END IF;
  RAISE NOTICE 'PASS 8: bridge share capped at k/2 (min 1) — k=5/3/2/1 match the fork rule';
END $$;

-- (9) SEED WINDOW (plan 087 fork parity): seeds come from a buffered window of the first
-- seed_window = max(m_seeds*8, m_seeds+32) filter-passing stream candidates (m_seeds=1
-- -> 33) and are the m_seeds NEAREST within it; the window is EXEMPT from drop
-- accounting (fork phase 1/3a), so term_cond starts counting only after it closes.
-- Query ~ id 100.1 (vertex 100 has no P2 out-edges: reach = {100}), term_cond=8: the
-- operator must pull the full 33-candidate window PLUS >= 8 post-window drops ->
-- examined >= 41. Pre-087 first-m seeding terminated at examined ~ 14.
-- (A fixture where the FIRST emitted candidate is provably not the nearest cannot be
-- built deterministically on stock pgvector — HNSW level assignment is randomized, so
-- an emitted-order-divergence test would be flaky. The window-size + drop-exemption
-- footprint is the deterministic lock on the fork's nearest-in-window rule.)
DO $$
DECLARE got bigint[]; ex bigint; reason text;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 8, 1, 2, 'id', '',
    '[0.05005,0,0,0,0,0,0,0]'::vector, NULL, current_setting('tjs.ptype2')::int) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  IF got <> ARRAY[100,101,99,102,98]::bigint[] THEN
    RAISE EXCEPTION 'seed window: got % (expected [100,101,99,102,98])', got;
  END IF;
  IF ex < 41 OR ex > 80 THEN
    RAISE EXCEPTION 'seed window examined=% (expected 41..80: 33-candidate window exempt + term_cond 8)', ex;
  END IF;
  IF reason <> 'term_cond' THEN
    RAISE EXCEPTION 'seed window reason=% (expected term_cond)', reason;
  END IF;
  RAISE NOTICE 'PASS 9: 33-candidate seed window buffered, drop-exempt, nearest-in-window seed (examined %)', ex;
END $$;

-- (10) UNIFORM STREAM ACCOUNTING (plan 087 fork parity): hub 500 --P2--> {400..600}\{500}
-- makes EVERY near-band candidate a bridge. The fork admits every streamed candidate to
-- the vector top-k and the drop counter sees the uniform improve-or-drop outcome — a
-- bridge-dense prefix must NOT defer term_cond. Pre-087, bridges bypassed both the
-- vector heap and the counter, so this scan marched ~214 stream candidates deep before
-- terminating; post-087 the stream stops at window(33) + term_cond(8) = 41. The scan
-- budget 100 sits between the two, so the discrimination is CATEGORICAL: uniform
-- accounting -> term_cond fires well under the budget (reason 'term_cond'); the old
-- bridge-exempt counter would hit the budget first (reason 'stream_end_unknown').
-- examined = 41 streamed + 160 phase-3b direct fetches (reach 201 minus the 41 in-stream
-- reach members) = 201 — direct fetches count as examined work (plan 074 contract).
SELECT count(*) FROM (
  SELECT graph_store.gph_insert_edge(500, g, current_setting('tjs.ptype2')::int)
  FROM generate_series(400, 600) AS g WHERE g <> 500
) s;
SET hnsw.max_scan_tuples = 100;
DO $$
DECLARE got bigint[]; ex bigint; reason text;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 8, 1, 2, 'id', '',
    '[0.24990,0,0,0,0,0,0,0]'::vector, NULL, current_setting('tjs.ptype2')::int) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  IF got <> ARRAY[500,499,501,498,502]::bigint[] THEN
    RAISE EXCEPTION 'uniform accounting: got % (expected [500,499,501,498,502])', got;
  END IF;
  IF reason <> 'term_cond' THEN
    RAISE EXCEPTION 'uniform accounting reason=% (term_cond must fire on a bridge-dense stream under the scan budget)', reason;
  END IF;
  IF ex <> 201 THEN
    RAISE EXCEPTION 'uniform accounting examined=% (expected 201 = 41 streamed + 160 direct fetches)', ex;
  END IF;
  RAISE NOTICE 'PASS 10: bridges compete in the vector top-k and drop accounting (examined %, reason %)', ex, reason;
END $$;
SET hnsw.max_scan_tuples = 20000;

\echo === tjs_pg (stock-PG fused operator, ADR-0019): ALL PASS ===

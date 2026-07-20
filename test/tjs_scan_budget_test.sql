-- tjs_scan_budget_test.sql — tjs.vector_scan_budget (plan 102 / issue #30): E3 budget-shaped
-- termination for the seedless/vector-first stream. Runs via scripts/pg17_graph_test.sh
-- (stock PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- Contract under test:
--   * 0 (default) = disabled: behavior byte-identical to pre-102 (the whole existing suite —
--     test/tjs_pg_test.sql, test/tjs_pg_tr1_test.sql, test/tjs_ppr_test.sql — runs at the
--     default and is the primary negative control; S1 below additionally proves DEFAULT and
--     explicit 0 agree with each other on this file's own fixture).
--   * > 0: the stream ends after examining exactly `budget` visible heap candidates, and the
--     ending is DISCLOSED, never silent: tjs_open_termination_reason() = 'scan_budget',
--     tjs_open_budget_capped() = true (the first observable-true case — the operator owns
--     this cap, unlike pgvector's unobservable stream end, which stays
--     'stream_end_unknown'/NULL).
--   * The ADR-0007 drop rule is UNTOUCHED (DEV-1169: filter-failers never count as drops) —
--     S5 proves the exemption still holds with the budget compiled in, and that the budget
--     (which counts ALL examined candidates, passers or not) is the disclosed bound for the
--     no-more-passers drains the drop rule deliberately cannot terminate.
--
-- Corpus: the tjs_pg_test.sql shape — 2000 entities, embedding = [i/2000, 0 x7] (monotone in
-- id), ts = 100 for id < 500 else 900; graph: dense vids 0..1999, hub 2 --P1--> {1000..1100}.

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

SET hnsw.iterative_scan = relaxed_order;
SET hnsw.max_scan_tuples = 20000;

-- (S1) NEGATIVE CONTROL: DEFAULT (never set in this session) and explicit 0 are the same
-- disabled behavior — same ids, same examined, same reason ('stream_end_unknown': budget
-- 20000 > table 2000, the stream exhausts; pre-102 shape, tjs_pg_test PASS 6b), and
-- budget_capped() stays SQL NULL — the plan-074 censoring contract is unchanged when the
-- new GUC is off.
DO $$
DECLARE got_def bigint[]; got_zero bigint[]; ex_def bigint; ex_zero bigint;
        r_def text; r_zero text; c_def boolean; c_zero boolean;
BEGIN
  SELECT array_agg(t) INTO got_def FROM tjs_open('entities', 5, 0, 0, 0, 'id', 'ts < 500',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  ex_def := tjs_open_candidates_examined();
  r_def := tjs_open_termination_reason();
  c_def := tjs_open_budget_capped();

  SET tjs.vector_scan_budget = 0;
  SELECT array_agg(t) INTO got_zero FROM tjs_open('entities', 5, 0, 0, 0, 'id', 'ts < 500',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  ex_zero := tjs_open_candidates_examined();
  r_zero := tjs_open_termination_reason();
  c_zero := tjs_open_budget_capped();
  RESET tjs.vector_scan_budget;

  IF got_def IS DISTINCT FROM got_zero OR ex_def <> ex_zero
     OR r_def <> r_zero OR c_def IS DISTINCT FROM c_zero THEN
    RAISE EXCEPTION 'S1: default vs explicit-0 diverge: ids %/%, examined %/%, reason %/%, capped %/%',
      got_def, got_zero, ex_def, ex_zero, r_def, r_zero, c_def, c_zero;
  END IF;
  IF r_def <> 'stream_end_unknown' THEN
    RAISE EXCEPTION 'S1: budget-off exhausted-stream reason=% (expected stream_end_unknown)', r_def;
  END IF;
  IF c_def IS NOT NULL THEN
    RAISE EXCEPTION 'S1: budget-off capped=% (expected SQL NULL — plan 074 censoring unchanged)', c_def;
  END IF;
  RAISE NOTICE 'PASS S1: budget off (default == explicit 0) — ids/examined/reason/capped identical, censoring contract unchanged';
END $$;

-- (S2) BUDGET FIRES, exactly and disclosed: term_cond = 0 (the drop rule cannot fire),
-- budget = 50 << the 2000-row stream. examined must be EXACTLY 50 (the cap's unit is the
-- candidates-examined counter), reason 'scan_budget', capped TRUE (the first observable
-- true), and the call is deterministic (two runs, identical arrays).
SET tjs.vector_scan_budget = 50;
DO $$
DECLARE got1 bigint[]; got2 bigint[]; ex bigint; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got1 FROM tjs_open('entities', 10, 0, 0, 0, 'id', '',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF ex <> 50 THEN
    RAISE EXCEPTION 'S2: examined=% (expected exactly 50, the budget)', ex;
  END IF;
  IF reason <> 'scan_budget' THEN
    RAISE EXCEPTION 'S2: reason=% (expected scan_budget)', reason;
  END IF;
  IF capped IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'S2: capped=% (expected true — the operator OWNS this budget signal)', capped;
  END IF;
  IF array_length(got1, 1) <> 10 THEN
    RAISE EXCEPTION 'S2: got % ids (expected k=10 from the 50-candidate prefix)', array_length(got1, 1);
  END IF;
  SELECT array_agg(t) INTO got2 FROM tjs_open('entities', 10, 0, 0, 0, 'id', '',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  IF got1 IS DISTINCT FROM got2 THEN
    RAISE EXCEPTION 'S2: budget-capped result not deterministic: % vs %', got1, got2;
  END IF;
  RAISE NOTICE 'PASS S2: budget=50 -> examined=50 exactly, reason=scan_budget, capped=true, deterministic top-k';
END $$;
RESET tjs.vector_scan_budget;

-- (S3) term_cond fires FIRST: a small term_cond with a huge budget must keep the KNOWN
-- 'term_cond' ending and capped = false — the budget never claims an ending it did not
-- cause. (Same call shape as tjs_pg_test PASS 4, which pins the budget-off twin.)
SET tjs.vector_scan_budget = 100000;
SET tjs.graph_scoring = 'membership';
DO $$
DECLARE ex bigint; reason text; capped boolean;
BEGIN
  PERFORM t FROM tjs_open('entities', 5, 64, 0, 0, 'id', 'ts < 500',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  ex := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF reason <> 'term_cond' THEN
    RAISE EXCEPTION 'S3: reason=% (expected term_cond — the drop rule fired before the budget)', reason;
  END IF;
  IF capped IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'S3: capped=% (expected false — no cap fired)', capped;
  END IF;
  IF ex >= 100000 THEN RAISE EXCEPTION 'S3: examined=% at the budget (term_cond did not stop)', ex; END IF;
  RAISE NOTICE 'PASS S3: term_cond ending wins over an unhit budget (reason=term_cond, capped=false, examined=%)', ex;
END $$;
RESET tjs.graph_scoring;

-- (S4) budget NOT reached before natural stream end: the ending stays right-censored
-- ('stream_end_unknown' / NULL) — an unhit budget never manufactures a cap claim.
DO $$
DECLARE reason text; capped boolean;
BEGIN
  PERFORM t FROM tjs_open('entities', 5, 0, 0, 0, 'id', '',
    '[0.1,0,0,0,0,0,0,0]'::vector) AS t;
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF reason <> 'stream_end_unknown' THEN
    RAISE EXCEPTION 'S4: reason=% (expected stream_end_unknown — budget 100000 unhit at table size 2000)', reason;
  END IF;
  IF capped IS NOT NULL THEN
    RAISE EXCEPTION 'S4: capped=% (expected SQL NULL — pgvector''s own stream end stays unobservable)', capped;
  END IF;
  RAISE NOTICE 'PASS S4: unhit budget stays honest — natural stream end still stream_end_unknown/NULL';
END $$;
RESET tjs.vector_scan_budget;

-- (S5) DEV-1169 REGRESSION GUARD + the disclosed bound it necessitates. Query [0.0001,...]
-- (nearest = id 0) with filter ts >= 900: the ~500 nearest candidates (ids 0..499, ts=100)
-- ALL fail the filter. (a) Budget OFF, term_cond=8: filter-failers are EXEMPT from the drop
-- count (DEV-1169), so the operator must stream PAST the ~500-failer prefix and return the
-- true passers (ids 500..509 region) — a regression that counts failers as drops terminates
-- inside the prefix and returns junk/empty. (b) Budget = 300 (< the failer prefix): the
-- budget is predicate-BLIND, so it fires mid-prefix and disclosed-censors the call —
-- exactly the no-more-passers drain issue #30 measured at 1M (there: ~21k tuples, ~200 ms).
DO $$
DECLARE got bigint[]; oracle bigint[]; hits int := 0; i int; reason text;
BEGIN
  -- (a) the DEV-1169 exemption, budget off
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 10, 8, 0, 0, 'id', 'ts >= 900',
    '[0.0001,0,0,0,0,0,0,0]'::vector) AS t;
  SELECT array_agg(id) INTO oracle FROM (
    SELECT id FROM entities WHERE ts >= 900
    ORDER BY embedding <-> '[0.0001,0,0,0,0,0,0,0]'::vector, id LIMIT 10) q;
  FOR i IN 1..10 LOOP
    IF got @> ARRAY[oracle[i]] THEN hits := hits + 1; END IF;
  END LOOP;
  IF hits < 8 THEN
    RAISE EXCEPTION 'S5a DEV-1169 REGRESSION: recall %/10 across a ~500-failer prefix (got %, oracle %) — failers were counted as drops', hits, got, oracle;
  END IF;
  IF tjs_open_candidates_examined() <= 500 THEN
    RAISE EXCEPTION 'S5a: examined=% (must exceed the ~500-failer prefix to find passers)',
      tjs_open_candidates_examined();
  END IF;
  RAISE NOTICE 'PASS S5a: DEV-1169 exemption intact — %/10 recall past a 500-failer prefix (examined %)',
    hits, tjs_open_candidates_examined();
END $$;
SET tjs.vector_scan_budget = 300;
DO $$
DECLARE got bigint[]; bad int; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 10, 8, 0, 0, 'id', 'ts >= 900',
    '[0.0001,0,0,0,0,0,0,0]'::vector) AS t;
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF tjs_open_candidates_examined() <> 300 THEN
    RAISE EXCEPTION 'S5b: examined=% (expected exactly 300 — the budget is predicate-blind)',
      tjs_open_candidates_examined();
  END IF;
  IF reason <> 'scan_budget' OR capped IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'S5b: reason=%, capped=% (expected scan_budget/true — a censored call is DISCLOSED, never silent)',
      reason, capped;
  END IF;
  -- whatever the capped prefix yielded must still honor the filter (never junk rows)
  SELECT count(*) INTO bad FROM unnest(got) AS u WHERE u < 500;
  IF bad > 0 THEN
    RAISE EXCEPTION 'S5b: % filter-failing ids leaked into the capped result %', bad, got;
  END IF;
  RAISE NOTICE 'PASS S5b: budget=300 censors the no-passers-yet drain, disclosed (examined=300, scan_budget/true, % passer rows)',
    coalesce(array_length(got, 1), 0);
END $$;
RESET tjs.vector_scan_budget;

-- (S6) budget fires INSIDE the seed window (m_seeds > 0): with m_seeds=2 the seed window
-- is max(2*8, 2+32) = 34 > budget 20, so the stream ends mid-window via the cap and the
-- operator must seed from the PARTIAL buffer (the stream-end fallback path), complete
-- normally, and disclose 'scan_budget'.
SET tjs.graph_scoring = 'membership';
SET tjs.vector_scan_budget = 20;
DO $$
DECLARE got bigint[]; reason text; capped boolean;
BEGIN
  SELECT array_agg(t) INTO got FROM tjs_open('entities', 5, 32, 2, 2, 'id', '',
    '[0.0011,0,0,0,0,0,0,0]'::vector) AS t;
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  IF tjs_open_candidates_examined() < 20 THEN
    RAISE EXCEPTION 'S6: examined=% (expected >= 20: the 20-candidate stream cap plus any phase-3b bridge fetches)',
      tjs_open_candidates_examined();
  END IF;
  IF reason <> 'scan_budget' OR capped IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'S6: reason=%, capped=% (expected scan_budget/true)', reason, capped;
  END IF;
  IF got IS NULL OR array_length(got, 1) < 1 THEN
    RAISE EXCEPTION 'S6: empty result from the partial-window seed path (got %)', got;
  END IF;
  RAISE NOTICE 'PASS S6: budget inside the seed window -> partial-buffer seeding completes, disclosed (got %, reason=%)',
    got, reason;
END $$;
RESET tjs.vector_scan_budget;
RESET tjs.graph_scoring;
SET hnsw.iterative_scan = DEFAULT;
SET hnsw.max_scan_tuples = DEFAULT;

\echo '============ tjs_pg vector scan budget (plan 102 / issue #30): ALL TESTS PASSED ============'

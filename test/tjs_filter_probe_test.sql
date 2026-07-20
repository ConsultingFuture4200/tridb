-- tjs_filter_probe_test.sql — the plan 103 / issue #31 BYTE-PARITY suite: the seedless
-- ExprState filter fast path (tjs.filter_probe = auto) vs the cached-SPI probe (= spi).
-- Runs via scripts/pg17_graph_test.sh (stock PG 16/17 + pgvector + graph_store_am + tjs_pg).
--
-- Contract under test:
--   * Result sets, tjs_open_candidates_examined(), tjs_open_termination_reason(), and
--     tjs_open_budget_capped() are IDENTICAL between the two probe modes on the supported
--     subset — for every filter shape in the matrix below.
--   * NON-VACUITY: the fast path actually engages for eligible fragments — proven via the
--     tjs.last_filter_probe_mode report register ('expr' under auto), not assumed.
--   * Ineligible fragments (subqueries) silently take the SPI path under auto ('spi' in the
--     register) and still agree with forced spi.
--   * Error parity: a fragment SPI rejects (unknown column) raises the SAME error class in
--     both modes (the SPI fallback plan is prepared first, unconditionally); a fragment that
--     errors at evaluation time (bad cast) raises the same class in both modes too.
--   * ACL guard: a caller with only COLUMN-level SELECT grants falls back to SPI (which
--     enforces column ACLs through the executor) and still gets the right answer.
--   * Plan-102 interaction: tjs.vector_scan_budget caps + disclosure identical on both paths.
--   * DEV-1169: the filter-failer drop-count exemption holds on the fast path (deep drain
--     through a ~500-failer prefix still finds the true passers).
--
-- Corpus: the tjs_pg_test.sql shape widened for the filter matrix — 2000 entities,
-- embedding = [i/2000, 0 x7] (monotone in id), ts = 100 for id < 500 else 900,
-- nv = NULL when id % 3 = 0 else id, name = 'e' || id, tags = [id % 10, id % 7];
-- graph: dense vids 0..1999, hub 2 --P1--> {1000..1100}.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS graph_store_am;
CREATE EXTENSION IF NOT EXISTS tjs_pg;

CREATE TABLE entities (id bigint PRIMARY KEY, ts int, nv int, name text, tags int[],
                       embedding vector(8));
INSERT INTO entities
  SELECT g, CASE WHEN g < 500 THEN 100 ELSE 900 END,
         CASE WHEN g % 3 = 0 THEN NULL ELSE g END,
         'e' || g,
         ARRAY[g % 10, g % 7],
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

-- a STABLE user function for the matrix (the fast path must run it per candidate,
-- exactly as the SPI executor would)
CREATE FUNCTION f_stable_even(bigint) RETURNS boolean
  LANGUAGE sql STABLE AS 'SELECT $1 % 2 = 0';

-- one seedless run under an explicit probe mode; restores tjs.filter_probe = auto after
CREATE FUNCTION probe_run(fltr text, mode text, k int, tc int, ms int, hp int, q vector,
                          OUT ids bigint[], OUT examined bigint, OUT reason text,
                          OUT capped boolean, OUT probe_mode text)
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('tjs.filter_probe', mode, false);
  SELECT array_agg(t) INTO ids
    FROM tjs_open('entities', k, tc, ms, hp, 'id', fltr, q) AS t;
  examined := tjs_open_candidates_examined();
  reason := tjs_open_termination_reason();
  capped := tjs_open_budget_capped();
  probe_mode := current_setting('tjs.last_filter_probe_mode');
  PERFORM set_config('tjs.filter_probe', 'auto', false);
END $$;

SET hnsw.iterative_scan = relaxed_order;
SET hnsw.max_scan_tuples = 20000;

-- (P1) THE PARITY MATRIX: eligible fragments — simple predicates, array @>, NULL-involving
-- predicates, type coercions, function calls (builtin + STABLE user function), compounds.
-- Each must be byte-identical across modes AND must actually engage the fast path.
DO $$
DECLARE
  filters text[] := ARRAY[
    'ts < 500',
    'tags @> ARRAY[3]',
    'nv IS NULL',
    'nv > 1000',
    'ts < 500.5',
    'length(name) > 4',
    'f_stable_even(id)',
    'ts < 500 AND nv IS NOT NULL AND tags @> ARRAY[1]'
  ];
  f text; a record; s record;
BEGIN
  FOREACH f IN ARRAY filters LOOP
    SELECT * INTO a FROM probe_run(f, 'auto', 10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
    SELECT * INTO s FROM probe_run(f, 'spi',  10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
    IF a.ids IS DISTINCT FROM s.ids OR a.examined <> s.examined
       OR a.reason <> s.reason OR a.capped IS DISTINCT FROM s.capped THEN
      RAISE EXCEPTION 'P1 PARITY DIVERGENCE on [%]: ids %/%, examined %/%, reason %/%, capped %/%',
        f, a.ids, s.ids, a.examined, s.examined, a.reason, s.reason, a.capped, s.capped;
    END IF;
    IF a.probe_mode <> 'expr' THEN
      RAISE EXCEPTION 'P1 NON-VACUITY: eligible filter [%] did not engage the fast path (mode=%)',
        f, a.probe_mode;
    END IF;
    IF s.probe_mode <> 'spi' THEN
      RAISE EXCEPTION 'P1: forced spi not reported for [%] (mode=%)', f, s.probe_mode;
    END IF;
    RAISE NOTICE 'PASS P1 [%]: auto==spi (% ids, examined %, reason %), fast path engaged',
      f, coalesce(array_length(a.ids, 1), 0), a.examined, a.reason;
  END LOOP;
END $$;

-- (P2) SUBQUERY FRAGMENTS: must take the SPI path under auto (register says 'spi' — the
-- guard, not a crash) and still agree with forced spi.
DO $$
DECLARE
  filters text[] := ARRAY[
    'ts IN (SELECT 100)',
    'ts = (SELECT max(x) FROM (VALUES (100), (900)) v(x) WHERE x < 500)'
  ];
  f text; a record; s record;
BEGIN
  FOREACH f IN ARRAY filters LOOP
    SELECT * INTO a FROM probe_run(f, 'auto', 10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
    SELECT * INTO s FROM probe_run(f, 'spi',  10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
    IF a.ids IS DISTINCT FROM s.ids OR a.examined <> s.examined OR a.reason <> s.reason THEN
      RAISE EXCEPTION 'P2 PARITY DIVERGENCE on [%]: ids %/%, examined %/%, reason %/%',
        f, a.ids, s.ids, a.examined, s.examined, a.reason, s.reason;
    END IF;
    IF a.probe_mode <> 'spi' THEN
      RAISE EXCEPTION 'P2: SubLink filter [%] must fall back to SPI under auto (mode=%)',
        f, a.probe_mode;
    END IF;
    RAISE NOTICE 'PASS P2 [%]: SubLink fragment fell back to SPI under auto and agrees', f;
  END LOOP;
END $$;

-- (P3) EMPTY FILTER: no probe at all — register reports 'none' in both modes, results agree.
DO $$
DECLARE a record; s record;
BEGIN
  SELECT * INTO a FROM probe_run('', 'auto', 10, 0, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  SELECT * INTO s FROM probe_run('', 'spi',  10, 0, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  IF a.ids IS DISTINCT FROM s.ids OR a.examined <> s.examined OR a.reason <> s.reason THEN
    RAISE EXCEPTION 'P3: empty-filter divergence: ids %/%, examined %/%, reason %/%',
      a.ids, s.ids, a.examined, s.examined, a.reason, s.reason;
  END IF;
  IF a.probe_mode <> 'none' OR s.probe_mode <> 'none' THEN
    RAISE EXCEPTION 'P3: empty filter must report mode none/none (got %/%)', a.probe_mode, s.probe_mode;
  END IF;
  RAISE NOTICE 'PASS P3: empty filter — no probe on either path (mode none), identical results';
END $$;

-- (P4) ERROR PARITY, prepare-time: a fragment referencing a column that does not exist must
-- raise undefined_column (42703) in BOTH modes — under auto, the SPI fallback plan is
-- prepared FIRST, so the error comes from the same place as pre-103.
DO $$
DECLARE st_auto text := 'no error'; st_spi text := 'no error';
BEGIN
  BEGIN
    PERFORM probe_run('no_such_col > 0', 'auto', 10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  EXCEPTION WHEN undefined_column THEN st_auto := SQLSTATE; END;
  BEGIN
    PERFORM probe_run('no_such_col > 0', 'spi', 10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  EXCEPTION WHEN undefined_column THEN st_spi := SQLSTATE; END;
  IF st_auto <> '42703' OR st_spi <> '42703' THEN
    RAISE EXCEPTION 'P4: undefined-column error class diverged: auto=%, spi=%', st_auto, st_spi;
  END IF;
  RAISE NOTICE 'PASS P4: unknown-column fragment errors 42703 in both modes';
END $$;

-- (P5) ERROR PARITY, evaluation-time: a fragment that fails per-row (text -> int cast on
-- 'e<id>') must raise invalid_text_representation (22P02) in BOTH modes.
DO $$
DECLARE st_auto text := 'no error'; st_spi text := 'no error';
BEGIN
  BEGIN
    PERFORM probe_run('name::int > 0', 'auto', 10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  EXCEPTION WHEN invalid_text_representation THEN st_auto := SQLSTATE; END;
  BEGIN
    PERFORM probe_run('name::int > 0', 'spi', 10, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  EXCEPTION WHEN invalid_text_representation THEN st_spi := SQLSTATE; END;
  IF st_auto <> '22P02' OR st_spi <> '22P02' THEN
    RAISE EXCEPTION 'P5: runtime-error class diverged: auto=%, spi=%', st_auto, st_spi;
  END IF;
  RAISE NOTICE 'PASS P5: per-row cast failure errors 22P02 in both modes';
END $$;

-- (P6) PLAN-102 INTERACTION: tjs.vector_scan_budget caps the drain identically on both
-- paths — examined EXACTLY the budget, 'scan_budget'/capped=true disclosed, identical ids.
SET tjs.vector_scan_budget = 50;
DO $$
DECLARE a record; s record;
BEGIN
  SELECT * INTO a FROM probe_run('ts >= 900', 'auto', 10, 0, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  SELECT * INTO s FROM probe_run('ts >= 900', 'spi',  10, 0, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  IF a.examined <> 50 OR s.examined <> 50 THEN
    RAISE EXCEPTION 'P6: budget=50 but examined auto=%, spi=% (cap must be probe-mode-independent)',
      a.examined, s.examined;
  END IF;
  IF a.reason <> 'scan_budget' OR s.reason <> 'scan_budget'
     OR a.capped IS DISTINCT FROM true OR s.capped IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'P6: disclosure diverged: reason %/%, capped %/%',
      a.reason, s.reason, a.capped, s.capped;
  END IF;
  IF a.ids IS DISTINCT FROM s.ids THEN
    RAISE EXCEPTION 'P6: capped result sets diverged: % vs %', a.ids, s.ids;
  END IF;
  IF a.probe_mode <> 'expr' THEN
    RAISE EXCEPTION 'P6: fast path did not engage under the budget (mode=%)', a.probe_mode;
  END IF;
  RAISE NOTICE 'PASS P6: scan budget cap + disclosure identical on both paths (examined=50, scan_budget/true)';
END $$;
RESET tjs.vector_scan_budget;

-- (P7) DEV-1169 GUARD ON THE FAST PATH: query [0.0001,...] with 'ts >= 900' — the ~500
-- nearest candidates ALL fail the filter; failers are exempt from the drop count, so both
-- modes must stream past the prefix (examined > 500) and agree on the true passers.
DO $$
DECLARE a record; s record;
BEGIN
  SELECT * INTO a FROM probe_run('ts >= 900', 'auto', 10, 8, 0, 0, '[0.0001,0,0,0,0,0,0,0]'::vector);
  SELECT * INTO s FROM probe_run('ts >= 900', 'spi',  10, 8, 0, 0, '[0.0001,0,0,0,0,0,0,0]'::vector);
  IF a.ids IS DISTINCT FROM s.ids OR a.examined <> s.examined OR a.reason <> s.reason THEN
    RAISE EXCEPTION 'P7 PARITY DIVERGENCE: ids %/%, examined %/%, reason %/%',
      a.ids, s.ids, a.examined, s.examined, a.reason, s.reason;
  END IF;
  IF a.examined <= 500 THEN
    RAISE EXCEPTION 'P7 DEV-1169 REGRESSION: examined=% (must exceed the ~500-failer prefix)', a.examined;
  END IF;
  IF a.probe_mode <> 'expr' THEN
    RAISE EXCEPTION 'P7: fast path did not engage (mode=%)', a.probe_mode;
  END IF;
  RAISE NOTICE 'PASS P7: DEV-1169 exemption intact on the fast path (examined=%, identical passers)', a.examined;
END $$;

-- (P8) GRAPH LEG (m_seeds > 0, membership scoring for determinism): the stream probe is the
-- only thing the mode switches — the phase-3b bridge fetch keeps its embedded-filter SPI
-- statement on BOTH paths — so seeded calls must agree end to end.
SET tjs.graph_scoring = 'membership';
DO $$
DECLARE a record; s record;
BEGIN
  SELECT * INTO a FROM probe_run('nv IS NOT NULL', 'auto', 5, 32, 2, 2, '[0.0011,0,0,0,0,0,0,0]'::vector);
  SELECT * INTO s FROM probe_run('nv IS NOT NULL', 'spi',  5, 32, 2, 2, '[0.0011,0,0,0,0,0,0,0]'::vector);
  IF a.ids IS DISTINCT FROM s.ids OR a.examined <> s.examined OR a.reason <> s.reason THEN
    RAISE EXCEPTION 'P8 PARITY DIVERGENCE (m_seeds=2): ids %/%, examined %/%, reason %/%',
      a.ids, s.ids, a.examined, s.examined, a.reason, s.reason;
  END IF;
  IF a.probe_mode <> 'expr' THEN
    RAISE EXCEPTION 'P8: fast path did not engage on the seeded call (mode=%)', a.probe_mode;
  END IF;
  RAISE NOTICE 'PASS P8: seeded (graph-leg) call identical across probe modes (% ids)',
    coalesce(array_length(a.ids, 1), 0);
END $$;
RESET tjs.graph_scoring;

-- (P9) ACL GUARD: a role with only COLUMN-level SELECT grants must NOT get the fast path
-- (table-level SELECT is the eligibility bar) — it falls back to SPI, which enforces the
-- column ACLs through the executor, and the answer matches the table-owner's.
DO $$
DECLARE a record;
BEGIN
  SELECT * INTO a FROM probe_run('ts < 500', 'auto', 5, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  PERFORM set_config('tjs_test.owner_ids', a.ids::text, false);
  IF a.probe_mode <> 'expr' THEN
    RAISE EXCEPTION 'P9 setup: owner run should use the fast path (mode=%)', a.probe_mode;
  END IF;
END $$;
CREATE ROLE tjs_probe_col_user;
GRANT SELECT (id, ts, embedding) ON entities TO tjs_probe_col_user;
-- tjs_open is REVOKEd from PUBLIC (internal-only surface, SECURITY.md) — the ACL under
-- test is the TABLE grant, so EXECUTE is granted explicitly.
GRANT EXECUTE ON FUNCTION
  tjs_open(regclass, integer, integer, integer, integer, text, text, vector, bigint, integer)
  TO tjs_probe_col_user;
SET ROLE tjs_probe_col_user;
DO $$
DECLARE a record;
BEGIN
  SELECT * INTO a FROM probe_run('ts < 500', 'auto', 5, 8, 0, 0, '[0.1,0,0,0,0,0,0,0]'::vector);
  IF a.probe_mode <> 'spi' THEN
    RAISE EXCEPTION 'P9: column-grant-only caller must fall back to SPI (mode=%)', a.probe_mode;
  END IF;
  IF a.ids::text IS DISTINCT FROM current_setting('tjs_test.owner_ids') THEN
    RAISE EXCEPTION 'P9: column-grant caller result % != owner result %',
      a.ids, current_setting('tjs_test.owner_ids');
  END IF;
  RAISE NOTICE 'PASS P9: column-level-grant caller took the SPI path (ACL guard) with the same answer';
END $$;
RESET ROLE;
REVOKE ALL ON entities FROM tjs_probe_col_user;
REVOKE EXECUTE ON FUNCTION
  tjs_open(regclass, integer, integer, integer, integer, text, text, vector, bigint, integer)
  FROM tjs_probe_col_user;
DROP ROLE tjs_probe_col_user;

SET hnsw.iterative_scan = DEFAULT;
SET hnsw.max_scan_tuples = DEFAULT;

\echo '============ tjs_pg filter probe parity (plan 103 / issue #31): ALL TESTS PASSED ============'

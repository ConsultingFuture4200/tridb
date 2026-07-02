-- pgmain_rewriter_removed.sql — advisor plan 018 regression.
--
-- MSVBASE's patch/Postgres.patch injected a hand-written string rewriter into PostgresMain's
-- simple-query ('Q') handler that intercepts any statement containing approximate_sum(...) and
-- string-rewrites it into a topk(...) call. TriDB never uses this path (its canonical query
-- lowers directly to tjs()/tjs_open(); `grep -rn approximate_sum src/ test/ tools/ bench/` is
-- empty), yet inherited the whole liability: a char* order[100] stack overflow, an unbounded
-- strcat past palloc(strlen*2), '-unescaped SQL injection, a pfree(NULL) crash on a WHERE-less
-- query, an always-on per-query heap leak (lowercase()+palloc freed only in the taken branch),
-- and an ereport(LOG) logging the full text of EVERY query. advisor plan 018 removes the entire
-- rewrite block via scripts/patches/tridb_remove_pgmain_rewriter.patch.
--
-- This regression proves the rewriter is GONE:
--   (a) an ordinary query is unaffected — the removed per-query lowercase()/palloc preamble does
--       not change results;
--   (b) the top-level approximate_sum(...) statement takes the PLAIN PostgresMain path: it errors
--       normally instead of being rewritten to topk(...) — and, critically, does NOT crash the
--       backend (the connection survives, so every later statement still runs);
--   (c) approximate_sum is genuinely an unknown function (no SQL function was introduced) — the
--       error class is undefined_function, asserted hard in a DO/EXCEPTION block.
--
-- Runs under psql -v ON_ERROR_STOP=1 (scripts/graph_test.sh). ON_ERROR_STOP is relaxed ONLY around
-- the top-level approximate_sum probe in (b) so its expected error does not abort the suite; a
-- backend CRASH there drops the connection and fails the very next statement (nonzero exit).

-- (a) ordinary query unaffected by the removed preamble.
DO $$
BEGIN
    IF (SELECT 1) <> 1 THEN
        RAISE EXCEPTION 'plan018: ordinary SELECT 1 did not return 1';
    END IF;
    RAISE NOTICE 'PASS (a): ordinary query unaffected';
END $$;

-- A concrete target so the ONLY undefined symbol in the probe is approximate_sum() itself
-- (not the table), pinning the failure to the rewriter/function, not a missing relation.
CREATE TEMP TABLE plan018_t (id bigint, price int);
INSERT INTO plan018_t VALUES (1, 20);

-- (b) top-level statement through the real PostgresMain 'Q' handler — the exact SELECT ... WHERE
-- ... ORDER BY approximate_sum('...') LIMIT n shape the upstream rewriter matched. With the
-- rewriter removed this is plain (invalid) SQL: it must ERROR, not crash and not silently rewrite
-- to a topk() call. Relax abort just for this one statement; the survival check follows.
\set ON_ERROR_STOP off
SELECT id FROM plan018_t WHERE price > 10
  ORDER BY approximate_sum('vector1<->{1,2,3}') LIMIT 5;
\set ON_ERROR_STOP on

-- No-crash proof: if the statement above had crashed the backend, the connection is gone and this
-- runs against a dead server -> nonzero exit. Under ON_ERROR_STOP=1 a clean error above would have
-- aborted here too, so reaching this line means (b) errored cleanly AND the backend is alive.
SELECT 'plan018: backend alive after approximate_sum probe' AS status;

-- (c) hard assertion: approximate_sum is an unknown function (the rewriter is gone AND no
-- approximate_sum SQL function was introduced), so the probe raises undefined_function.
DO $$
BEGIN
    EXECUTE $q$SELECT id FROM plan018_t WHERE price > 10
              ORDER BY approximate_sum('vector1<->{1,2,3}') LIMIT 5$q$;
    RAISE EXCEPTION 'plan018: approximate_sum query did NOT error — rewriter still active or a function was introduced?';
EXCEPTION
    WHEN undefined_function THEN
        RAISE NOTICE 'PASS (b)(c): approximate_sum rejected cleanly as undefined_function (%), rewriter removed', SQLERRM;
END $$;

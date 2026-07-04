-- graph_freeze_test.sql — advisor plan 036 / DEV-1347: the gph_freeze() anti-wraparound pass.
--
-- Proves the design's acceptance sketch (docs/graph_store_freeze_design_v0.1.0.md §"Acceptance"),
-- the parts that ARE testable in CI (the 2^31-scale clock itself is not):
--   (a) pre-freeze answers == post-freeze answers (neighbors + counts): freeze is a pure storage
--       rewrite, visibility byte-identical;
--   (b) an ABORTED insert stays INVISIBLE across the freeze (its old xid -> InvalidTransactionId);
--   (d) pg_class.relfrozenxid for gstore is advanced to the horizon (disarms forced autovacuum);
--   (e) a re-run at the same horizon is a no-op (idempotent, returns 0);
--   plus horizon validation (a too-new horizon RAISES) and the plan-026 ACL (non-owner denied).
-- (c) crash/WAL durability of the frozen pages is covered by scripts/crash_recovery_test.sh.
--
-- Run by scripts/graph_freeze_test.sh (AM harness: PGXS-builds src/graph_store in the image) with
-- psql -v ON_ERROR_STOP=1, so any RAISE EXCEPTION produces a nonzero exit. GX10/Docker only.

CREATE EXTENSION graph_store_am;
SET search_path TO graph_store, public;

-- Seed 5 committed vertices (vids 0..4) and 3 committed edges from vid 0. Each statement is its own
-- (auto-commit) transaction, so each consumes a real xid that will fall below the freeze horizon.
SELECT gph_insert_vertex() FROM generate_series(1, 5);   -- vids 0..4
SELECT gph_insert_edge(0, 1);
SELECT gph_insert_edge(0, 2);
SELECT gph_insert_edge(0, 3);

-- An ABORTED insert BEFORE we capture the horizon: its xid is normal and below the horizon, so the
-- freeze must rewrite it to InvalidTransactionId and it must STAY invisible. The doomed vid is
-- gm_next_vid (deterministically 5); the doomed edge is 0 -> 5.
BEGIN;
    SELECT gph_insert_vertex();      -- doomed vid 5 (self-visible in-txn)
    SELECT gph_insert_edge(0, 5);    -- doomed edge 0 -> 5
ROLLBACK;

-- Pre-freeze snapshot (the (a) oracle). neighbors(0) must be exactly {1,2,3}; the doomed 5 absent.
CREATE TEMP TABLE pre AS
SELECT (SELECT array_agg(x ORDER BY x) FROM gph_neighbors(0) x) AS nbrs,
       gph_vertex_count() AS vcount,
       gph_edge_count()   AS ecount;

DO $$
DECLARE p record;
BEGIN
    SELECT * INTO p FROM pre;
    IF p.nbrs IS DISTINCT FROM ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION 'bad pre-freeze neighbors(0)=% (expected {1,2,3})', p.nbrs;
    END IF;
    IF p.vcount <> 5 THEN
        RAISE EXCEPTION 'bad pre-freeze vertex count=% (expected 5)', p.vcount;
    END IF;
    RAISE NOTICE 'baseline: neighbors(0)={1,2,3}, 5 visible vertices, edge_count=%', p.ecount;
END $$;

-- Capture the freeze horizon: this statement's xid is strictly greater than every seed/aborted
-- xmin above and (on a fresh cluster) well below 2^32 so the text->xid cast is exact.
CREATE TEMP TABLE hz AS SELECT (txid_current()::text)::xid AS h;

-- Burn a few xids so the horizon strictly PRECEDES the oldest running xmin when the freeze runs
-- (each autocommit txid_current() advances the global xid).
SELECT txid_current();
SELECT txid_current();
SELECT txid_current();

-- (Step 2/d) Run the freeze. It must rewrite the pre-horizon rows and return a positive count.
DO $$
DECLARE n bigint; h xid;
BEGIN
    SELECT hz.h INTO h FROM hz;
    n := graph_store.gph_freeze(h);
    IF n <= 0 THEN
        RAISE EXCEPTION 'gph_freeze(%) froze % records (expected > 0)', h, n;
    END IF;
    RAISE NOTICE 'PASS freeze: gph_freeze(%) froze % records', h, n;
END $$;

-- (a) visibility byte-identical; (b) aborted row still invisible.
DO $$
DECLARE p record; nbrs bigint[]; vc bigint;
BEGIN
    SELECT * INTO p FROM pre;
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    vc := gph_vertex_count();
    IF nbrs IS DISTINCT FROM p.nbrs THEN
        RAISE EXCEPTION '(a) neighbors(0)=% after freeze (expected % — visibility changed)', nbrs, p.nbrs;
    END IF;
    IF 5 = ANY(nbrs) THEN
        RAISE EXCEPTION '(b) aborted edge 0->5 became visible after freeze (neighbors=%)', nbrs;
    END IF;
    IF vc <> p.vcount THEN
        RAISE EXCEPTION '(a) vertex count=% after freeze (expected % — visibility changed)', vc, p.vcount;
    END IF;
    RAISE NOTICE 'PASS (a)+(b): neighbors + counts byte-identical; aborted row stays invisible';
END $$;

-- (d) relfrozenxid advanced to the horizon (this is what disarms the forced anti-wraparound vacuum).
DO $$
DECLARE rf xid; h xid;
BEGIN
    SELECT hz.h INTO h FROM hz;
    SELECT relfrozenxid INTO rf FROM pg_class WHERE oid = 'graph_store.gstore'::regclass;
    IF rf <> h THEN
        RAISE EXCEPTION '(d) gstore relfrozenxid=% after freeze (expected horizon %)', rf, h;
    END IF;
    RAISE NOTICE 'PASS (d): gstore relfrozenxid advanced to horizon %', h;
END $$;

-- (e) idempotent re-run at the same horizon is a no-op (monotonicity early-out returns 0).
DO $$
DECLARE n bigint; nbrs bigint[]; h xid;
BEGIN
    SELECT hz.h INTO h FROM hz;
    n := graph_store.gph_freeze(h);
    IF n <> 0 THEN
        RAISE EXCEPTION '(e) re-run gph_freeze(%) froze % records (expected 0 — not idempotent)', h, n;
    END IF;
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs IS DISTINCT FROM ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION '(e) neighbors(0)=% after re-run (expected {1,2,3})', nbrs;
    END IF;
    RAISE NOTICE 'PASS (e): idempotent re-run froze 0, visibility unchanged';
END $$;

-- Horizon validation: a horizon that does NOT precede the oldest running xmin must RAISE (freezing
-- an in-progress xid into permanent visibility is made unreachable). horizon = current xid + 100 is
-- strictly in the future of every running xmin on a fresh cluster.
DO $$
DECLARE too_new xid;
BEGIN
    too_new := ((txid_current() + 100)::text)::xid;
    BEGIN
        PERFORM graph_store.gph_freeze(too_new);
        RAISE EXCEPTION 'horizon validation missing: gph_freeze(%) (a future horizon) was accepted', too_new;
    EXCEPTION WHEN others THEN
        IF SQLERRM LIKE '%does not precede%' THEN
            RAISE NOTICE 'PASS validation: future horizon rejected: %', SQLERRM;
        ELSE
            RAISE;   -- some other error: propagate
        END IF;
    END;
END $$;

-- ACL (plan 026 discipline): gph_freeze is a maintenance mutator, REVOKEd from PUBLIC.
CREATE ROLE tridb_freeze_probe LOGIN;
GRANT USAGE ON SCHEMA graph_store TO tridb_freeze_probe;   -- schema visibility only (see ACL test)
SET ROLE tridb_freeze_probe;
DO $$ BEGIN
    BEGIN
        PERFORM graph_store.gph_freeze('1'::xid);
        RAISE EXCEPTION 'PUBLIC EXECUTE on gph_freeze was accepted (REVOKE missing)';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    RAISE NOTICE 'PASS ACL: gph_freeze() denied to non-owner (insufficient_privilege)';
END $$;
RESET ROLE;
REVOKE USAGE ON SCHEMA graph_store FROM tridb_freeze_probe;
DROP ROLE tridb_freeze_probe;

\echo '============ gph_freeze anti-wraparound pass (DEV-1347): ALL TESTS PASSED ============'

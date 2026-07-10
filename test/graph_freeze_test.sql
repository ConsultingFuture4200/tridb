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
-- Advisor plan 040 (DEV-1354) extends this with the xmax half of freeze — plan 037's tombstone
-- (es_xmax / vr_xmax) was previously left un-frozen, so a committed delete's xmax could outlive
-- relfrozenxid and hit truncated clog on a later read. Added below (after the horizon-validation
-- block, before the ACL section):
--   (f) tombstone-then-freeze: a COMMITTED delete stays deleted, no clog error, xmin+xmax both frozen;
--   (g) freeze mid-txn (past a still-open tombstone) is BLOCKED by the existing oldest-xmin guard;
--   (h) tombstone + ROLLBACK + freeze: the aborted delete resurrects the record (flag cleared, xmax
--       reset to Invalid) and it reads LIVE both before and after.
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

-- ============================================================================
-- (f) Tombstone-then-freeze (advisor plan 040 / DEV-1354's clog-truncation hazard): freeze previously
-- rewrote only es_xmin, so a COMMITTED delete's es_xmax survived past `relfrozenxid` unfrozen — a
-- later TransactionIdDidCommit(xmax) call in gph_deleted_visible could hit clog truncated past that
-- xid. This proves the fixed freeze rewrites xmax too: 0->4 is inserted, tombstoned (committed
-- delete), then frozen past both xids; it must stay absent from traversal with no error, and the
-- freeze count must show BOTH its xmin and its xmax were newly frozen (2 — nothing else in the store
-- is unfrozen at this point).
-- ============================================================================
SELECT gph_insert_edge(0, 4);          -- vid 4 already exists (committed above); new edge 0->4

DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,2,3,4]::bigint[] THEN
        RAISE EXCEPTION 'f(setup): neighbors(0)=% (expected {1,2,3,4})', nbrs;
    END IF;
END $$;

SELECT gph_tombstone_edge(0, 4);       -- committed delete of 0->4 (its es_xmax is now a normal, committed xid)

-- Second horizon, captured after the tombstone commits; burn a few xids so it strictly precedes the
-- oldest running xmin, same discipline as the first horizon capture above.
CREATE TEMP TABLE hz2 AS SELECT (txid_current()::text)::xid AS h;
SELECT txid_current();
SELECT txid_current();
SELECT txid_current();

DO $$
DECLARE n bigint; h xid; nbrs bigint[];
BEGIN
    SELECT hz2.h INTO h FROM hz2;
    n := graph_store.gph_freeze(h);
    IF n <> 2 THEN
        RAISE EXCEPTION '(f) gph_freeze(%) froze % records (expected 2: 0->4''s es_xmin + es_xmax)', h, n;
    END IF;
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION '(f) neighbors(0)=% after tombstone-then-freeze (expected {1,2,3}: 0->4 stays deleted)', nbrs;
    END IF;
    RAISE NOTICE 'PASS (f): tombstone-then-freeze — 0->4 stays absent, no clog error, froze % records', n;
END $$;

-- ============================================================================
-- (g) Freeze mid-txn is BLOCKED: a horizon that would have to cover a still-OPEN transaction's own
-- xid can never precede the oldest running xmin, because that open transaction's own xid IS (part
-- of) the oldest running xmin — the pre-existing horizon-validation guard (design §1) already makes
-- this unreachable, so no second mechanism is needed. Chosen rule, documented here rather than
-- invented in the C: freezing past an in-flight tombstone is rejected by the SAME check that rejects
-- freezing past an in-flight insert.
-- ============================================================================
BEGIN;
    SELECT gph_tombstone_edge(0, 3);   -- self-visible in-txn delete; own xid still open
    DO $$
    DECLARE bad_h xid;
    BEGIN
        bad_h := ((txid_current() + 1)::text)::xid;    -- past our OWN still-open xid
        BEGIN
            PERFORM graph_store.gph_freeze(bad_h);
            RAISE EXCEPTION '(g) gph_freeze(%) inside the open tombstone txn was accepted (expected block)', bad_h;
        EXCEPTION WHEN others THEN
            IF SQLERRM LIKE '%does not precede%' THEN
                RAISE NOTICE 'PASS (g): freeze mid-txn blocked by the oldest-xmin guard: %', SQLERRM;
            ELSE
                RAISE;   -- some other error: propagate
            END IF;
        END;
    END $$;
ROLLBACK;

-- ============================================================================
-- (h) Tombstone, ROLLBACK, THEN freeze: GenericXLog has no in-process UNDO, so the aborted delete's
-- GPH_FLAG_DELETED + es_xmax bytes are still physically on the page after ROLLBACK (same as an
-- aborted INSERT) — 0->3 must read LIVE both before AND after a freeze that walks past this aborted
-- xmax. This exercises the resurrection rule: GPH_FLAG_DELETED + xmax ABORTED + <= horizon clears the
-- flag and resets xmax to Invalid (gph_freeze_xmax), matching the FR-7 rollback semantics
-- gph_deleted_visible already gives this edge before the freeze ever runs.
-- ============================================================================
BEGIN;
    SELECT gph_tombstone_edge(0, 3);
ROLLBACK;

DO $$
DECLARE nbrs bigint[];
BEGIN
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION '(h) neighbors(0)=% after ROLLBACK, pre-freeze (expected {1,2,3}: 0->3 still LIVE)', nbrs;
    END IF;
END $$;

CREATE TEMP TABLE hz3 AS SELECT (txid_current()::text)::xid AS h;
SELECT txid_current();
SELECT txid_current();
SELECT txid_current();

DO $$
DECLARE n bigint; h xid; nbrs bigint[];
BEGIN
    SELECT hz3.h INTO h FROM hz3;
    n := graph_store.gph_freeze(h);
    IF n <> 1 THEN
        RAISE EXCEPTION '(h) gph_freeze(%) froze % records (expected 1: 0->3''s aborted es_xmax)', h, n;
    END IF;
    SELECT array_agg(x ORDER BY x) INTO nbrs FROM gph_neighbors(0) x;
    IF nbrs <> ARRAY[1,2,3]::bigint[] THEN
        RAISE EXCEPTION '(h) neighbors(0)=% after freezing past a rolled-back tombstone (expected {1,2,3}: still LIVE)', nbrs;
    END IF;
    RAISE NOTICE 'PASS (h): rolled-back tombstone stays LIVE after freeze (froze % records)', n;
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

\echo '============ gph_freeze anti-wraparound pass (DEV-1347/DEV-1354): ALL TESTS PASSED ============'

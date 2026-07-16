# Plan 062: `tjs_open` rejects NULL args instead of segfaulting the backend

> **Executor instructions**: Follow this plan step by step. Run every verification command and
> confirm the expected result before moving on. If a STOP condition occurs, stop and report — do
> not improvise. When done, update the status row for this plan in `advisor-plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- src/tjs_pg/tjs_pg.c src/tjs_pg/tjs_pg--0.1.0.sql test/tjs_pg_test.sql`
> If any of those files changed since this plan was written, compare the "Current state" excerpts
> against the live code before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

`tjs_pg.c` is a new PostgreSQL C extension (the D2/ADR-0019 stock-PG fused operator). Its SRF
`tjs_open` is declared `LANGUAGE C VOLATILE` — **not** `STRICT` — and it fetches the text/vector
args 5, 6, 7 with no NULL check. A NULL `id_col`, `filter`, or `query` dereferences a NULL varlena
or is used as a NULL pointer in the index ScanKey and the distance recompute, **crashing the
backend (SIGSEGV)** and taking down every session on the server — worse than a clean error, and it
bypasses transaction-abort cleanup. The fork operator it replaces was `LANGUAGE C STRICT`
specifically to prevent this (documented in `test/tjs_open_smoke.sql`: "STRICT: NULL arg returns no
rows (previously: backend segfault)"). The operator legitimately **cannot** be `STRICT`, because
`src IS NULL` is a meaningful, required input (it selects the vector-first physical path) — so the
fix is explicit per-arg guards, not the `STRICT` marker.

## Current state

- `src/tjs_pg/tjs_pg--0.1.0.sql:9-21` declares the function:
  ```sql
  CREATE FUNCTION tjs_open(tbl regclass, k integer, term_cond integer, m_seeds integer,
                           hops integer, id_col text, filter text, query vector,
                           src bigint DEFAULT NULL, edge_type integer DEFAULT 0)
  RETURNS SETOF bigint
  AS 'MODULE_PATHNAME', 'tjs_open_pg'
  LANGUAGE C VOLATILE;   -- <-- no STRICT (correct: src IS NULL is meaningful)
  ```
- `src/tjs_pg/tjs_pg.c:279-286` — the arg fetches, guards on 8/9 only:
  ```c
  Oid         reloid = PG_GETARG_OID(0);
  int32       k = PG_GETARG_INT32(1);
  int32       term_cond = PG_GETARG_INT32(2);
  int32       m_seeds = PG_GETARG_INT32(3);
  int32       hops = PG_GETARG_INT32(4);
  text       *id_col_t = PG_GETARG_TEXT_PP(5);   // NULL -> segfault
  text       *filter_t = PG_GETARG_TEXT_PP(6);   // NULL -> segfault
  Datum       query_vec = PG_GETARG_DATUM(7);    // NULL -> segfault in ScanKey / FunctionCall2Coll
  bool        have_src = !PG_ARGISNULL(8);       // <-- only 8 and 9 are guarded
  int64       src = have_src ? PG_GETARG_INT64(8) : 0;
  int32       edge_type = PG_ARGISNULL(9) ? 0 : PG_GETARG_INT32(9);
  ```
- Just below (`tjs_pg.c:288-296`) there is already a block of `ereport(ERROR, ...)` validation for
  `k`, `hops`, `term_cond` — **match that exact style** for the null guards; put the null checks
  first (before those range checks).

Repo convention for arg validation in this file: plain `ereport(ERROR, (errmsg("tjs_open: ...")))`,
lowercase `tjs_open:` prefix, no errcode needed. See `tjs_pg.c:288-296`.

## Steps

1. In `src/tjs_pg/tjs_pg.c`, immediately after the arg-fetch block (after line 286, before the
   existing `if (k <= 0 ...)` range checks at ~288), add a NULL guard for the required args
   **0 and 1–7** (args 8 `src` and 9 `edge_type` stay optional — do NOT guard them):
   ```c
   if (PG_ARGISNULL(0) || PG_ARGISNULL(1) || PG_ARGISNULL(2) || PG_ARGISNULL(3) ||
       PG_ARGISNULL(4) || PG_ARGISNULL(5) || PG_ARGISNULL(6) || PG_ARGISNULL(7))
       ereport(ERROR,
               (errmsg("tjs_open: args tbl, k, term_cond, m_seeds, hops, id_col, filter, "
                       "query must all be non-NULL (src and edge_type may be NULL)")));
   ```
   Place this **before** the three `PG_GETARG_*` calls for args 5/6/7 dereference their varlenas —
   i.e. move the `id_col_t`/`filter_t`/`query_vec` fetches to *after* the guard, OR (simpler) keep
   the fetches where they are but note `PG_GETARG_TEXT_PP` on a NULL is the crash, so the guard
   MUST run before them. The clean form: declare the three vars, then guard, then assign:
   ```c
   text       *id_col_t;
   text       *filter_t;
   Datum       query_vec;
   /* ... after the PG_ARGISNULL guard above ... */
   id_col_t = PG_GETARG_TEXT_PP(5);
   filter_t = PG_GETARG_TEXT_PP(6);
   query_vec = PG_GETARG_DATUM(7);
   ```
   Keep `have_src`/`src`/`edge_type` exactly as they are (they correctly handle NULL).

2. Add a NULL-arg regression test to `test/tjs_pg_test.sql`. The suite already sets up the
   `entities` table + extensions and uses `DO $$ ... EXCEPTION WHEN others ...` blocks (see its
   PASS 3, which asserts a clean error on the missing-relaxed-order case). Add a new block after
   the existing PASS 3 that asserts each required NULL arg raises (not crashes). Follow the exact
   pattern of PASS 3 (`tjs_pg_test.sql:58-69`):
   ```sql
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
   ```

## Verification

The graph AM + operator build/run only inside the stock-PG docker image (no local Postgres).

1. Build the image (if not already built):
   `docker build -t tridb/pg17-unfork:dev scripts/pg17/`
   Expected: image builds; `tjs_pg.c` compiles with **zero warnings** (the build echoes the gcc
   line; there must be no `warning:`/`error:` lines for tjs_pg.c).

2. Run the operator suite:
   `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_test.sql`
   Expected final lines:
   ```
   NOTICE:  PASS 3b: NULL required args raise cleanly (no backend crash)
   === tjs_pg (stock-PG fused operator, ADR-0019): ALL PASS ===
   [pg17_graph_test] done
   ```
   Every prior PASS (1, 2, 3, 4, 5, 6) must still print. The run must exit 0 (no
   `server closed the connection unexpectedly` — that string means a segfault survived).

3. Also verify on PG16: `docker build --build-arg PG_MAJOR=16 -t tridb/pg16-unfork:dev scripts/pg17/`
   then `bash scripts/pg17_graph_test.sh tridb/pg16-unfork:dev test/tjs_pg_test.sql` — same ALL PASS.

## Done criteria (machine-checkable)

- `grep -c 'PG_ARGISNULL(5)\|non-NULL' src/tjs_pg/tjs_pg.c` returns ≥ 1.
- The suite prints `PASS 3b` and `ALL PASS` on both PG16 and PG17 images and exits 0.

## Out of scope / do NOT touch

- Do NOT add `STRICT` to the SQL declaration — `src`/`edge_type` NULL is meaningful; STRICT would
  break the vector-first path (`src IS NULL`).
- Do NOT change the range checks for `k`/`hops`/`term_cond` or any operator logic.
- Do NOT touch the fork operator (`scripts/patches/tridb_tjs_open_operator.patch`).

## STOP conditions

- If the drift check shows `tjs_pg.c` args 5/6/7 already have NULL guards (someone fixed this
  first), STOP and report — nothing to do.
- If the build produces any warning in `tjs_pg.c` after your change, STOP and report the warning.
- If any pre-existing PASS (1–6) fails after your change, STOP — your guard is rejecting a valid
  call; report which PASS broke.

## Maintenance note

Any future arg added to `tjs_open` must be added to this guard (or explicitly documented as
nullable like `src`/`edge_type`). The arity of the guard must match the SQL signature — a reviewer
should check both move together.

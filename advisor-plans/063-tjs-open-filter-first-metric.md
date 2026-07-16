# Plan 063: `tjs_open` filter-first ranks by the index's actual distance metric, not a hardcoded L2

> **Executor instructions**: Follow step by step; run every verification and confirm the expected
> result before moving on. On a STOP condition, stop and report. When done, update this plan's row
> in `advisor-plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat a41b0c7..HEAD -- src/tjs_pg/tjs_pg.c test/tjs_pg_test.sql`
> If those changed, compare the "Current state" excerpt to live code first; mismatch = STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (independent of plan 062, but both touch `tjs_pg.c` — land 062 first if
  doing both, to keep diffs clean)
- **Category**: bug
- **Planned at**: commit `a41b0c7`, 2026-07-15

## Why this matters

`tjs_open` has two physical paths. The **vector-first** path correctly resolves the distance
function from the HNSW index's opclass (strategy-1 operator) and ranks by it. The **filter-first**
path (the Gate-B headline path) calls `find_hnsw_index()` to get the vector column but **throws away
the resolved distance operator** and hardcodes the L2 operator `<->` in its `ORDER BY`. For any
index built with a non-L2 opclass — `vector_cosine_ops` (`<=>`), `vector_ip_ops` (`<#>`) — the
filter-first path ranks the survivor set by the **wrong metric** and returns a wrong top-k. The two
paths of the same operator then disagree on ordering for identical inputs. It only happens to be
correct today because the Wikidata benchmark uses `vector_l2_ops` with L2-normalized embeddings
(where L2 order == cosine order) — but the operator is a shipped, general extension and must rank by
whatever metric the index was built for.

## Current state

- `src/tjs_pg/tjs_pg.c:340-358` — the filter-first branch discards the distance proc and hardcodes
  `<->`:
  ```c
  {
      Relation    heap = table_open(reloid, AccessShareLock);
      AttrNumber  vattno = InvalidAttrNumber;
      Oid         dp = InvalidOid;

      (void) find_hnsw_index(heap, &vattno, &dp);   // <-- dp (the distproc) is discarded
      vec_col = get_attname(reloid, vattno, false);
      table_close(heap, AccessShareLock);
  }
  ...
  appendStringInfo(&q, " ORDER BY e.%s <-> $1 LIMIT %d", qident(vec_col), k);  // <-- hardcoded <->
  ```
- The vector-first path already does this correctly — `find_hnsw_index` returns `distproc` (the
  opfamily strategy-1 function OID), see `tjs_pg.c:178-215` (`find_hnsw_index` resolves
  `distop = get_opfamily_member(opfamily, opcintype, opcintype, 1)` then `*distproc = get_opcode(distop)`).
  The operator OID (`distop`) is what we need to render into SQL — the function has it in hand
  before it takes `get_opcode`.

## Steps

1. Extend `find_hnsw_index` (`src/tjs_pg/tjs_pg.c:178`) to also return the strategy-1 **operator**
   OID (not just its opcode/proc). Its current signature is
   `static Oid find_hnsw_index(Relation heap, AttrNumber *vec_attno, Oid *distproc)`. Add an
   out-param `Oid *distop`:
   - Inside, it already computes `distop = get_opfamily_member(opfamily, opcintype, opcintype, 1)`.
     Set `*distop = distop;` alongside the existing `*distproc = get_opcode(distop);`.
   - Update the vector-first call site (`tjs_pg.c:~415`, `find_hnsw_index(heap, &vec_attno, &distproc)`)
     to pass a `&dummy_op` (or reuse — vector-first uses `distproc`, doesn't need the operator OID,
     so pass an `Oid ignore_op;` and `&ignore_op`).

2. In the filter-first branch (`tjs_pg.c:340-346`), capture the operator OID and render its
   qualified name into the `ORDER BY`:
   ```c
   Oid         distop = InvalidOid;
   /* ... */
   (void) find_hnsw_index(heap, &vattno, &dp, &distop);   // now also gets the operator OID
   vec_col = get_attname(reloid, vattno, false);
   /* build the operator's schema-qualified name for safe SQL rendering */
   {
       char *opname = generate_operator_name(distop, cfg /* both operand types = the vector type */);
       ...
   }
   ```
   The clean approach: use `format_operator(distop)` (from `utils/regproc.h`) which returns the
   operator's name as text suitable for SQL (e.g. `<=>` or schema-qualified). Then:
   ```c
   appendStringInfo(&q, " ORDER BY e.%s OPERATOR(%s) $1 LIMIT %d",
                    qident(vec_col), format_operator(distop), k);
   ```
   NOTE: `format_operator` may return a bare `<->` or a schema-qualified `OPERATOR(pg_catalog.<->)`
   form; test which renders correctly in the `ORDER BY OPERATOR(...)` syntax. If `format_operator`'s
   output already includes `OPERATOR(...)`, do not double-wrap. **Verify the exact rendering with a
   probe query in step 4 before finalizing the appendStringInfo format.**
   Add `#include "utils/regproc.h"` if not already present.

3. Move `vec_col`/`distop` capture so both physical paths resolve the metric the same way — the
   goal is that filter-first and vector-first, given the same index, rank by the *same* operator.

## Verification

1. `docker build -t tridb/pg17-unfork:dev scripts/pg17/` — compiles, zero warnings in `tjs_pg.c`.
2. Add a cosine-index assertion to `test/tjs_pg_test.sql`. After the existing corpus setup, add a
   second small table with a **cosine** index and assert filter-first ranks correctly:
   ```sql
   -- (1c) filter-first honors a non-L2 (cosine) index metric
   CREATE TABLE ent_cos (id bigint PRIMARY KEY, ts int, embedding vector(8));
   INSERT INTO ent_cos SELECT g, 100,
     (('[' || (g::float8/2000)::text || ',1,0,0,0,0,0,0]')::vector(8)) FROM generate_series(0,1999) g;
   CREATE INDEX ent_cos_hnsw ON ent_cos USING hnsw (embedding vector_cosine_ops)
     WITH (m=16, ef_construction=64);
   -- (reuse the same graph edges: hub 2 -> 1000..1100 already inserted on vids 0..1999)
   DO $$
   DECLARE got bigint[]; oracle bigint[];
   BEGIN
     SELECT array_agg(t) INTO got FROM tjs_open('ent_cos', 5, 0, 0, 2, 'id', '',
       (SELECT embedding FROM ent_cos WHERE id=1000), 2, current_setting('tjs.ptype')::int) AS t;
     SELECT array_agg(id) INTO oracle FROM (
       SELECT id FROM ent_cos WHERE id IN (
         SELECT dst FROM graph_store.gph_traverse_bfs(2, 2, current_setting('tjs.ptype')::int))
         AND id <> 2
       ORDER BY embedding <=> (SELECT embedding FROM ent_cos WHERE id=1000) LIMIT 5) q;
     IF got <> oracle THEN RAISE EXCEPTION 'cosine filter-first: got % expected %', got, oracle; END IF;
     RAISE NOTICE 'PASS 1c: filter-first ranks by the index cosine metric';
   END $$;
   ```
   This test **fails on the current hardcoded-`<->` code** (L2 order != cosine order for these
   non-normalized vectors) and passes after the fix — it is the regression guard.
3. `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_test.sql` → `PASS 1c` prints
   and `ALL PASS`; exit 0. Repeat on the PG16 image.
4. Rendering probe (do this DURING step 2 to pick the right `appendStringInfo` format): in a psql
   inside the image, run `SELECT format_operator(oid) FROM pg_operator WHERE oprname='<=>' LIMIT 1;`
   and confirm the `ORDER BY x OPERATOR(<that>) y` syntax parses.

## Done criteria

- `grep -c 'ORDER BY e.%s <->' src/tjs_pg/tjs_pg.c` returns **0** (the hardcoded operator is gone).
- The suite prints `PASS 1c` and `ALL PASS` on PG16 and PG17, exit 0.

## Out of scope / do NOT touch

- The vector-first path's ranking (already correct) — only make its `find_hnsw_index` call compile
  with the new signature.
- Any distance recompute logic (`FunctionCall2Coll`) — that path already uses the resolved proc.
- The fork operator.

## STOP conditions

- If `format_operator`/`OPERATOR(...)` rendering cannot be made to parse in `ORDER BY` after a
  reasonable attempt, STOP and report — do NOT fall back to hardcoding a different operator; the
  reviewer will decide between `format_operator` and passing the recomputed distance as a scalar
  expression instead.
- If the drift check shows the `<->` literal is already gone, STOP and report.

## Maintenance note

The two physical paths must resolve the metric identically. A reviewer should confirm both paths go
through `find_hnsw_index`'s resolved operator/proc and neither hardcodes an operator symbol.

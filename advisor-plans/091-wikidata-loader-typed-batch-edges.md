# Plan 091: Typed batched edge insert for the Wikidata engine loader

> **Executor instructions**: This adds a typed batch-insert API to the graph AM and uses it in the
> Wikidata loader. The audit finding's premise needs correction (read Current state): the existing
> batch API is UNTYPED, so the loader cannot "just use it". Preserve the loader's count assertion
> and edge semantics exactly. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/graph_store/graph_am.c src/graph_store/graph_store_am--0.1.0.sql tools/wikidata_engine_load.py tests/test_wikidata_engine_load.py test/`
> Plan 079 also edits `tools/wikidata_engine_load.py` — land after 079 and re-read the emit path.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (graph AM C + WAL path)
- **Depends on**: 079 (same loader file); 072 (trustworthy harness results)
- **Category**: performance / engine C
- **Planned at**: commit `a780b46`, 2026-07-16

## Why this matters

The Wikidata engine load inserts typed edges one scalar call at a time; each `gph_insert_edge`
locates the source vertex independently, so a large slice pays a per-edge locate + page pin/WAL
cycle the batched path already amortizes for the untyped case. Load time is the wall-clock gate on
every at-scale Wikidata measurement pass. The batched API (`gph_insert_edges`) exists but takes no
edge type, so the typed loader cannot use it — that API gap is the real work here.

## Current state (verified)

- `tools/wikidata_engine_load.py:291-298` (emitted SQL): stages edges via COPY into `edge_stage`,
  then
  ```sql
  SELECT graph_store.gph_insert_edge(e.src, e.dst, m.type_id)
  FROM edge_stage e JOIN etype_map m USING (pid)
  ORDER BY e.src
  ```
  inside a `DO $$` block that asserts the returned count equals the staged count.
- `src/graph_store/graph_store_am--0.1.0.sql:35` — `gph_insert_edges(bigint, bigint[]) RETURNS
  bigint` — one src, dst array, NO type parameter. C entry `graph_am.c:662`
  (`PG_FUNCTION_INFO_V1(gph_insert_edges)`). `:189` REVOKEs it from PUBLIC.
- The scalar `gph_insert_edge(src, dst, type_id)` is the only typed insert.
- The wiki-scale batch work (advisor 2026-07-09/10 batch) fixed the untyped path; typed edges were
  added by plan 038 (edge-type dictionary) without a batch variant.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stock PG17 engine | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/graph_typed_traversal_test.sql` (+ the suite you extend) | ALL PASS |
| Full stock | `make stock-graph-test` | all suites pass |
| Loader tests | `.venv/bin/pytest tests/test_wikidata_engine_load.py -q` | all pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `src/graph_store/graph_am.c` (typed batch insert)
- `src/graph_store/graph_store_am--0.1.0.sql` (new function decl + REVOKE, matching the untyped
  one; follow the established in-place-edit vs upgrade-script convention you find)
- `tools/wikidata_engine_load.py` (emit the batched form)
- `tests/test_wikidata_engine_load.py`, plus the engine SQL suite that covers typed inserts

**Out of scope**:
- Changing edge visibility/WAL semantics or on-disk layout.
- The loader's two-full-disk-pass residual (separate known [PERF] item).
- Fork patches; GX10 sign-off (the same C builds there — mark unbuilt-there if not run).

## Git workflow

Use assigned `dustin/dev-NNNN`. Suggested commits: `feat(graph): typed batched edge insert`,
`perf(bench): batch wikidata edge load`.

## Steps

### Step 1: Typed batch API with parity oracle

Add `gph_insert_edges(src bigint, dsts bigint[], type_id int) RETURNS bigint` (overload or a new
name if overload resolution against the untyped variant is ambiguous from SQL — check with a test
before committing to a name). Implementation mirrors the untyped batch (single locate of src,
amortized page/WAL handling) but stamps `type_id` exactly as the scalar typed insert does. REVOKE
from PUBLIC like its siblings. Extend the typed-traversal engine suite with a parity oracle: N
edges inserted via the batch == the same N via scalar calls, byte-identical under
`gph_traverse_typed` and edge counts, including tombstone interaction and rollback (batch in an
aborted txn leaves nothing visible).

**Verify**: extended suite ALL PASS on stock PG16 + PG17; rollback case proven.

### Step 2: Loader emits the batched form

Group `edge_stage` rows by `(src, type_id)` (the staged data is already `ORDER BY e.src`) and emit
the batch call per group — e.g. `SELECT sum(graph_store.gph_insert_edges(src, array_agg(dst ORDER
BY dst), type_id)) FROM ... GROUP BY src, type_id`, preserving the existing count assertion
(`n <> staged` still raises). Keep the `#WDL` marker lines unchanged (plan 079's parser depends on
them).

**Verify**: loader unit tests pass; emitted SQL inspected in a test (fixture asserting the batched
shape and the intact count assert + markers).

### Step 3: Measure honestly

On the local stock image with a small-but-nontrivial slice (whatever the existing loader tests /
fixtures support), record before/after load wall-clock in the commit message or a short note — a
local x86 number labeled as such, NOT a GX10/at-scale claim.

**Verify**: `make stock-graph-test`, `make test`, `make lint`, `git diff --check` all green.

## Test plan

Batch/scalar parity oracle (order, types, counts, traversal), abort atomicity, empty-array and
single-dst boundaries, loader SQL-shape fixture, marker stability, full stock suites both majors.

## Done criteria

- [ ] A typed batch insert exists with scalar-parity proven by an engine test on PG16 + PG17.
- [ ] The Wikidata loader emits batched typed inserts; count assertion and #WDL markers intact.
- [ ] A labeled local before/after load timing is recorded.
- [ ] Host + stock suites green; only in-scope files changed.

## STOP conditions

- Batch WAL records for typed edges would exceed GenericXLog's 4-buffer/record limit differently
  than the untyped path — report the constraint (known from the 009 spike) rather than redesigning.
- SQL overload of the untyped name is ambiguous and a rename debate is needed — pick the
  conservative new name (`gph_insert_edges_typed`) and note it.
- Plan 079 unmerged (same-file conflict).
- The parity oracle finds a scalar/batch divergence — that's a finding, report it.

## Maintenance notes

Every future insert-path variant needs the scalar-parity oracle pattern. If the loader's two-pass
disk residual is picked up later, it composes with this change (grouping happens in SQL, not in
the Python pass).

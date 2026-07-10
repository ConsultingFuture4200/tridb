# Plan 046: Reject tombstoned destinations in gph_insert_edges

> **Executor instructions**: Native graph C + AM SQL tests. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store/graph_am.c test/graph_store_am_test.sql`

## Status
- **Priority**: P1
- **Effort**: S–M
- **Risk**: LOW–MED
- **Depends on**: none
- **Category**: bug / correctness
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Contract: `gph_insert_edges` is **byte-identical** to N × `gph_insert_edge`. Scalar path locates
**both** endpoints and fails on tombstoned vertices. Batch path only checks `0 <= dst < gm_next_vid`
and dense-locates **src**. After `gph_tombstone_vertex(dst)`, batch still appends edges → phantom
adjacency from live sources.

## Current state

- Scalar (`graph_am.c:527-532` region): `gph_locate_vertex` on src and dst (tombstone → miss → ERROR).
- Batch (`:725-741`):
  ```c
  if (dsts[k] < 0 || (uint64) dsts[k] >= meta0.gm_next_vid)
      ereport(...);
  /* src dense locate only */
  if (!gph_locate_vertex_dense(rel, src, &meta0, ...))
      ereport(...);
  ```
- Tests: `test/graph_store_am_test.sql:65-122` happy path only.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Engine AM | `bash scripts/graph_am_test.sh` | PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** `gph_insert_edges` dst visibility checks; `test/graph_store_am_test.sql` (+ delete suite if easier).

**Out of scope:** changing dense bounds for sparse maps; multi-writer.

## Git workflow
- Branch: `advisor/046-batch-dst-tombstone`
- Commit: `fix(graph): gph_insert_edges rejects deleted dst (advisor 046)`

## Steps

### Step 1: Visibility check per dst

After bounds check, for each dst (or unique set for large arrays):

- Call `gph_locate_vertex_dense` (or visibility-only dense read) and ERROR if missing/tombstoned —
  same message class as scalar missing vertex.
- Keep O(1) dense path; do **not** fall back to silent skip.

Wiki bulk load (no deletes) stays fast: dense locate is one page per distinct dst if you dedupe;
for 1M unique dsts per batch rare — per-element dense locate is still O(1) each.

**Verify**: engine compile.

### Step 2: SQL tests

1. Insert vertices 0..4; tombstone vertex 2; `gph_insert_edges(0, ARRAY[1,2])` → ERROR; no edge to 2.
2. `gph_insert_edges(0, ARRAY[1,3])` succeeds; neighbors correct.
3. Existing multi-page + rollback tests still pass.

**Verify**: `graph_am_test.sh` PASS; `graph_delete_test.sh` PASS if touched.

## Test plan
- Cases above in AM SQL.

## Done criteria
- [ ] Batch parity with scalar on deleted/missing dst
- [ ] Wiki-scale load path (no tombstones) still works (no accidental dense ERROR on live dense graphs)
- [ ] Engine green
- [ ] Index DONE

## STOP conditions
- Dense locate ERROR on non-dense layouts during wiki load — do not break Wall-3; use dense only when layout is dense (same as src path).
- Performance of unique-dst hash at 39M edges becomes a wall — batch validate with metapage + sparse tombstone bitmap only if measured; report first.

## Maintenance notes
- Coordinate with plan 045 if both touch delete tests.

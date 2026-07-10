# Plan 045: Filter edge type in gph_tombstone_edge

> **Executor instructions**: Native graph C + SQL tests. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store test/graph_delete_test.sql test/graph_typed_traversal_test.sql`

## Status
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / correctness
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Docs say `gph_tombstone_edge` soft-deletes **related_to** edges. Implementation matches only
`es_dst_vid` — **no** `es_edge_type_id` filter — so typed edges (plan 038) between the same endpoints
are wiped by a related_to delete. Silent multi-type data loss after typed graphs land.

## Current state

```c
/* graph_am.c:1233-1236 — claims related_to */
/* graph_am.c:1197-1221 — match only dst, no type */
if (!match_all && s->es_dst_vid != match_dst)
    continue;
s->es_flags |= GPH_FLAG_DELETED;
s->es_xmax = xid;
```

- Typed insert writes `es_edge_type_id` (`graph_am.c` insert path).
- Default traversal filters `RELATED_TO` (`gs_getnext` type filter).
- Tests: `test/graph_delete_test.sql`, `test/graph_typed_traversal_test.sql`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Host | `make test && make lint` | exit 0 |
| Engine | `bash scripts/graph_delete_test.sh` + typed suite | PASS |

## Scope

**In scope:** `gph_tombstone_adjacency` / `gph_tombstone_edge` (+ SQL wrapper if signature changes),
`graph_store_am--0.1.0.sql`, `test/graph_delete_test.sql`.

**Out of scope:** reverse-index in-edge sweep (plan 056 territory); physical reclaim.

## Git workflow
- Branch: `advisor/045-typed-tombstone`
- Commit: `fix(graph): type-filter gph_tombstone_edge (advisor 045)`

## Steps

### Step 1: Add type match to tombstone walk

Pass `type_id` (default `GPH_EDGE_TYPE_RELATED_TO`). Skip slots where `es_edge_type_id != type_id`
unless an explicit “any type” sentinel is requested (optional second function or NULL type arg).

Keep `gph_tombstone_edge(src,dst)` defaulting to RELATED_TO for back-compat.

**Verify**: code review; compile on engine.

### Step 2: SQL surface

If needed: `gph_tombstone_edge(src, dst, type_id int default related_to)`. Keep GRANT/REVOKE like other mutators.

**Verify**: `\df graph_store.gph_tombstone*` in engine.

### Step 3: Tests

In `graph_delete_test.sql` or typed suite:

1. Insert related_to + typed edge same src/dst (if API allows).
2. Tombstone default related_to → typed edge still emitted by typed traversal; related_to gone.
3. Existing delete/rollback tests pass.

**Verify**: delete + typed harnesses PASS.

## Test plan
- New typed-delete cases; FR-7 rollback still holds for typed tombstone.

## Done criteria
- [ ] Default tombstone only affects RELATED_TO (or documented type arg)
- [ ] SQL tests cover typed survival
- [ ] Engine suites green
- [ ] Index DONE

## STOP conditions
- No way to insert two types between same endpoints in public API — still add type filter; test with internal insert if needed.
- Callers depended on all-type wipe — document migration; do not keep the bug.

## Maintenance notes
- Align with ADR-0016 typed traversal docs.

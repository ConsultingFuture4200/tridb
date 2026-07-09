# Plan 049: Hold Relation across gph_neighbors Next()

> **Executor instructions**: Native graph C; careful lock lifetime. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store/graph_am.c docs/decisions/0013-graph-store-v1-rewire.md`

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: preferably after or with plan 048 (shared `GraphScanDesc` edits)
- **Category**: performance
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

`gph_neighbors` (and siblings) call `gph_open_store` + `relation_close` on **every** `Next()`:

```c
/* graph_am.c:1556-1567 */
rel = gph_open_store(AccessShareLock);
if (gs_getnext(rel, scan, &elem)) {
    relation_close(rel, AccessShareLock);
    SRF_RETURN_NEXT(...);
}
relation_close(rel, AccessShareLock);
gs_close(scan);
```

ADR-0013 rider 3. High-degree wiki hubs pay RangeVar/lock overhead per edge on the TR-1 hot path
that already buffers page slots in memory.

## Current state

- Open already opens once then closes before Next loop (`:1545-1550`).
- Same pattern in `gph_neighbors_ext_cached`, `gph_traverse`, `gph_traverse_typed` (~1900+, ~2040+).
- `gs_getnext` needs a `Relation` for ReadBuffer.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Engine | `make graph-test` | ALL PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** SRF shells that re-open per Next; stash `Relation` (or reloid + one open) on scan/funcctx;
close only on done/error.

**Out of scope:** snapshot SI (plan 056); changing GenericXLog writers.

## Git workflow
- Branch: `advisor/049-hold-relation`
- Commit: `perf(graph): hold Relation across neighbor Next (advisor 049)`

## Steps

### Step 1: Extend scan / funcctx

Store `Relation rel` opened with `AccessShareLock` at FIRSTCALL; pass to all `gs_getnext` calls;
`relation_close` in SRF done path and any PG_CATCH if present.

### Step 2: Apply to all SRF shells

At least: `gph_neighbors`, `gph_neighbors_ext_cached`, `gph_traverse`, `gph_traverse_typed`.
Do not leave one path on the old pattern.

### Step 3: Error / interrupt safety

`CHECK_FOR_INTERRUPTS` mid-scan must not leak the relation — use PG_TRY/PG_CATCH or ensure
resource owner cleanup matches Postgres SRF conventions used elsewhere in the file.

**Verify**: engine suites; especially LIMIT early stop and empty scans.

## Test plan
- Existing traversal/early-term tests prove TR-1 still stops.
- No new leak tooling required; code review of close paths is mandatory.

## Done criteria
- [ ] No per-Next `gph_open_store` in the listed SRFs (`rg` check)
- [ ] Engine green
- [ ] Index DONE

## STOP conditions
- Holding AccessShareLock across user-visible wait is unacceptable under concurrent DDL — document
  single-writer contract still holds; if product needs concurrent DROP EXTENSION mid-scan, report.
- Memory context: Relation pointer invalid after context switch — fix before merge.

## Maintenance notes
- Reviewer: grep for remaining `gph_open_store` inside Next loops.

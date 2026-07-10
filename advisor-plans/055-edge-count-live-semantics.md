# Plan 055: Honest edge-count semantics after deletes

> **Executor instructions**: Graph C + legstats + docs/tests. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store src/planner/join_order_legstats.c test/`

## Status
- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none (coordinate with 040 if freeze reclaim lands later)
- **Category**: bug / tech-debt
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

Plan 037 deliberately does **not** decrement `gm_edge_count` on tombstone. `gph_edge_count()` still
documents “v1 has no edge-delete path so the counter only grows” (`graph_am.c:2146-2153`) — **false**.
`join_order_legstats.c:162-164` uses `gm_edge_count / gm_vertex_count` for `avg_out_degree` (EXPLAIN /
future cost). After deletes, degree is inflated; load asserts that treat the counter as live topology
are only valid for insert-only stores.

## Current state

```c
/* graph_am.c:1237-1240 — intentionally not decremented */
/* graph_am.c:2146-2153 — stale "no edge-delete path" comment */
```

```c
/* join_order_legstats.c:162-164 */
out->avg_out_degree =
    (float8) gmeta.gm_edge_count / (float8) gmeta.gm_vertex_count;
```

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Engine | `make graph-test` | PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** choose **one** of:

**Option A (preferred, smaller):** Document raw counter honestly; add `gph_visible_edge_count()` that
scans (or maintains a second counter); legstats uses visible or documents EXPLAIN-only raw caveat.

**Option B:** Maintain live counter on tombstone/insert under GenericXLog (harder abort accounting).

Default recommendation: **Option A** with clear naming.

**Out of scope:** physical compaction of tombstones; full SI.

## Git workflow
- Branch: `advisor/055-edge-count-semantics`
- Commit: `fix(graph): honest edge count after tombstones (advisor 055)`

## Steps

### Step 1: Fix comments + SQL docs

Immediately correct “no edge-delete path” language on `gph_edge_count`.

### Step 2: Implement Option A or B

If A: add visible count function + tests (insert 3, tombstone 1 → visible 2, raw 3).
If B: decrement on visible tombstone commit path with FR-7 tests.

### Step 3: Legstats

Point `avg_out_degree` at the live/visible metric **or** document that EXPLAIN fanout is raw-upper-bound
and freeze decision inputs that must not use it for correctness (comment already says FR-6 decision
does not use avg_out_degree — keep that true; still fix misleading EXPLAIN).

**Verify**: edge_count tests + join_order tests.

## Test plan
- Delete suite: raw vs visible after tombstone + rollback.

## Done criteria
- [ ] No false “no delete path” docs
- [ ] Callers that need live topology have a correct API
- [ ] Engine green
- [ ] Index DONE

## STOP conditions
- Visible scan at wiki scale too slow for EXPLAIN — cache on metapage with freeze reconcile; report design if needed.

## Maintenance notes
- Wiki load asserts on raw count remain valid for insert-only loads.

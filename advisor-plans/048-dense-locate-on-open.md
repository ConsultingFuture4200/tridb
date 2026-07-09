# Plan 048: Dense O(1) vertex locate on traversal open

> **Executor instructions**: Native graph C; engine-gated perf + parity. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store/graph_am.c test/ docs/decisions/0013-graph-store-v1-rewire.md`

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none (plan 049 can share scan work)
- **Category**: performance
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

`gph_locate_vertex` walks the full vertex-page chain — **O(V)** at position V. Dense O(1) locate
exists for **writes** (`gph_insert_edges`) but **every scan open** still uses the linear path
(`gs_open` → `gph_locate_vertex`). At V=1M–7M, multi-seed BFS / `tjs_open` expand is dominated by
locate, not edge page reads. ADR-0013 rider 1 names this unfinished.

## Current state

```c
/* graph_am.c:225-274 linear walk */
/* graph_am.c:295+ gph_locate_vertex_dense — hard-verify, ERROR on non-dense */
/* graph_am.c:1399 */
if (!gph_locate_vertex(rel, start, &vblk, &vslot, &src_rec))
    return false;
```

Dense precondition (from dense helper comments): vertices dense-in-order, no adj pages interleaved
before first edge (wiki bulk-load).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Engine | `make graph-test` | ALL PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** `gs_open` locate strategy; optional metapage flag if needed; parity tests under identity/dense load; brief ADR-0013 rider note.

**Out of scope:** holding Relation across Next (plan 049); CSR-lite migration.

## Git workflow
- Branch: `advisor/048-dense-locate-open`
- Commit: `perf(graph): dense locate on gs_open (advisor 048)`

## Steps

### Step 1: Prefer dense locate in gs_open

```c
gph_read_meta(rel, &meta);
if (/* dense eligible */) {
    if (gph_locate_vertex_dense(rel, start, &meta, &vblk, &vslot, &src_rec))
        goto opened;
    /* dense miss = absent/tombstoned → false (same as linear) */
    if (start < meta.gm_next_vid)
        return false; /* careful: distinguish layout ERROR vs miss — dense already ERRORs on layout */
}
/* fallback linear for non-dense layouts */
if (!gph_locate_vertex(...))
    return false;
```

**Safety**: never catch dense layout ERROR and fall back silently to wrong page — only fall back when
you **know** layout is non-dense (e.g. explicit meta flag, or first dense attempt not used). Preferred
approach: try dense only when `identity_mode` or a metapage `gm_dense_vertices` bit is set; otherwise
linear. Wiki load already uses dense inserts — set/clear flag accordingly if missing.

**Verify**: non-dense seed graphs (AM tests) still pass via linear path.

### Step 2: Parity + smoke

- Existing `graph_store_am_test`, traversal, typed, vid-cache tests pass.
- Optional: notice/counter for dense vs linear opens in debug builds only — skip if noisy.

**Verify**: full `make graph-test`.

### Step 3: Doc rider

One paragraph on ADR-0013 rider 1 “landed for open path when dense” or STATUS one-liner.

## Test plan
- No regression on sparse/map-based graphs that are non-dense.
- Dense identity load: open latency not required to assert numerically on CI; correctness only.

## Done criteria
- [ ] Dense graphs open via O(1) locate
- [ ] Non-dense still correct (linear)
- [ ] Never silent wrong-vid write/read
- [ ] Engine green
- [ ] Index DONE

## STOP conditions
- Cannot detect dense layout safely — implement opt-in GUC/meta flag only; do not guess.
- Dense path returns wrong neighbor set vs linear on same DB — STOP, fix before merge.

## Maintenance notes
- Plan 049 holds Relation; implement after or in same PR if small.
- Reviewer: hard-verify path must still ERROR on broken contiguity.

# Plan 047: gph_neighbors_ext_cached honors identity_mode

> **Executor instructions**: Native graph C + SQL parity tests. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- src/graph_store/graph_am.c src/graph_store/graph_store_am--0.1.0.sql test/graph_vid_cache_test.sql`

## Status
- **Priority**: P1
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: none (plan 057 benefits after this)
- **Category**: bug / correctness
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

SQL `gph_neighbors_ext` short-circuits both map directions when `identity_mode` is ON
(`graph_store_am--0.1.0.sql:276-286`). C `gph_neighbors_ext_cached` is documented as a
**byte-identical twin** but always SPI-looks up `gph_vid_map` and never reads identity_mode. Under
identity ON with empty/incomplete map, SQL returns full adjacency while cached path returns empty —
TJS/filter paths that SPI-call cached neighbors diverge from interactive SQL.

## Current state

```sql
-- graph_store_am--0.1.0.sql:277-286
SELECT CASE WHEN meta.identity_mode THEN n.nvid ELSE (SELECT m.ext_id ...) END
FROM gph_am_meta meta, gph_neighbors(CASE WHEN meta.identity_mode THEN src ELSE ...) ...
```

```c
/* graph_am.c ~1823+ : always SPI SELECT vid FROM gph_vid_map WHERE ext_id = ... */
/* no identity_mode read */
```

- Vid cache tests: `test/graph_vid_cache_test.sql` via `scripts/` AM wiring if present.
- Identity mode setter: `gph_set_identity_mode` in same SQL file.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Engine | `make graph-test` / vid-cache + AM tests | PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** `gph_neighbors_ext_cached` Open path; optional shared helper with SQL semantics;
`test/graph_vid_cache_test.sql` (or new SQL file wired into AM_TESTS).

**Out of scope:** switching `tjs_open` to cached (plan 057); densifying locate (plan 048).

## Git workflow
- Branch: `advisor/047-identity-cached`
- Commit: `fix(graph): identity_mode in gph_neighbors_ext_cached (advisor 047)`

## Steps

### Step 1: Read identity_mode once per Open

On FIRSTCALL, read `graph_store.gph_am_meta.identity_mode` (SPI or direct catalog — match existing
meta read patterns in graph_am.c). Cache the bool on the scan/funcctx.

### Step 2: Identity ON path

If identity_mode:

- Treat `src` as native vid (no map lookup).
- Emit neighbor **vids** as bigint (no reverse map/hash).
- Skip `gph_vid_cache_ensure` reverse path if unused.

If OFF: keep current map + cache behavior.

**Verify**: parity tests.

### Step 3: Parity SQL tests

1. Identity ON; empty `gph_vid_map`; insert dense vertices/edges;  
   `gph_neighbors_ext(x)` set-equals `gph_neighbors_ext_cached(x)`.
2. Identity OFF; populated map; same equality (existing oracle).
3. Identity ON with map rows still equals (ext_id == vid).

**Verify**: engine test PASS.

## Test plan
- Set equality oracles as above.

## Done criteria
- [ ] Cached path honors identity_mode
- [ ] Parity tests pass
- [ ] Index DONE

## STOP conditions
- Meta table missing in some installs — ERROR clearly; do not silent-empty.
- Comment claims “byte-identical” but SQL wrapper drifts — update both together.

## Maintenance notes
- Plan 057 should call this function from `tjs_open` after parity holds.

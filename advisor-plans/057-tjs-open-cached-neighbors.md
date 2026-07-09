# Plan 057: tjs_open expand uses gph_neighbors_ext_cached

> **Executor instructions**: Fork patch on tjs_open. Prefer after plan 047. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- scripts/patches/tridb_graph_vid_cache.patch scripts/patches/tridb_graph_v1_rewire.patch scripts/patches/tridb_tjs_open_operator.patch scripts/lib/msvbase_patches.sh`

## Status
- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: plan 047 (identity_mode parity on cached path)
- **Category**: performance
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

v1 rewire points multi-seed expand at SQL `gph_neighbors_ext`. Vid-cache patch upgrades **only**
`tjs` `graphReachableT` to `gph_neighbors_ext_cached`. Wiki fused path (`tjs_open`) still pays
uncached map lookups + SPI SRF overhead on every frontier node.

```diff
# tridb_graph_vid_cache.patch — tjs only
- gph_neighbors_ext(%lld)
+ gph_neighbors_ext_cached(%lld)

# tridb_graph_v1_rewire.patch expandMultiSeedO still:
LATERAL graph_store.gph_neighbors_ext(f.src)
```

## Current state

- `expandMultiSeedO` in `tridb_tjs_open_operator.patch` / post-rewire / batched BFS (plan 017)
- Sentinel style: `gph_neighbors_ext_cached` string in operator cpp

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patches | `bash scripts/ci_check_patches.sh` | exit 0 |
| Engine | `make graph-test` incl. `tjs_open_smoke` | PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** change expand SPI SQL to `gph_neighbors_ext_cached`; update sentinels; smoke set-equivalence still holds.

**Out of scope:** removing SPI entirely; dense locate (048) though complementary.

## Git workflow
- Branch: `advisor/057-tjs-open-cached`
- Commit: `perf(tjs_open): expand via gph_neighbors_ext_cached (advisor 057)`

## Steps

### Step 1: Patch expandMultiSeedO

Replace LATERAL `gph_neighbors_ext` with `gph_neighbors_ext_cached` in the open operator source
(via updating the governing patch file(s) and re-verify apply order).

### Step 2: Sentinels

Ensure `verify_patches` expects cached name in `tjs_open_operator.cpp`.

**Verify**: `ci_check_patches.sh` exit 0.

### Step 3: Engine smoke

`test/tjs_open_smoke.sql` batched BFS set-equivalence still passes; no empty expand under identity mode
(plan 047).

## Test plan
- Existing tjs_open smoke + arg guards.

## Done criteria
- [ ] `tjs_open` expand uses cached neighbors
- [ ] Patch CI + engine smoke green
- [ ] Index DONE

## STOP conditions
- SPI + cached function nesting deadlocks or snapshot issues (DEV-1236 class) — STOP and report stack.
- Plan 047 not merged and identity load returns empty — do not land expand switch alone on identity corpora.

## Maintenance notes
- Keep `tjs` and `tjs_open` on the same neighbor API going forward.

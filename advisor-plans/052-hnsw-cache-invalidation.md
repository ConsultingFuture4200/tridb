# Plan 052: Implement HNSW vector_index_map invalidation (ADR-0014 Option A)

> **Executor instructions**: Fork patch; engine-gated. Design is done (plan 023 / ADR-0014). Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- docs/decisions/0014-hnsw-index-cache-invalidation.md scripts/patches scripts/hnsw_stale_index_repro.sh test/hnsw_stale_index.sql`

## Status
- **Priority**: P2
- **Effort**: M–L
- **Risk**: MED
- **Depends on**: none (coordinate with plan 043 if both touch HNSW patches)
- **Category**: bug / correctness
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

MSVBASE caches HNSW graphs in a process-global `vector_index_map` keyed by index **name**, never
erased. After `DROP INDEX`+`CREATE` / `REINDEX`, a pooled backend serves a **stale** graph; dim change
can OOB-read (plan 019 class). ADR-0014 recommends Option A (relcache callback + re-key on relid) but
production code was deferred. Repro harness already exists.

## Current state

- ADR: `docs/decisions/0014-hnsw-index-cache-invalidation.md` — **read in full before coding**
- Repro: `scripts/hnsw_stale_index_repro.sh` + `test/hnsw_stale_index.sql` (scenarios A–D)
- Map lives in vendored HNSW sources; fix via **new** `scripts/patches/tridb_hnsw_index_cache_inval.patch`
- Rebuild-on-recovery patch composes: invalidation → miss → rebuild

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patches | `bash scripts/ci_check_patches.sh` | exit 0 |
| Repro | `bash scripts/hnsw_stale_index_repro.sh tridb/msvbase:dev` | B=42, C=99, D no crash |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope:** Option A implementation per ADR; re-key on relid; shared_ptr ownership rule (scan holds copy); wire patch; optional wire repro into AM_TESTS as non-fatal or assert mode; mark ADR Accepted when green.

**Out of scope:** plan 043 1M Sort hang (separate); changing ANN algorithm.

## Git workflow
- Branch: `advisor/052-hnsw-cache-inval`
- Commit: `fix(hnsw): relcache invalidation for vector_index_map (advisor 052)`

## Steps

### Step 1: Re-read ADR ownership rules

Mandatory: scan-open copies `shared_ptr` out of map; callback only `erase`s map entry.

### Step 2: Implement patch

- `CacheRegisterRelcacheCallback` in `_PG_init` / AM init
- On inval Oid: erase matching cache entries (InvalidOid → flush all)
- Re-key map on `relid` (and store relfilenode for debugging)

### Step 3: Sentinels + chain order

Register last-or-compatible in `msvbase_patches.sh`; unique verify greps.

**Verify**: `ci_check_patches.sh` exit 0.

### Step 4: Engine acceptance

Run `hnsw_stale_index_repro.sh` — scenarios B/C return fresh ids; D no OOB crash.

**Verify**: document results; set ADR status Accepted.

## Test plan
- Repro harness is the acceptance test.
- Full `make graph-test` non-regression.

## Done criteria
- [ ] Stale scenarios B/C fixed on engine
- [ ] shared_ptr rule followed (code comment + review)
- [ ] Patch chain CI green
- [ ] ADR-0014 status updated
- [ ] Index DONE

## STOP conditions
- Relcache callback cannot safely erase during scan — implement deferred erase list; do not free under scan.
- Conflicts with plan 043 patches — serialize or merge carefully.

## Maintenance notes
- DEV-1259 Phase C ownership note in ADR may need Linear link update.

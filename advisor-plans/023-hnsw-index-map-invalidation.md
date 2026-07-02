# Plan 023: Design cache-invalidation for the process-global HNSW in-RAM index map (stale/wrong-dimension reads after DROP/REINDEX/recreate) — design + spike

> **Executor instructions**: This is a DESIGN + SPIKE plan — the deliverable is an ADR + a measured
> reproduction, NOT a finished executor-side fix (the safe fix touches the hot scan path and belongs
> with DEV-1259). Follow step by step; run every verification command. Stop and report on any "STOP
> condition". Update `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `cd vendor/MSVBASE && grep -n "vector_index_map\|LoadIndex" src/hnswindex_scan.cpp src/hnswindex.cpp | head`

## Status

- **Priority**: P2
- **Effort**: M (design + spike; the eventual fix is L and hot-path-sensitive)
- **Risk**: MED for the spike; the fix it scopes is MED-HIGH (hot scan path)
- **Depends on**: relates to Linear DEV-1259 (HNSW durability Phase C) — coordinate, may merge
- **Category**: bug / architecture
- **Planned at**: commit `408e852`, 2026-07-01
- **Upstream**: microsoft/MSVBASE `src/hnswindex_scan.cpp` (`vector_index_map`, `LoadIndex`)

## Why this matters

MSVBASE caches each HNSW index's in-RAM graph in a **process-global `std::map`**
(`src/hnswindex_scan.cpp:27-28`), populated once per backend on a `LoadIndex` cache-*miss* (guard at
~line 113) and keyed on `DataDir/DatabasePath/RelationGetRelationName(index)`
(`src/hnswindex.cpp:208-210`). **No entry is ever erased.** Consequences a long-lived (pooled)
backend inherits:

- After `DROP INDEX` + `CREATE INDEX` reusing the same name, or `REINDEX`, the next scan
  cache-*hits* and serves the **stale** graph — wrong results.
- If the recreated index has a different `dimension` reloption, every probe then hits the OOB read
  from plan 019/UPCORE-01 (mismatched `dim`) — a crash, not just wrong data.
- The map key is the index *name*, so distinct-but-same-named indexes collide within one backend.
  (UPCORE-02)

TriDB's rebuild-on-recovery patch (DEV-1235) only fills on cache-*miss*, so it does **not** cover
this — a `tjs()`/`topk()` query against a re-created or reindexed vector index from a pooled
connection silently serves stale or wrong-dimension data. This is a correctness/robustness gap on
the exact hot path the product depends on, but the safe fix (relcache invalidation or a
generation/relfilenode check on the scan-open path) is delicate enough to warrant a design + a
repro before code — hence a spike, coordinated with the already-open DEV-1259 Phase C.

## Current state

- Cache structure: `static std::map<...> vector_index_map;` +
  `static std::map<...> distanceFunction_map;` (`src/hnswindex_scan.cpp:27-28`), process-global,
  never erased. Same shape in `src/index_scan.cpp:7`, `src/pase_hnswindex_scan.cpp:7`.
- Key construction: `src/hnswindex.cpp:208-210` builds the key from data dir + db path + index NAME.
- Load guard: `LoadIndex` returns early on a cache hit (`if (vector_index_map.find(p_path) != end)
  return;`, ~line 113) — so a stale entry is never refreshed.
- Correct invalidation primitives available on PG 13.4: `CacheRegisterRelcacheCallback` (fires on
  relcache invalidation incl. DROP/REINDEX), and `relfilenode` (changes on REINDEX/rewrite) as a
  freshness key; `rd_options` carries the reloption `dimension` to re-validate on hit.
- Related open work: DEV-1259 (HNSW durability Phase C: GenericXLog page durability + scan-time
  xmin/xmax visibility) is the natural home for a scan-open freshness check — this plan should feed
  it, not fork a parallel effort.
- Fix mechanism (for the eventual patch): a TriDB fork patch in `scripts/patches/` on the scan-open
  path, wired + sentinel'd. This plan produces the DESIGN + a repro harness, not that patch.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Python layer | `make test && make lint` | exit 0 |
| Repro harness (this plan creates it, gated) | `bash scripts/hnsw_stale_index_repro.sh tridb/msvbase:dev` | demonstrates stale/OOB read |
| Patch chain (if a spike patch is prototyped) | `bash scripts/ci_check_patches.sh` | exit 0 |

## Scope

**In scope** (deliverables):
- `docs/decisions/0014-hnsw-index-cache-invalidation.md` (create — the design/decision; renumber if
  0014 is taken by then)
- `scripts/hnsw_stale_index_repro.sh` + `test/hnsw_stale_index.sql` (create — the measured repro:
  pooled connection, DROP+CREATE same-name / REINDEX / dim-change, show stale-or-crash)
- Optionally a prototype patch `scripts/patches/tridb_hnsw_cache_invalidation.patch` marked
  DRAFT/unbuilt if the design converges — but the finished fix is deferred to DEV-1259 scope
- `advisor-plans/README.md` (status row)

**Out of scope**:
- Shipping the production invalidation fix (delicate hot-path change — belongs in DEV-1259 with full
  recall/latency validation).
- The SPTAG/PASE map siblings (`index_scan.cpp`, `pase_hnswindex_scan.cpp`) — SPTAG default-OFF; note
  them, don't fix.

## Git workflow

- Branch: `advisor/023-hnsw-cache-invalidation` from `origin/master`
- Commits: `docs(adr): 0014 HNSW index cache invalidation design (advisor plan 023)` etc.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Build the repro

Create `scripts/hnsw_stale_index_repro.sh` + `test/hnsw_stale_index.sql` (model the docker-exec
bootstrap on `scripts/crash_recovery_hnsw_test.sh`): on a SINGLE persistent connection (to keep one
backend, so the process-global map persists), (a) create an HNSW index, run a query (populates the
cache), (b) `DROP INDEX` + `CREATE INDEX` same name with DIFFERENT data, run the query again on the
same connection, and assert whether results are stale; (c) repeat with `REINDEX`; (d) repeat with a
recreated index of a different `dimension` and observe stale/crash. Capture what actually happens
per scenario.

**Verify**: `bash -n scripts/hnsw_stale_index_repro.sh` → exit 0; engine-gated live run if the image
exists (else commit the harness and mark "engine-gated: unbuilt here").

### Step 2: Write the design ADR

`docs/decisions/0014-...`: Context (the cache facts above, inlined), the repro results from Step 1,
and a Decision comparing the two viable fixes — (A) `CacheRegisterRelcacheCallback` that erases the
map entry on relcache invalidation for the index's relid; (B) a freshness check at scan-open that
compares the cached `relfilenode`/`dimension` against the live relation and rebuilds/evicts on
mismatch. Weigh cost on the hot path (B adds a per-scan-open compare; A adds a callback but keeps
the hot path clean), memory-safety of eviction (a shared graph in use by a concurrent scan must not
be freed underneath it — document the ownership/refcount requirement), and interaction with the
DEV-1235 rebuild-on-recovery path. Recommend one; hand the implementation to DEV-1259.

**Verify**: `ls docs/decisions/0014-*.md`; the ADR cites the Step 1 results.

### Step 3: Surface + link

Add a one-line note to `docs/STATUS.md` (dated) and a comment on Linear DEV-1259 (if the executor
has Linear access — otherwise note in the ADR) that the cache-invalidation gap is designed in
ADR-0014 and should be implemented within Phase C. Update this plan's row.

**Verify**: `grep -n "ADR-0014\|cache invalidation" docs/STATUS.md` → present.

## Test plan

- The Step 1 repro harness is the executable artifact; its per-scenario results go in the ADR and
  become the acceptance test for the eventual DEV-1259 fix.
- `make test && make lint` unchanged.

## Done criteria

- [ ] Repro harness committed (`bash -n` clean); results captured or "engine-gated: unbuilt here"
- [ ] ADR-0014 written with the two-option analysis + a recommendation + the ownership/refcount note
- [ ] STATUS note + DEV-1259 linkage added
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` row updated

## STOP conditions

- The repro shows the cache is actually invalidated correctly (a hidden callback exists) — then this
  is a non-finding; report that with the evidence and close the plan REJECTED.
- Building the repro requires more than one backend to observe (i.e. the map isn't actually process-
  global as read) — report the corrected mechanism before writing the ADR.

## Maintenance notes

- The production fix is deferred to DEV-1259 (Phase C) deliberately — it's a hot-path change needing
  recall/latency validation. This plan de-risks it with a design + repro.
- Reviewer: scrutinize the ownership/refcount reasoning — freeing an in-use shared HNSW graph is the
  way a naive eviction crashes concurrent scanners.
- The SPTAG/PASE map siblings carry the identical bug but are default-OFF (DEV-1228).

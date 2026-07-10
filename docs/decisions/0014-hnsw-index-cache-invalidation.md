# ADR-0014: Invalidate the process-global HNSW in-RAM index-map on DROP/REINDEX/recreate

**Status:** Option A PATCH AUTHORED (advisor plan 052, 2026-07-09) — apply/sentinel-verified via
`ci_check_patches.sh` (exit 0) on the x86 standin; **NOT yet Accepted**. The patch has not been
compiled or run: the live engine acceptance (`scripts/hnsw_stale_index_repro.sh` showing B=42,
C=99, D=7-with-no-crash) plus the recall/latency non-regression sweep are GX10+Docker-gated and
remain open for DEV-1259 Phase C. See "Implementation status (advisor plan 052)" below.
**Date:** 2026-07-02
**Issue:** advisor plan 023 (design + repro). Implementation home: DEV-1259 (HNSW durability Phase C).
**Relates to:** UPCORE-02 (index name as the cache key collides distinct-but-same-named indexes),
UP-CORE-12, plan 019 / UPCORE-01 (the dimension-mismatch OOB read this can trigger),
ADR-0009 (WAL durability; the DEV-1235 rebuild-on-recovery path this must not regress).
**Repro:** `scripts/hnsw_stale_index_repro.sh` + `test/hnsw_stale_index.sql`.
**Upstream:** microsoft/MSVBASE `src/hnswindex_scan.cpp` (`vector_index_map`, `LoadIndex`),
`src/hnswindex.cpp` (key construction).

## TL;DR

MSVBASE caches each HNSW index's in-RAM graph in a **process-global `std::map`**
(`src/hnswindex_scan.cpp:27-28`), populated once per backend on a `LoadIndex`
cache-**miss** (guard at ~line 113: `if (vector_index_map.find(p_path) != end) return;`)
and keyed on `DataDir/DatabasePath/RelationGetRelationName(index)`
(`src/hnswindex.cpp:208-210`). **No entry is ever erased.** A long-lived (pooled)
backend therefore serves a **stale** graph after `DROP INDEX`+`CREATE INDEX` (same name)
or `REINDEX`, and — if the recreated index has a different `dimension` reloption — a
**wrong-dimension** graph, which turns every probe into the plan-019/UPCORE-01
out-of-bounds read (a crash, not just wrong data). The DEV-1235 rebuild-on-recovery
patch only fills on cache-**miss**, so it does not cover this.

We recommend **Option A: a `CacheRegisterRelcacheCallback` that erases the map entry
for an index's relid on relcache invalidation** (which fires on DROP/REINDEX), keeping
the hot scan-open path untouched — paired with **eviction that hands ownership to a
`shared_ptr` released by the callback, so a concurrent scanner holding its own
`shared_ptr` keeps the old graph alive until it finishes** (never freed underneath a
running scan). The finished, recall/latency-validated implementation is handed to
DEV-1259 Phase C. This ADR records the decision and the measured repro; no production
code ships from this plan.

## Context

### The cache facts (code-verified at commit `408e852`)

- **Structure.** `static std::map<std::string, std::shared_ptr<hnswlib::HierarchicalNSW<float>>>
  HNSWIndexScan::vector_index_map;` plus a parallel `distanceFunction_map`
  (`src/hnswindex_scan.cpp:27-28`). Both are static class members — **process-global,
  one instance per backend**, never erased.
- **Population.** `HNSWIndexScan::LoadIndex(index, p_path, distance_method, dim)` returns
  early on a cache **hit** (`if (vector_index_map.find(p_path) != vector_index_map.end())
  return;`, ~line 113). On a miss it builds/loads the graph and stores it under `p_path`.
  Called from `hnsw_begin_scan`, the bulk-delete path, etc. (`src/hnswindex.cpp:211-240`).
- **Key.** `p_path = DataDir + "/" + DatabasePath + "/" + RelationGetRelationName(index)`
  (`src/hnswindex.cpp:208-210`). The key is the index **name** — so a dropped-and-recreated
  index, a reindexed one, and even a distinct-but-same-named index (UPCORE-02) all collide
  onto one cache slot.
- **No invalidation exists.** `grep` for `CacheRegisterRelcacheCallback`, `.erase`, and
  `relfilenode` across `src/*.cpp`/`src/*.h` returns **nothing** — there is no callback,
  no eviction, and no freshness key. (This closes plan 023's STOP condition #1: the cache
  is genuinely never invalidated; this is a real finding, not a hidden-callback false alarm.)
- **The map is process-global as read.** Because it is a static class member, a SINGLE
  backend suffices to observe the bug (plan 023 STOP condition #2 does not fire) — the repro
  runs all scenarios on one connection.

### Invalidation primitives available on PG 13.4

- `CacheRegisterRelcacheCallback(callback, arg)` — registers a process-lifetime callback
  invoked on **relcache invalidation**, which fires on `DROP INDEX`, `REINDEX`, and relation
  rewrites. The callback receives the invalidated relation's `Oid` (or `InvalidOid` to mean
  "flush everything").
- `relfilenode` — the physical file identity; **changes on REINDEX and any rewrite**, so it
  is a sound freshness key even when the index name is reused.
- `rd_options` — carries the `dimension` reloption, so a scan-open freshness check can also
  re-validate the cached graph's dimension before probing (defusing the OOB path directly).

### Interaction with DEV-1235 rebuild-on-recovery

DEV-1235 (ADR-0009) rebuilds the graph from the heap **on cache-miss** inside `LoadIndex`.
Invalidation composes cleanly with it: whichever option erases the stale entry, the very next
`LoadIndex` cache-misses and the existing rebuild path repopulates from the current heap. No
change to the rebuild logic is required — invalidation only has to guarantee the miss.

## Repro (Step 1 results)

`scripts/hnsw_stale_index_repro.sh` drives `test/hnsw_stale_index.sql` on a **single
persistent psql session** (one backend, so the process-global map persists) through four
scenarios, printing the id the HNSW scan returned next to the id a correctly-invalidated
index would return:

- **A** — initial build; query `[1,0,0,0]` → populates the cache (fresh = 1).
- **B** — `DROP INDEX` + `CREATE INDEX` same name with new data where id=42 is the exact
  match; fresh = 42. `returned_id != 42` ⇒ the stale scenario-A graph was served.
- **C** — `REINDEX` after moving the exact match onto id=99; fresh = 99. `returned_id != 99`
  ⇒ stale.
- **D** — recreate the same index name at `dimension=8` while the cached graph is still
  dim-4; the dim-8 query vector is read against a dim-4 space on the cache hit ⇒ the
  plan-019/UPCORE-01 **out-of-bounds read** — garbage distance or a backend crash. (Last on
  purpose: it may terminate the session.)

**Measured result: engine-gated: unbuilt here.** No `tridb/msvbase:dev` image exists on the
authoring box, so the live run was not executed. The harness is `bash -n`-clean and committed;
its per-scenario `returned_id` vs `fresh_id` output is the acceptance test for the DEV-1259
fix (fixed ⇒ B=42, C=99, D returns id=7 with no crash). Run on the engine via
`bash scripts/hnsw_stale_index_repro.sh tridb/msvbase:dev`.

## Decision

Two options are viable; both make the next scan cache-miss so DEV-1235's rebuild path refills
from the live heap.

### Option A — relcache callback that erases the map entry (RECOMMENDED)

Register `CacheRegisterRelcacheCallback` at backend start. On invalidation for an index's
relid (DROP/REINDEX/rewrite), **erase that index's `vector_index_map` / `distanceFunction_map`
entry**. Requires mapping the invalidated `Oid` → the string `p_path` key; keep a side map
`Oid → p_path` (or re-key the cache on `relfilenode`/`relid` so the callback needs no lookup —
which also fixes the UPCORE-02 name-collision, since `relid` is unique).

- **Hot path:** untouched. `hnsw_begin_scan`/`LoadIndex` keep their single `find()`; no
  per-scan-open comparison is added. This is the decisive advantage — the scan-open path is
  the exact hot path the product's `tjs()`/`topk()` depend on.
- **Cost:** one callback registration per backend + O(1) erase on the (rare) DDL event.
- **Caveat:** relcache callbacks fire on many invalidations; the callback must be cheap and
  must not `ereport(ERROR)` (it runs in invalidation-processing context).

### Option B — freshness check at scan-open

At scan-open, compare the cached graph's stored `relfilenode` **and** `dimension` against the
live relation; on mismatch, evict and rebuild.

- **Directly defuses the OOB:** the `dimension` compare rejects scenario D before probing.
- **Cost:** a per-scan-open compare on the hot path (cheap — two integer compares — but it is
  *on* the hot path, and TR-1's early-termination budget is measured in exactly this region).
  Requires storing `relfilenode`/`dimension` alongside each cached graph.
- **Weaker on UPCORE-02:** keyed on name, two same-named indexes still collide unless the key
  is also changed.

### Recommendation

**Adopt Option A**, and additionally **re-key the cache on `relid` (with `relfilenode` folded
into the stored value)** so UPCORE-02's name-collision is closed at the same time and the
callback needs no `Oid → path` side lookup. Keep the hot path free of per-scan comparisons.
Fold Option B's **`dimension` re-validation as a cheap defensive assert** only if the recall/
latency budget in DEV-1259 shows it is free — the callback already prevents the stale hit that
causes the OOB, so B's check is belt-and-suspenders, not the primary mechanism.

## Ownership / refcount requirement (the way a naive eviction crashes)

`vector_index_map` stores `shared_ptr<HierarchicalNSW<float>>`. A concurrent scanner obtains
the graph while holding the map slot. **The eviction must not free a graph that a running scan
is still traversing.** Mandatory rule for the DEV-1259 implementation:

- The scan-open path must **copy the `shared_ptr` out of the map into scan-local state** (bump
  the refcount) *before* it starts iterating, and hold it for the scan's lifetime.
- Eviction (Option A's callback or Option B's mismatch branch) must only **`erase` the map
  entry** — dropping the map's reference. The underlying `HierarchicalNSW` is then freed by
  `shared_ptr` **only when the last scanner's local copy is destroyed**, never underneath an
  in-flight probe.
- Do **not** call `saveIndex`/mutate the evicted graph from the callback; just release the map
  reference. The next `LoadIndex` cache-misses and rebuilds a fresh instance under a new slot.
- Concurrency around `vector_index_map` itself (insert/find/erase from the callback vs. a scan)
  must be serialized — today the map has no lock because it was append-only; adding `erase`
  introduces a writer, so a lock (or the existing per-backend single-threaded assumption, if
  Postgres guarantees the callback and scan cannot interleave within one backend) must be
  stated and enforced. **Reviewer: scrutinize this — a shared graph freed under a concurrent
  scanner is the naive-eviction crash.**

## Consequences

- **Positive:** DROP/REINDEX/recreate become correct on pooled connections; the dimension-
  change OOB (plan 019) is prevented at its source; UPCORE-02 name-collision closed if the
  cache is re-keyed on `relid`. The hot scan-open path stays free of per-scan work (Option A).
- **Negative / deferred:** the fix is hot-path C in vendored sources and needs recall/latency
  validation → it ships as a TriDB fork patch under `scripts/patches/` wired into
  `scripts/lib/msvbase_patches.sh` with a `verify_patches` sentinel, **within DEV-1259 Phase C**,
  not from this plan.
- **Siblings:** `src/index_scan.cpp:7` (SPTAG) and `src/pase_hnswindex_scan.cpp:7` (PASE) carry
  the identical process-global-map bug but are default-OFF (DEV-1228 / ADR-0004). Noted, not
  fixed here.

## Handoff to DEV-1259 (Phase C)

1. Implement Option A as a fork patch (relcache callback + `relid`-keyed cache + shared_ptr
   ownership rule above); optional Option-B `dimension` assert if the budget allows.
2. Wire the patch into `scripts/lib/msvbase_patches.sh` with a `verify_patches` sentinel.
3. Gate on `bash scripts/ci_check_patches.sh` (patch applies) + a live engine run of
   `scripts/hnsw_stale_index_repro.sh` showing B=42, C=99, D=7-with-no-crash, plus the
   existing recall/latency sweep to prove no hot-path regression.

## Implementation status (advisor plan 052, 2026-07-09)

Step 1 of the handoff above is done as a fork patch; steps 2 and half of 3 are done; the live
engine run in step 3 is **not**.

- **Patch:** `scripts/patches/tridb_hnsw_index_cache_inval.patch`. Implements Option A with the
  side-map variant the ADR explicitly sanctions ("keep a side map Oid -> p_path") rather than a
  full re-key of `vector_index_map`/`distanceFunction_map` to relid — smaller diff, same
  correctness for scenarios B/C/D (DROP+CREATE, REINDEX, dimension-change recreate). This does
  **not** additionally close UPCORE-02 (distinct-but-same-named indexes colliding); that would
  need the full re-key and is left for a follow-up if UPCORE-02 is prioritized separately.
  - `HNSWIndexScan::relid_to_path_map` (Oid -> p_path), populated in `LoadIndex`.
  - `HNSWCacheInvalCallback` registered once per backend via `CacheRegisterRelcacheCallback`
    (`HNSWIndexScan::RegisterInvalidationCallback()`, called from `lib.cpp`'s `_PG_init`). On a
    specific `relid` it erases only that relid's map entries; on `InvalidOid` it flushes all three
    maps. The callback only ever `.erase()`s — it never frees/mutates the graph directly.
  - Ownership rule implemented: `HNSWIndexScan::BeginScan` gained an optional out-param that
    copies the map's `shared_ptr` into caller-held storage (`WorkSpace::indexKeepalive` /
    `TridbVectorIter::indexKeepalive`) *before* constructing the `ResultIterator`, so an eviction
    racing a long-lived scan only drops the map's own reference — the graph is freed only once
    every keepalive copy is also released. All three `BeginScan` call sites (both branches of
    `hnsw_gettuple` in `hnswindex.cpp`, and `tridb_vec_open` in `tridb_vector_iter.cpp`) were
    updated to pass the out-param; the 2-arg call form still compiles (default `nullptr`) so no
    other `BeginScan` caller (SPANN/PASE, which have their own separate process-global-map bug,
    noted above as unfixed) needed changes.
- **Verified in this pass:** `bash scripts/ci_check_patches.sh` — clean-clones MSVBASE at the pin,
  applies the full fork-patch chain including this one, and greps all sentinels — **exit 0**.
  `git apply --check` of the patch also passes standalone against the post-chain tree. This proves
  the patch applies and the sentinels are load-bearing; it does **not** prove the C compiles or the
  fix behaves correctly at runtime.
- **NOT done (GX10/Docker-gated, no engine image available on the authoring box):** the patch has
  not been compiled; `scripts/hnsw_stale_index_repro.sh` has not been run against it. Status is
  therefore **not** "Accepted" — do not treat this as a shipped fix until DEV-1259 runs the live
  repro (B=42, C=99, D=7-with-no-crash) and the recall/latency non-regression sweep on the GX10.

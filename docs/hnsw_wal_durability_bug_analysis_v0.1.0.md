# HNSW vector index is not crash/abort-durable — root-cause analysis (DEV-1235)

**Status:** DIAGNOSIS + SPIKE. Fix is DRAFT/UNBUILT-HERE (vendored C++, GX10+Docker-gated).
**Issue:** DEV-1235 (spike). FR-7 gap on the vector leg.
**Companion:** ADR-0009 (`docs/decisions/0009-hnsw-wal-durability.md`),
draft patch `scripts/patches/hnsw_wal_durability.patch`.
**Confirms:** ADR-0003a KNOWN-LIMITATION #3 (the vendored HNSW index is not
abort/crash-durable for incremental inserts), with the precise mechanism.

## TL;DR

The vendored MSVBASE HNSW index keeps its entire graph in a **process-local C++
heap object** (`hnswlib::HierarchicalNSW`) and persists it to a **flat file via
raw `std::ofstream`**, completely outside PostgreSQL's storage manager and WAL.

- `aminsert` (`hnsw_insert`) mutates that in-memory object with `addPoint()` and
  **never writes anything to disk or WAL** — not even the flat file. The change
  lives only in malloc'd RAM.
- `ambulkdelete` (`hnsw_bulkdelete`) mutates the same object with `markDelete()`,
  also RAM-only.
- The only persistence is `saveIndex()` during `ambuild` (full rebuild).

Two distinct durability failures follow, both already observed in DEV-1166:

1. **ABORT-stress crash.** Because `addPoint()` mutates a shared C++ structure
   with no Postgres-managed undo and no per-tuple visibility, the index has no
   way to roll back an aborted insert; cumulative aborted inserts corrupt the
   in-memory graph and crash the backend after ~25–50 aborts.
2. **Crash loses data (no WAL redo).** After `pg_ctl stop -m immediate`, the
   in-memory graph evaporates. On restart there is no WAL record to redo, so the
   index reverts to whatever the last `saveIndex()` wrote (the `ambuild` snapshot)
   — every committed incremental insert since the last full build is lost from
   the index.

The fix must route every index mutation through the **same** PostgreSQL WAL the
native graph store already uses (`GenericXLog`), inside the **same** transaction
and process — never a second WAL (golden rule #2).

## Exactly which mutations skip WAL/GenericXLog

| AM entry point | Vendored impl | What it touches | WAL? |
|---|---|---|---|
| `ambuild` = `hnsw_build` (`hnswindex.cpp:119`) | `HNSWIndexBuilder::ConstructInternalBuilder` + `SaveIndex` (`hnswindex_builder.cpp:139` → `hnswalg.h:657 saveIndex`) | builds in RAM, then `std::ofstream` to a flat file at `DataDir/DatabasePath/<indexname>` | **No** — raw file IO, not a relation fork, not WAL-logged |
| `aminsert` = `hnsw_insert` (`hnswindex.cpp:148`) | `HNSWIndexScan::Insert` (`hnswindex_scan.cpp:58`) → `vector_index_map[path]->addPoint(...)` (`:87`) | mutates the cached `HierarchicalNSW` object in **process heap RAM** | **No** — no disk write at all, no `saveIndex`, no WAL |
| `ambulkdelete` = `hnsw_bulkdelete` (`hnswindex.cpp:179`) | `HNSWIndexScan::BulkDelete` (`hnswindex_scan.cpp:91`) → `markDelete(number)` (`:110`) | mutates the same in-RAM object | **No** |
| `amvacuumcleanup` = `hnsw_vacuumcleanup` (`hnswindex.cpp:450`) | no-op (`stats->num_pages = 0`) | nothing | n/a |

### The cache that makes the data loss observable

`HNSWIndexScan` holds two process-global maps keyed by the on-disk path
(`hnswindex_scan.cpp:12-13`):

```cpp
std::map<std::string, std::shared_ptr<hnswlib::SpaceInterface<float>>> distanceFunction_map;
std::map<std::string, std::shared_ptr<hnswlib::HierarchicalNSW<float>>>  vector_index_map;
```

`LoadIndex` (`:15`) reads the flat file **once** (`hnswalg.h:687 loadIndex`) and
caches the `HierarchicalNSW` for the life of the backend. Every subsequent
`addPoint`/`markDelete`/scan hits the cached object. Nothing ever flushes that
object back to the file after an insert. So:

- The flat file only ever reflects the **last `ambuild`**.
- Committed incremental inserts live **only** in RAM.
- A crash that skips a graceful shutdown loses every post-build insert, and
  because the index file is not a relation fork, the buffer manager and
  checkpointer never touch it and the WAL never describes it.

## How abort loses data (no undo, no visibility)

PostgreSQL has no undo. The native graph store (`graph_am.c`) handles this by
WAL-logging each page mutation through `GenericXLog` **and** filtering reads with
`gph_xmin_visible()` so an aborted insert's bytes are simply never visible
(`graph_am.c:65`). The HNSW index does neither:

- `addPoint()` links the new node into the navigable-small-world graph
  immediately and irreversibly. There is no xmin stamped on the node and no
  visibility filter on scan output (`hnsw_gettuple` returns whatever the
  in-memory graph yields).
- On ABORT, Postgres rolls back the heap tuple (its xid dies), but the index
  node is already woven into the graph with no hook to remove it. The aborted
  vector can still be returned as a nearest neighbor, and repeated aborted
  inserts accumulate orphaned/half-linked nodes.
- Empirically (DEV-1166, graph store absent) this corrupts the structure enough
  to crash the backend after ~25–50 aborted inserts — which is why the SM-5 loop
  had to cap the HNSW leg at a bounded abort budget (ADR-0003a, Test C2).

## How crash loses data (no redo)

`crash_recovery_assert.sql` already encodes the gap (lines 33-41, 51-59): the
committed-crash assertion deliberately reads the vector row via a **seqscan**
(`enable_indexscan=off`) because "the vendored HNSW INDEX itself does not redo".
The vector STORE's heap backing redoes from WAL (it is an ordinary heap tuple);
the **index** does not, because there is no WAL stream describing the index and
no rmgr/redo for the flat file. After restart the cached object is rebuilt from
the stale flat file via `LoadIndex`, so it answers the **pre-crash** nearest even
after a later CHECKPOINT.

## Root-cause statement (precise)

The HNSW index is a **non-transactional, non-WAL-logged sidecar store** that
happens to live in the Postgres process: its state is process-heap RAM persisted
by ad-hoc flat-file IO at build time only. It violates two TriDB invariants the
native graph store satisfies:

1. **Durability via the host WAL.** No index mutation is described by any WAL
   record, so crash redo cannot reconstruct post-build inserts. (FR-7 redo gap.)
2. **Atomicity via MVCC visibility.** No per-node xmin and no visibility filter,
   and the mutation is irreversible, so an aborted insert cannot be undone.
   (FR-7 abort gap, escalating to a crash under repeated aborts.)

This is a **vendored MSVBASE property**, reproduced with the graph store absent
(ADR-0003a) — not a TriDB graph-store defect. The reference for the fix already
exists in-tree: `src/graph_store/graph_am.c` does it correctly with `GenericXLog`
+ `gph_xmin_visible`.

## Why the obvious "easy fixes" are wrong

- **`saveIndex()` after every insert.** Still bypasses WAL (raw `std::ofstream`),
  is not crash-atomic (a crash mid-write leaves a torn file), is O(N) per insert,
  and gives no abort rollback. Rejected.
- **A second WAL / sidecar log for the index.** Directly violates golden rule #2
  (one WAL, one txn manager). Rejected.
- **fsync the flat file in the commit hook.** Not redo-able (no WAL), not
  abort-aware, and couples index durability to a non-relation file the
  checkpointer ignores. Rejected.

The only invariant-preserving fix backs the index with **relation-fork pages
mutated under `GenericXLog`**, exactly like the graph store. See ADR-0009 for the
chosen design and `scripts/patches/hnsw_wal_durability.patch` for the DRAFT.

## Verification plan (proves the fix; GX10 + Docker, Phase B)

All additions live in `test/crash_recovery_assert.sql` (the existing two-pass
harness driven by `scripts/crash_recovery_test.sh`, selected by `:phase`). The
test already CHECKPOINTs a baseline, runs a tri-store txn, then crashes with
`pg_ctl stop -m immediate` (SIGQUIT, no shutdown checkpoint) so committed page
changes exist ONLY in the WAL — restart forces `GenericXLog` redo. Today the
committed-crash branch reads the vector row via **seqscan** and documents the
index-redo gap (lines 33-41, 51-59). The fix lets us assert the **index** path.

The fix is proven when these assertions, which FAIL on the current vendored
index, PASS after Phase B:

1. **Committed-crash, INDEX redo (new — flips the documented gap).** In the
   `:phase = 'committed'` branch, after recovery, set `enable_seqscan = off` /
   `enable_indexscan = on` and assert the HNSW index returns id 5000 as the
   nearest neighbor of `ARRAY[5000,0,0,0,0,0,0,0]::float8[]`. Today this is
   deliberately NOT asserted because the index answers the pre-crash nearest.
   This is the single load-bearing assertion that the redo gap is closed.

2. **Incremental-insert crash durability (new test, no rebuild).** A txn does
   `CHECKPOINT` (snapshot the build), then COMMITs N incremental HNSW inserts of
   known vectors, then `pg_ctl stop -m immediate` BEFORE any `ambuild`/rebuild.
   After recovery, an index scan (`enable_seqscan=off`) must return all N
   committed vectors as their own nearest neighbors. Today all N are lost (the
   in-RAM graph evaporates and `LoadIndex` reads the stale build-time flat file).

3. **Uncommitted-crash, INDEX abort (extend the `:phase='uncommitted'` branch).**
   The doomed vector (id 6000) must NOT be returned by an **index** scan after
   recovery (currently only the seqscan/heap-backing absence is asserted). This
   proves the aborted insert left no durable index node.

4. **Abort-stress durability (new test, retires ADR-0003a Test C2 cap).** Run the
   SM-5 randomized COMMIT/ROLLBACK loop over the vector leg at the FULL 200-iter
   budget (not the bounded budget the vendored index forced). Assert: (a) the
   backend does not crash, and (b) the visible index nearest-neighbor set exactly
   equals the committed set (zero divergence). Today this crashes the backend
   after ~25-50 aborts.

5. **No second WAL (architectural assertion).** A grep guard in
   `scripts/crash_recovery_test.sh` confirms the index produced NO sidecar log
   file and NO flat-file write during the run (only the index relation's forks +
   the host WAL grew). This enforces golden rule #2 at test time.

6. **Wiring + sentinel checks.** When the patch graduates from DRAFT,
   `verify_patches()` in `scripts/lib/msvbase_patches.sh` greps for the sentinel
   `TRIDB: HNSW WAL durability (DEV-1235)` in `src/hnswindex_scan.cpp` and for
   `src/tridb_hnsw_wal.cpp` in `CMakeLists.txt` (the sketch is recorded in that
   file's DRAFT block). A missing sentinel must `die`, matching every other
   TriDB fork patch.

Until items 1-4 pass GREEN in Docker on the GX10, the fix is **UNBUILT** and no
"passes"/"durable"/"fixed" claim may be made.

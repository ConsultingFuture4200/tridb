# ADR-0009: Make the HNSW vector index crash/abort-durable via the host WAL (GenericXLog)

**Status:** Proposed (SPIKE / DRAFT — UNBUILT-HERE; vendored C++, GX10+Docker-gated)
**Date:** 2026-06-25
**Issue:** DEV-1235 (spike) — FR-7 gap on the vector leg.
**Relates to:** ADR-0003 (graph store v1-core, the reference WAL pattern),
ADR-0003a (recorded this exact gap as KNOWN-LIMITATION #3),
ADR-0004 (vector-index seam / decouple).
**Root cause:** `docs/hnsw_wal_durability_bug_analysis_v0.1.0.md`.
**Draft fix:** `scripts/patches/hnsw_wal_durability.patch` (+ wiring sketch into
`scripts/lib/msvbase_patches.sh`).

## TL;DR

The vendored HNSW index keeps its graph in process-heap RAM and persists it via a
flat file written by `ambuild` only; `aminsert`/`ambulkdelete` are RAM-only and
WAL-less. An abort-stress crash, or any immediate-stop crash, loses every
incremental insert and can corrupt the in-memory graph. We will make the index
durable by **mirroring the native graph store's pattern**: store the index's
authoritative bytes in the **index relation's own forks** and WAL-log every
mutation through **`GenericXLog`** — the SAME PostgreSQL WAL, the SAME
transaction, inside the SAME process. No second WAL, no sidecar, no flat file as
the source of truth.

This ADR records the decision and a phased design. The patch is a DRAFT against
vendored sources and is explicitly **not** built or verified here.

## Context

See the bug analysis for the precise mechanism. Summary of the invariant
violations:

- `hnsw_insert` → `addPoint` mutates `vector_index_map[path]` (a cached
  `HierarchicalNSW`) in RAM; nothing is WAL-logged or even flushed.
- Persistence is a raw `std::ofstream` flat file (`hnswalg.h::saveIndex`) written
  only at `ambuild`.
- No per-node xmin, no visibility filter, no undo → aborts are irreversible and
  accumulate into a crash.

Golden rules in play: TR-1 (don't regress early termination on scan), rule #2
(one WAL / one txn manager — the hard constraint here), rule #5 (vector store
stays the similarity leg; this is durability, not a new store).

## Decision

Adopt a **WAL-backed page store for the HNSW index, owned by the index relation
and logged through `GenericXLog`** — structurally the same technique ADR-0003
used for the graph store. Concretely:

1. **The index relation's main fork is the source of truth.** Stop treating the
   flat file as authoritative. The `HierarchicalNSW` byte regions
   (`data_level0_memory_`, the `linkLists_`, and the scalar header POD fields
   already enumerated by `saveIndex`) are laid into 32KB pages of the index
   relation, mutated under `GenericXLogStart` / `GenericXLogRegisterBuffer` /
   `GenericXLogFinish`, exactly like `gph_*` page writes.

2. **`aminsert` becomes WAL-logged and transactional.** Each `addPoint` that
   touches the level-0 block, a node's link list, and the enterpoint/maxlevel
   header registers the affected buffer(s) with `GenericXLog` and finishes inside
   the caller's transaction. The change is then redo-able from WAL and rolls back
   with the transaction (the registered buffers are part of the abort's WAL/redo
   accounting; uncommitted page images do not survive a crash-abort).

3. **Abort durability** is achieved the GraphAM way: version the touched index
   bytes with the inserting xid and filter on scan (`xmin`-visible). This is
   **mandatory, not deferrable**, and the spike must not pretend otherwise:

   > **Correction (Linus review):** `GenericXLog`'s register-and-finish does
   > **not** by itself give abort durability. Finish writes the WAL record and
   > leaves the shared buffer dirty with the mutated page image; a later
   > same-transaction abort does not undo that page mutation, and crash recovery
   > replays it. Worse, `addPoint` is irreversible at the `hnswlib` level — the
   > in-memory `HierarchicalNSW` still has the aborted node woven into its link
   > lists. So WAL-logging alone (the discarded "option (b)") fixes ONLY the
   > **crash-redo** gap (committed inserts survive restart); it does **nothing**
   > for the **abort-corruption** gap. Retiring ADR-0003a KNOWN-LIMITATION #3 and
   > passing the abort-stress test require the `xmin` visibility filter (option a)
   > **plus** rebuilding the in-memory structure to honor it. There is no valid
   > v1-scope shortcut here, unlike ADR-0003a's atomicity/isolation split.

   This ADR mandates that the mechanism is `GenericXLog` on the index relation,
   never a sidecar, **and** that abort isolation uses xid stamping + a scan-time
   visibility filter mirroring `gph_xmin_visible()`.

4. **`amvacuumcleanup` / `ambulkdelete`** route `markDelete` through the same
   `GenericXLog` page path so tombstones are durable and redo-able.

5. **No flat file as truth.** `ambuild` builds the structure and lays it into the
   relation forks under WAL (full-page images via `GENERIC_XLOG_FULL_IMAGE` for
   the initial layout), instead of `std::ofstream`. The flat file is removed from
   the durability path (it may survive only as an optional warm-start cache that
   is *validated against* the WAL-recovered relation, never trusted over it).

6. **Scan path (TR-1) is unchanged.** `hnsw_gettuple` / the relaxed-monotonicity
   iterator continue to read the in-memory working structure; durability is a
   write-path concern. We must ensure the working structure is faithfully
   reconstructed from the WAL-recovered relation pages on first scan after
   recovery (replacing today's "rebuild from stale flat file").

### Hard constraint (non-negotiable)

> Use the SAME Postgres WAL via `GenericXLog`, like the graph store — NEVER a
> second WAL. The index becomes a Postgres access method writing through the
> existing buffer manager + WAL, not a sidecar with its own log.

## Phasing

- **Phase A (this spike, DEV-1235):** diagnosis (done), ADR (this), DRAFT patch
  + wiring sketch + verification plan. No build.
- **Phase B (GX10, follow-on):** implement the `GenericXLog` page layer for the
  index relation; make `aminsert` WAL-logged; reconstruct the in-memory graph
  from recovered pages. This closes the **crash-redo** gap only — land the
  crash-recovery tests (below) GREEN in Docker on GX10.
- **Phase C (GX10, follow-on) — NOT optional:** per-node xid stamping + scan-time
  `xmin` visibility filter, and rebuild the in-memory `HierarchicalNSW` to honor
  it, so an aborted insert is invisible and uncorrupting. Only this closes the
  **abort-corruption** gap; only then can the abort budget cap be removed from
  the SM-5 loop (ADR-0003a Test C2) and KNOWN-LIMITATION #3 retired. Phase B
  alone does **not** suffice (see Decision §3 correction).

## Consequences

- **Positive:** the vector leg gains the same crash + abort durability the
  relational heap and native graph already have; FR-7 / SM-5 hold across all
  three stores without the bounded-abort caveat; the ADR-0003a KNOWN-LIMITATION
  #3 is retired (after Phase B/C ship on GX10).
- **Cost:** real C++ work inside vendored MSVBASE; per-insert WAL volume rises
  (acceptable — same as any WAL-logged AM). Build/verify is GX10-gated.
- **Risk:** the vendored `HierarchicalNSW` layout (`data_level0_memory_` +
  `linkLists_`) was designed for contiguous malloc, not paged storage; the page
  mapping is the hard part and must be proven on GX10. Until then this stays
  DRAFT/UNBUILT.

## What this ADR does NOT do

- It does not introduce a second WAL, a sidecar log, or a cross-system
  transaction. (Rule #2.)
- It does not change the query surface or add a query language. (Rule #4.)
- It does not claim the patch compiles or passes — that is GX10+Docker-gated and
  explicitly out of scope for this x86 standin spike.

---

## Addendum A — DEV-1235 Defect A: rebuild-from-heap (v1 interim fix, 2026-06-25)

**Status:** IMPLEMENTED — built, all four oracles pass on `tridb/msvbase:dev` (x86 Docker).
**Patch:** `scripts/patches/tridb_hnsw_rebuild_on_recovery.patch`
**Issue:** DEV-1235 Defect A (crash/cross-session/abort-exclusion/no-dup).

### What shipped

Instead of implementing the full GenericXLog page-store described above (Phase B/C,
GX10-gated), v1 adopts a simpler mechanism that achieves crash-redo correctness and
abort exclusion without touching the WAL-write path:

**`LoadIndex` now rebuilds the in-RAM `HierarchicalNSW` from the WAL-durable HEAP
on cache-miss, via `table_index_build_scan`**, instead of loading the stale flat file.

The heap is the source of truth — it always was, since the relational heap is
WAL-logged and MVCC-managed by PostgreSQL. The flat file is no longer in the load
path (it remains on disk as a dead artifact but is not read by `LoadIndex`).

### Mechanism

1. Cache guard: `vector_index_map.find(p_path)` short-circuits to avoid re-scanning.
2. `IndexGetRelation(RelationGetRelid(index), false)` → heap OID.
3. `table_open(heapOid, AccessShareLock)` → heap `Relation`.
4. `table_index_build_scan(heap, index, indexInfo, false, false, HNSWRebuildCallback, &state, NULL)`:
   - `scan=NULL` → heap AM creates its own `HeapScanDesc` with `SnapshotAny`.
   - `SnapshotAny` + `HeapTupleSatisfiesVacuum`: `HEAPTUPLE_DEAD` → `tupleIsAlive=false`
     → callback returns early → aborted rows excluded (Oracle C).
   - `HEAPTUPLE_LIVE`, `HEAPTUPLE_RECENTLY_DEAD`-with-alive-check, and
     `HEAPTUPLE_INSERT_IN_PROGRESS` (own-xact) → `tupleIsAlive=true` → `addPoint`.
5. `HNSWRebuildCallback` encodes each TID as `(blockId << 32) | offset` — identical
   to `hnsw_insert` — and calls `vector_index->addPoint`.
6. `table_close(heap, AccessShareLock)`.
7. Both `distanceFunction_map` and `vector_index_map` populated together so
   `hnswlib`'s raw pointer to the space object remains valid.

### Double-add policy (aminsert / own-transaction in-progress)

On the first insert in a fresh backend, `LoadIndex` runs before `aminsert`'s
`addPoint`. With `SnapshotAny` + `ii_Concurrent=false`, the in-progress row from
the current transaction is visible (`HEAPTUPLE_INSERT_IN_PROGRESS` → `tupleIsAlive=true`).
`HNSWRebuildCallback` calls `addPoint` for it; `aminsert` then calls `addPoint` again
with the same label.

Policy chosen: **(b) idempotency** — `hnswlib::HierarchicalNSW::addPoint` detects a
duplicate label in `label_lookup_` and calls `updatePoint` with the identical vector
data. No duplicate node is inserted; no corruption occurs. Verified by Oracle D.

### Oracle results (x86 Docker, `tridb/msvbase:dev`)

| Oracle | Scenario | Result |
|--------|----------|--------|
| A | Crash-immediate (no shutdown checkpoint), WAL-redo, HNSW scan must find post-checkpoint committed row | PASS — returns 9001 |
| B | Cross-session: row committed by session 1, HNSW scan in fresh session 2 must find it | PASS — returns 75 |
| C | Abort exclusion: rolled-back insert must NOT appear in HNSW scan results | PASS — 9999 absent |
| D | Recall / no-dup: 7 nearest-neighbour queries with pre-crash data return correct results | PASS — all 7 correct |

### Performance tradeoff

O(heap) rebuild at first access per backend per index. Subsequent accesses in the
same backend are O(1) from `vector_index_map` cache. The cost is proportional to table
size; for workloads with many small indexes and many backend restarts this is a regression
over the (broken) flat-file load. Accepted as the v1 tradeoff. Documented here so Phase B
(GenericXLog page store, GX10) can be prioritized when this cost becomes observable.

### What this addendum does NOT change

- The GenericXLog Phase B/C plan (Decision §§1-4) remains the long-term target.
  Addendum A is a correctness fix that closes the crash-redo and abort-exclusion gaps
  without GX10 work; it does NOT retire ADR-0003a KNOWN-LIMITATION #3 (that requires
  Phase C's xid-stamped scan-time visibility filter — abort durability in the WAL-write
  path, not just at rebuild time).
- The flat file produced by `ambuild` (`SaveIndex`) still exists; it is not deleted
  by this patch. It is simply no longer trusted or read during `LoadIndex`.
- The full GenericXLog route (WAL-log mutations at `aminsert` time) eliminates the
  per-backend O(heap) rebuild cost and is the correct v2 target.

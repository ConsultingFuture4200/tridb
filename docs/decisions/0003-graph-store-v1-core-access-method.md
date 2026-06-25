# ADR-0003: Graph store v1-core access method — page-level store via shared buffer manager + GenericXLog

**Status:** Accepted (2026-06-24)
**Issue:** DEV-1164
**Related:** ADR-0002 (adjacency-list layout), DEV-1163 (layout spec), DEV-1165 (iterator), DEV-1166 (FR-7)
**Supersedes for the graph backing:** the v0 heap-backed extension (`src/graph_store_ext/`)

## Context

DEV-1164 ("implement the adjacency-list access method") is estimate L. A literal reading is a
full PostgreSQL table access method: a `CREATE ACCESS METHOD ... HANDLER` returning a
`TableAmRoutine` with 40+ callbacks (sequential scan, executor tuple insert/update/delete,
bitmap scan, vacuum, analyze, TID fetch, …). For TriDB the graph store is **never** accessed as
`SELECT * FROM graph_table` — it is reached through the Open/Next/Close C iterator (DEV-1165)
and, later, the GRAPH_TABLE/PGQ surface. Most of the TableAM vtable would be dead weight.

The acceptance criteria are concrete and narrower than "implement a full TableAM":
registered/loadable, vertices+edges persist and **survive restart (WAL-backed)**, graph writes
**participate in transactions** (the FR-7 substrate), and the **seed graph loads**. The
anti-requirement is explicit: no private buffer pool, no separate persistence path.

## Decision

Implement DEV-1164 as a **native page-level store** that manages 32KB pages directly through
PostgreSQL's existing infrastructure, not as a TableAM handler:

1. **Storage = blocks of a container relation** (`graph_store.gstore`, autovacuum off, never
   accessed as a heap). This gives the shared buffer manager, the shared WAL, the
   checkpointer, and the relfilenode lifecycle for free — satisfying "use Postgres's shared
   buffer manager / write through the existing WAL" and the no-private-path anti-requirement.
2. **Page format** per `docs/graph_store_layout_v0.1.0.md` §2 (metapage, vertex pages,
   adjacency pages of packed fixed-size `GphEdgeSlot`s, chained on overflow), using standard
   `PageInit` + special area + `pd_lower`-tracked records so checksums/torn-page protection
   work unchanged.
3. **WAL via `GenericXLog`** (sanctioned by spec §5). Generic REDO replays our page diffs in
   the host stream — crash recovery and commit durability with **no custom rmgr**.
4. **Minimal MVCC visibility**: each record carries `xmin`; reads filter by
   current-txn-OR-committed. PostgreSQL has no undo, so without this an aborted INSERT's bytes
   would stay visible. This is what makes graph writes roll back atomically on ABORT and after
   a crash — the FR-7 substrate. Physical page allocation is race-safe (relation extension
   lock); the **logical** graph is single-writer for v1.

## Deferred (explicitly, to follow-up issues)

- The formal `CREATE ACCESS METHOD ... HANDLER` `TableAmRoutine` vtable.
- Secondary B-tree indexes (vertex/edge attribute, edge uniqueness) — spec §4.
- Property co-location / PropBlocks / overflow pages — spec §2.3-2.4.
- Per-tuple `xmin`/`xmax` with full cross-session **snapshot isolation** (the concurrent cases
  are DEV-1166); v1 covers commit/abort/crash only.
- A `vid → (block, slot)` index; v1 scans the vertex-page chain (O(V), fine at seed scale).
- Iterator perf: single-pass src+dst locate, caching the relation OID in the scan state.

## Consequences

- **All four DEV-1164 acceptance criteria are met and tested on the x86 standin**
  (`scripts/graph_am_test.sh`, `src/graph_store/graph_am.c`): registered/loadable
  (`CREATE EXTENSION graph_store_am`), persist + survive cluster restart (WAL recovery),
  transaction participation (ABORT rolls the graph write back), and bulk edge loading
  (1500-edge multi-page case stands in for the seed corpus loader, which calls the same
  `gph_insert_vertex`/`gph_insert_edge` entry points).
- **TR-1 holds at the storage level**: the iterator reads one adjacency page at a time, so a
  LIMIT above it stops before later chain pages are read — the property v0 (a single heap-array
  fetch in Open) could not provide.
- The iterator contract (`gph_neighbors` Open/Next/Close) is the surface DEV-1165 builds on and
  matches `src/graph_store/graphstore.h`.
- GX10: the code builds + passes on the x86 standin; the GX10 owns ARM64 build sign-off and the
  128 GB benchmark only. There is no second WAL and no second transaction manager.

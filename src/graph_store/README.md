# src/graph_store/ — native adjacency-list graph store (v1 core)

TriDB's native graph store: an adjacency-list topology store over 32KB pages, managed directly
through PostgreSQL's shared buffer manager and shared WAL (GenericXLog) — **not** relational join
tables, and **not** a sidecar (one process, one transaction manager, one WAL).

This is **real, compiled code**, built and tested on the x86 standin via `scripts/graph_am_test.sh`
(PGXS against the `tridb/msvbase:dev` image). It is architecture-independent PostgreSQL 13.4
access-method C; only the live GX10 ARM64 build and the 128GB benchmark remain hardware-gated.

> **Not to be confused with `src/graph_store_ext/`** — that is the superseded v0 heap-backed
> extension. **This** directory is the v1 custom 32KB-page store (DEV-1164 core + DEV-1165
> traversal). See ADR-0003 (AM core) and ADR-0005 (traversal iterator).

## Contents

| File | What it is |
| ---- | ---------- |
| `graph_am.c` | The implementation: page store, `gph_insert_vertex` / `gph_insert_edge` (WAL-logged via GenericXLog, MVCC-visibility-filtered reads), the shared `gs_open`/`gs_getnext`/`gs_close` traversal engine, and the `gph_neighbors` / `gph_traverse` SRFs + `gph_visits` / `gph_vertex_count` probes. |
| `gph_page.h` | On-disk 32KB page format (metapage, vertex pages, packed adjacency `GphEdgeSlot`s, chain pointers); static-asserts `BLCKSZ == 32768`. |
| `graphstore.h` | Shared types (`GraphVertexId`, `GraphElement`, kind/direction enums, constants) + the documented traversal contract. Compiled (included by `graph_am.c`). |
| `graph_store_am--0.1.0.sql` / `.control` | The `graph_store_am` extension: the `gstore` page container + the `gph_*` functions. |

## Invariants this store upholds

- **TR-1 — Volcano + early termination.** Traversal is `gs_open` / `gs_getnext` / `gs_close`;
  `gs_getnext` yields exactly **one** edge per call, reading at most one adjacency page, so an
  enclosing `LIMIT` stops before later chain pages are read. No blocking, no frontier
  materialization. (`gph_traverse` must be used in a target-list / `ProjectSet` position, not a
  FROM-clause `FunctionScan`, or `LIMIT` materializes and forfeits early termination.)
- **One process, one txn manager, one WAL.** Runs inside the forked Postgres backend, joins the
  current transaction, logs through the host's shared WAL. No second WAL, no cross-system txn.
- **Native adjacency list, not join tables.** Topology is a page-level store over **32KB** pages.
- **Single edge label for v1:** `:related_to` (entity → entity).

## Build & test

```bash
scripts/x86build.sh --docker     # produce tridb/msvbase:dev (once)
make graph-test                  # runs this store's suite (DEV-1164 core + DEV-1165 traversal)
                                 #   via scripts/graph_am_test.sh, then the tri-modal suites
```

## Specs and status

- **Layout contract (authoritative):** `docs/graph_store_layout_v0.1.0.md` (DEV-1163).
- **Decisions:** ADR-0002 (layout), ADR-0003 (v1 core AM), ADR-0005 (traversal iterator).
- **Build / gating status:** `docs/STATUS.md`. GX10 owns only ARM64 sign-off + the benchmark.

## Implementing issues

| Issue | Scope | Status |
| ----- | ----- | ------ |
| **DEV-1164** | Adjacency-list access method (page store over 32KB pages, WAL-backed). | core merged |
| **DEV-1165** | Graph traversal iterator (`gs_*` engine + `gph_traverse` edge SRF, one edge per Next, early termination). | this work |
| **DEV-1166** | Verify shared transaction manager (FR-7): atomicity under the host txn + concurrency audit. | pending |

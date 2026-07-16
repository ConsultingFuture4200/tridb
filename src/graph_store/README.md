# src/graph_store/ — native adjacency-list graph store (v1 core)

TriDB's native graph store: an adjacency-list topology store over BLCKSZ-derived pages, managed
directly through PostgreSQL's shared buffer manager and shared WAL (GenericXLog) — **not**
relational join tables, and **not** a sidecar (one process, one transaction manager, one WAL).

This is **real, compiled code** targeting **both** the PG 13.4 fork and **stock PostgreSQL 16/17**
access-method APIs (D2 un-fork). It builds and tests two ways off-GX10: PGXS against the fork
image (`scripts/graph_am_test.sh`, `tridb/msvbase:dev`) and as a plain extension on stock PG 16/17
+ pgvector (`scripts/pg17_graph_test.sh` / `make stock-graph-test`, CI job `stock-pg`, PG 16+17 —
see `docs/INSTALL_stock_pg.md`). Only the live GX10 ARM64 **fork** build sign-off and the 128GB
benchmark remain hardware-gated.

> **Not to be confused with `src/graph_store_ext/`** — that is the superseded v0 heap-backed
> extension. **This** directory is the v1 custom page store (DEV-1164 core + DEV-1165
> traversal). See ADR-0003 (AM core) and ADR-0005 (traversal iterator).

## Contents

| File | What it is |
| ---- | ---------- |
| `graph_am.c` | The implementation: page store, `gph_insert_vertex` / `gph_insert_edge` (WAL-logged via GenericXLog, MVCC-visibility-filtered reads), the shared `gs_open`/`gs_getnext`/`gs_close` traversal engine, and the `gph_neighbors` / `gph_traverse` SRFs + `gph_visits` / `gph_vertex_count` probes. |
| `gph_page.h` | On-disk page format (metapage, vertex pages, packed adjacency `GphEdgeSlot`s, chain pointers). Layout is BLCKSZ-derived; static-asserts `BLCKSZ >= 8192` (8KB works on stock PG, 32KB is the fork's high-degree performance target). |
| `graphstore.h` | Shared types (`GraphVertexId`, `GraphElement`, kind/direction enums, constants) + the documented traversal contract. Compiled (included by `graph_am.c`). |
| `graph_store_am--0.1.0.sql` / `.control` | The `graph_store_am` extension: the `gstore` page container + the `gph_*` functions. |

## Invariants this store upholds

- **TR-1 — Volcano + early termination.** Traversal is `gs_open` / `gs_getnext` / `gs_close`;
  `gs_getnext` yields exactly **one** edge per call, reading at most one adjacency page, so an
  enclosing `LIMIT` stops before later chain pages are read. No blocking, no frontier
  materialization. (`gph_traverse` must be used in a target-list / `ProjectSet` position, not a
  FROM-clause `FunctionScan`, or `LIMIT` materializes and forfeits early termination.)
- **One process, one txn manager, one WAL.** Runs inside the host Postgres backend (fork or
  stock), joins the current transaction, logs through the host's shared WAL. No second WAL, no
  cross-system txn.
- **Native adjacency list, not join tables.** Topology is a page-level store over BLCKSZ-derived
  pages (`BLCKSZ >= 8192`; 8KB on stock PG, 32KB on the fork).
- **Single edge label for v1:** `:related_to` (entity → entity).

## Build & test

```bash
# Fork image path (PG 13.4, 32KB pages)
scripts/x86build.sh --docker     # produce tridb/msvbase:dev (once)
make graph-test                  # runs this store's suite (DEV-1164 core + DEV-1165 traversal)
                                 #   via scripts/graph_am_test.sh, then the tri-modal suites

# Stock-PG path (PG 16/17 + pgvector, 8KB pages) — same suites, no fork
make stock-graph-test            # via scripts/pg17_graph_test.sh; CI job `stock-pg` (PG 16+17)
                                 #   see docs/INSTALL_stock_pg.md
```

## Specs and status

- **Layout contract (authoritative):** `docs/graph_store_layout_v0.1.0.md` (DEV-1163).
- **Decisions:** ADR-0002 (layout), ADR-0003 (v1 core AM), ADR-0005 (traversal iterator).
- **Build / gating status:** `docs/STATUS.md`. GX10 owns only the ARM64 fork sign-off + the benchmark.

## Implementing issues

| Issue | Scope | Status |
| ----- | ----- | ------ |
| **DEV-1164** | Adjacency-list access method (BLCKSZ-derived page store, WAL-backed). | core merged |
| **DEV-1165** | Graph traversal iterator (`gs_*` engine + `gph_traverse` edge SRF, one edge per Next, early termination). | this work |
| **DEV-1166** | Verify shared transaction manager (FR-7): atomicity under the host txn + concurrency audit. | pending |

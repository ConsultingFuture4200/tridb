# src/graph_store/ — native adjacency-list graph store (GX10-gated C surface)

This directory holds the **interface skeleton** for TriDB's native graph store:
the adjacency-list access method plus its Volcano traversal iterator, declared
against the PostgreSQL 13.4 access-method API.

> **Not to be confused with `src/graph_store_ext/`.** That is the *working* v0
> heap-backed extension (built and tested on the x86 standin, `scripts/graph_test.sh`
> green). **This** directory is the GX10-gated C *contract/skeleton* for the v1 custom
> 32KB-page access method (DEV-1164) — a known shape to implement against, never compiled here.

> **GX10-GATED: not built off-target.**
> Nothing here is compiled on the dev box. The implementing C must build against
> a live MSVBASE / PostgreSQL 13.4 fork built with `--with-blocksize=32` (32KB
> pages), which exists only on the GX10 target (ARM64 + CUDA, 128GB). On this
> repo the directory is a documented surface — opaque handles, typedefs, and
> per-function contracts — so the GX10 implementer drops in C against a known
> shape rather than designing from zero.

## Contents

| File | What it is |
| ---- | ---------- |
| `graphstore.h` | The access-method surface: opaque `GraphStore` / `GraphScanDesc` handles, `graphstore_open` / `graphstore_insert_vertex` / `graphstore_insert_edge`, and the `gs_open` / `gs_getnext` / `gs_close` traversal-iterator contract. Kept compilable-shaped (include guards, typedefs) but not built here. |

## Invariants this surface upholds

- **TR-1 — Volcano + early termination.** Traversal is `gs_open` / `gs_getnext`
  / `gs_close`; `gs_getnext` yields exactly **one** vertex-or-edge per call so
  the enclosing `ORDER BY <-> ... LIMIT 5` can stop early. No blocking, no
  frontier materialization.
- **One process, one txn manager, one WAL.** The access method runs inside the
  forked Postgres backend, joins the current transaction, and logs through the
  host's shared WAL. No second WAL, no cross-system transaction.
- **Native adjacency list, not join tables.** Topology is a B-tree access method
  over **32KB** pages — never relational join tables.
- **Single edge label for v1:** `:related_to` (entity → entity).

## Specs and status

- **Layout contract (authoritative):** `docs/graph_store_layout_v0.1.0.md`
  (32KB page layout, shared-WAL behavior, single `:related_to` edge). The
  constants in `graphstore.h` (`GRAPHSTORE_BLOCKSZ`, `GRAPHSTORE_EDGE_LABEL`)
  mirror that spec — keep them in sync.
- **Build / gating status:** `docs/STATUS.md`.

## Implementing issues

| Issue | Scope |
| ----- | ----- |
| **DEV-1164** | Adjacency-list access method (`graphstore_open` / `insert_vertex` / `insert_edge`, B-tree over 32KB pages). |
| **DEV-1165** | Graph traversal iterator (`gs_open` / `gs_getnext` / `gs_close`, one element per Next, early termination). |
| **DEV-1166** | Verify shared transaction manager (FR-7): no second WAL, atomicity under the host txn. |

## Handoff to GX10

1. Build the fork on GX10 via `scripts/gx10build.sh`.
2. Implement against `docs/graph_store_layout_v0.1.0.md`, binding `graphstore.h`
   to the PG 13.4 access-method headers listed in its include block.
3. Static-assert `GRAPHSTORE_BLOCKSZ` against the live `BLCKSZ`.

# src/graph_store_ext/ — working v0 graph store (heap-backed PG extension)

This is the **working v0** native graph-store extension: a PostgreSQL extension
(`graph_store.c` + `graph_store--0.1.0.sql`) that builds and tests on the x86_64 standin via
`scripts/graph_test.sh` (DEV-1165 Open/Next/Close traversal iterator, DEV-1166 FR-7 cross-store
atomicity). It is **heap-backed**, not the custom 32KB-page access method.

> **Not to be confused with `src/graph_store/`** — that is the GX10-gated C interface
> *skeleton* (`graphstore.h`) for the future v1 custom adjacency-list AM (DEV-1164), never
> compiled here.

- v0 scope and the deltas vs. the target design: `docs/graph_store_v0_limitations.md`.
- The future v1 custom-AM layout (the target, not what this implements):
  `docs/graph_store_layout_v0.1.0.md`.

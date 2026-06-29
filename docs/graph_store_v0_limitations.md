# graph_store v0 — known limitations (honest scope)

v0 (`src/graph_store_ext/`) is a **working, tested first increment** of the native graph
store: it proves the iterator contract and the shared-txn principle against the real
MSVBASE fork. It is **not** the moat yet. This doc records exactly what v0 does and does
NOT deliver — surfaced by the Linus verification loop (`graph-store-linus-loop` workflow).

## What v0 genuinely demonstrates
- `graph_store.neighbors(src)` is a Volcano Open/Next/Close iterator that **emits lazily**:
  a `LIMIT k` above it (target-list / `ProjectSet` form) emits ~k, not all N — so a top-k
  operator can stop without the iterator blocking. (TR-1 at the *emission* level.)
- A single transaction spanning the relational store + graph store commits and rolls back
  **atomically** — the shared-transaction-manager principle (FR-7), by construction of
  living in one Postgres process.
- Adjacency-list *access*: a vertex's out-neighbors are co-located in one tuple (`vid -> nbrs[]`)
  and walked by the C iterator — not an edge join table the planner joins.

## What v0 does NOT deliver (and which issue owns it)
1. **Storage-level early termination.** v0 reads the *entire* adjacency tuple in `Open()`
   (one heap fetch of a per-vertex array); the visit counter measures neighbors *emitted*,
   not neighbors *read*. True "examine <25% of corpus" (SM-3) requires the v1 custom 32KB-page
   access method reading adjacency incrementally via `amgettuple`. → **DEV-1164/1165**.
2. **FR-7 for the real store.** v0's atomicity is "free" because the backing is a heap
   relation. A custom access method does NOT inherit transactionality — it must implement
   `aminsert`/WAL (`XLogInsert`/`MarkBufferDirty`) + MVCC visibility. The v0 FR-7 test will
   still pass against the heap and would NOT catch a non-transactional custom AM. A v1 FR-7
   test must add **crash recovery / WAL replay** (SIGKILL the postmaster mid-commit, restart,
   verify both stores consistent) and concurrent-transaction isolation. → **DEV-1166**.
3. **Custom 32KB adjacency-page layout.** v0 uses a heap relation + Postgres `bigint[]`, not
   the page format in `docs/graph_store_layout_v0.1.0.md`. → **DEV-1163/1164**.
4. **Production iterator ≠ SRF.** FROM-clause SRFs are materialized by PostgreSQL
   (`ExecMakeTableFunctionResult`) and cannot early-terminate; only the target-list/`ProjectSet`
   form is lazy. The iterator that composes into the **TJS operator** (DEV-1169) must therefore
   be a custom-scan node or index-AM `amgettuple` path, not a userland SRF. → **DEV-1165**.

## v0 implementation caveats (smaller)
- `graph_store.add_edge` grows `nbrs[]` unbounded with **no dedup and no max-degree** — a
  high-degree vertex becomes one large toasted varlena, fully detoasted on each traversal.
- `graph_visit_counter` is a process-global static (per-backend); with a session pooler it
  accumulates across pooled logical sessions. Fine for the single-backend test; not a metric.
- Self-edges / parallel edges are permitted; no edge properties (single `:related_to`, per
  spec marker #3 for v1).

## Fixed during the loop
- **Use-after-free** (critical): `neighbors[]` was `palloc`'d in SPI's context (freed by
  `SPI_finish`); now allocated explicitly in `multi_call_memory_ctx`. Tests had masked it.
- Array decode now uses `get_typlenbyvalalign(INT8OID, ...)` instead of hardcoded byval args.

## Measurement quirk (noted 2026-06-29, not a defect)
- **`PERFORM ... FROM gph_neighbors(v) LIMIT k` inside a plpgsql loop does not early-stop the SRF.**
  plpgsql `PERFORM` does not propagate the outer `LIMIT` down to early-terminate a set-returning
  function the way a top-level `SELECT ... LIMIT k` does, so a benchmark written as `PERFORM 1 FROM
  gph_neighbors(0) LIMIT 5` reads the whole adjacency list instead of stopping at 5. This is standard
  PostgreSQL plpgsql behavior, **not** a defect in the graph scan (the iterator itself honors early
  termination — a direct `SELECT ... FROM gph_neighbors(v) LIMIT 5` reads only the pages needed). Use
  the direct-query form, not `PERFORM`, when measuring early-termination page reads.

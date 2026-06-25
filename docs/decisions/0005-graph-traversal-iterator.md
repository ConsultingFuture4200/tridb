# ADR-0005: Graph traversal iterator — shared Open/Next/Close engine + edge-emitting SRF

**Status:** Accepted (2026-06-25)
**Issue:** DEV-1165
**Related:** ADR-0003 (graph store v1 core AM), DEV-1164 (access method), DEV-1169 (TJS operator — the downstream consumer), spec §4.4 / TR-1

## Context

DEV-1164 already shipped a working, incremental, early-terminating traversal: `gph_neighbors(src)`
yields out-neighbor vids one per `Next()`, reading one adjacency page at a time so a `LIMIT k`
stops before later chain pages are read (`gph_visits()` proves `LIMIT 5 → 5 steps, not 1500`).
So DEV-1165 is not a greenfield iterator. The real gaps for the Phase-2 unified plan are:

1. **No edge emission.** `gph_neighbors` returns only the bare `dst` vid. The canonical query
   (spec §5) projects `src.embedding`, `dst.chunk`, `dst.timestamp` — it needs the *edge*
   `(src, dst)`, then joins `dst` back to its relational/vector payload.
2. **No shared engine.** The Open/Next/Close walk was inlined in the `gph_neighbors` SRF body,
   so a second consumer (the edge SRF, and the TJS operator's reachability probe) would have to
   duplicate it.
3. **The header lied.** `graphstore.h` declared the directory "GX10-GATED / not compiled here /
   interface skeleton only" with a handle-based API (`graphstore_open`, `gs_open(store, …)`) that
   was never implemented and does not match how the store actually works (it builds and passes on
   the x86 standin, per ADR-0003).
4. **The AM suites weren't wired into `make graph-test`**, and `scripts/graph_am_test.sh` masked
   `make` failures by piping to `tail` (a broken build looked green).

## Decision

1. **Factor one Open/Next/Close engine, shared by both SRFs.** Extract the walk into static
   `gs_open` / `gs_getnext` / `gs_close` over a `GraphScanDescData` cursor (`cur_blk`, `cur_slot`,
   `src`, `direction`). `gph_neighbors` and the new `gph_traverse` both drive it. The `Relation`
   is **caller-managed** (passed to `gs_getnext`, not retained by the scan) and **no buffer pin is
   held across `Next()` calls** — so the iterator is leak-free under early abandon (`LIMIT`), and
   the same engine serves both the per-call-reopen SRF model and a future direct-C caller.
2. **Add `gph_traverse(src) → TABLE(src, dst)`** — one `:related_to` edge per `Next()`. v1 edge
   slots (`GphEdgeSlot`) carry **no stored edge id**, so only `(src, dst)` are surfaced
   (`GraphElement.edge_id` is set to `GRAPHSTORE_INVALID_ID`). Property co-location is deferred
   (ADR-0003), so there is no on-page payload to point at — `payload = NULL`.
3. **De-gate and reconcile `graphstore.h`** to the implemented reality: keep the shared types
   (`GraphVertexId`, `GraphElement`, the kind/direction enums, constants), document the internal
   `gs_*` engine + the two SRFs, and mark the handle-based lifecycle/mutation API as deferred
   (v1 has no cross-extension C consumer).
4. **Wire `scripts/graph_am_test.sh` into `make graph-test`** and make it **fail loud** on a build
   error (redirect `make` to a log, `tail` + `exit 1` on nonzero) instead of masking it via `tail`.

### Why these three calls

**(a) Early termination is a property of SRF *placement*, not just the C loop.** A FROM-clause
`FunctionScan` is materialized to a tuplestore before `LIMIT` applies — which forfeits early
termination. Only a **target-list / `ProjectSet`** SRF stays pull-based so `LIMIT` halts `Next()`
mid-stream. The test asserts `gph_traverse(5) LIMIT 5 → 5 edge-steps` from a target-list position;
the header and SQL comments warn consumers (incl. DEV-1169) not to wrap it in a FROM clause. The
TJS operator reaches the graph leg via SPI over these SRFs (cross-`.so` C-linking the static engine
is not how PG extensions compose), and must keep the SRF in a pull-based position.

**(b) Emit the *edge*, join the payload.** The iterator emits `(src, dst)` — the minimal complete
topology element that exists on disk — and leaves chunk/timestamp/embedding to the relational and
vector legs joined on `dst` (exactly as the existing `trimodal_*` tests do). This keeps v1 honest
to the layout that exists rather than inventing a payload pointer the page format doesn't have.

**(c) Lenient SRF, strict C; cursor holds no pin.** `gs_open` returns `bool` (does `src` exist?)
and is policy-free: the SQL SRFs treat an absent source as an empty result (matching the existing
`neighbors(4)` = `{}` behavior), while a direct C consumer may raise. The cursor reuses the
existing `cur_blk`/`cur_slot` and holds no buffer pin across calls — preserving the leak-free
early-abandon property; we do **not** "optimize" by holding a pin or the relation across `Next()`.

## Consequences

- The canonical query's edge projection is now expressible (`gph_traverse`), and DEV-1169 has a
  single, documented traversal contract to consume via SPI.
- `gph_neighbors` behavior is byte-for-byte unchanged (same neighbors, same `LIMIT 5 → 5 visits`)
  — it now just delegates to the shared engine; the DEV-1164 suite passes unchanged.
- `make graph-test` now actually exercises the graph AM (DEV-1164 core + DEV-1165 traversal), and
  a broken build fails loudly instead of silently passing.
- **Not addressed here** (correctly): native multi-source `Open` (the LATERAL SQL pattern already
  covers multi-source; deferred until DEV-1169 demands a batched C set-source), and the concurrency
  audit / FR-7 cross-store proof (DEV-1166).

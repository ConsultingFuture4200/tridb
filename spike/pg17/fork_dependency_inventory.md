# Fork-dependency inventory (Plan 028, Step 1)

Spike artifact — NOT SHIPPED. Classifies every element of the TriDB patch chain and the three
PGXS extensions by what actually binds it to the MSVBASE PostgreSQL 13.4 fork.

Classification key:

- **fork-only mechanism** — depends on code the fork adds to PostgreSQL core or to the fork's
  vectordb module; has no stock-PG equivalent without re-implementation.
- **PG-13-API usage** — code written against PG 13 server APIs; portable in principle, may need
  mechanical updates for PG 17 headers (measured in Step 2, `compile_*.log`).
- **portable** — no fork dependency and no meaningful API drift expected.

## A. The base fork mechanism (MSVBASE `patch/Postgres.patch`)

Verified by reading `vendor/MSVBASE/patch/Postgres.patch` (read-only). The base patch modifies
PostgreSQL 13.4 core in exactly these places:

| File | What it adds |
|---|---|
| `src/include/access/amapi.h` | new `IndexAmRoutine` field `bool amcanrelaxedorderbyop` — the AM-API flag advertising relaxed (approximate) ORDER BY emission |
| `src/include/access/relscan.h` | new `IndexScanDescData` fields: `bool xs_inorder` (AM signals "my emission is now usably ordered") and `ItemPointerData xs_heaptid_orig` (pre-`index_fetch_heap` TID copy for parent operators, e.g. multicolumn top-k / TJS) |
| `src/include/nodes/execnodes.h` | new `EState` field `bool is_index_inorder` — executor-global channel from IndexScan to Sort |
| `src/include/nodes/pathnodes.h` | `IndexOptInfo.amcanrelaxedorderbyop` (planner copy of the AM flag) |
| `src/backend/optimizer/util/plancat.c` | copies `amcanrelaxedorderbyop` from `rd_indam` into `IndexOptInfo` |
| `src/backend/optimizer/plan/planner.c` | `create_ordered_paths()`: if the input path is an Index(Only)Scan whose AM sets `amcanrelaxedorderbyop`, force `is_sorted = false` so a bounded Sort node is placed above the relaxed scan |
| `src/backend/executor/nodeIndexscan.c` | `IndexNext`/`IndexNextWithReorder`: publish `scandesc->xs_inorder` into `estate->is_index_inorder`; DISABLE the stock `"index returned tuples in wrong order"` error (`cmp < 0`) so approximately-ordered emission survives reorder checking |
| `src/backend/executor/nodeSort.c` | `ExecSort()` input loop: `if (estate->is_index_inorder && tuplesort_heapfull(state)) break;` — the bounded-Sort early-stop that turns full sort into streaming top-k |
| `src/backend/utils/sort/tuplesort.c` + `utils/tuplesort.h` | new `tuplesort_heapfull()` (`memtupcount >= bound`) used by the nodeSort early-stop |
| `src/backend/access/index/indexam.c` | `index_getnext_slot()` saves `xs_heaptid_orig` before `index_fetch_heap` can clobber `xs_heaptid` |
| `src/backend/tcop/postgres.c` | MSVBASE query rewriter in the main loop (REMOVED by TriDB patch #12 below) |
| `src/pl/plpython/plpython.h` | build fix, incidental |

**PG-17 equivalent or absence:** none of the above exists in stock PG 17. `IndexAmRoutine` in PG 17
still has only `amcanorderbyop`; `IndexScanDescData` has no `xs_inorder`/`xs_heaptid_orig`; `EState`
has no `is_index_inorder`; nodeSort has no early-stop hook; and stock `IndexNextWithReorder` still
**errors** on out-of-order emission (`"index returned tuples in wrong order"`), which makes a
relaxed-emission index AM ILLEGAL on stock PG through the ORDER BY path. The stock-PG answer to the
same starvation problem is pgvector >= 0.8.0 `hnsw.iterative_scan` (`relaxed_order` /
`strict_order`), which keeps the approximation INSIDE the AM: the scan resumes and re-emits in
sorted-batch order instead of asking the executor to tolerate disorder (probed in Step 3).

## B. The 18 TriDB patches (`scripts/patches/`, applied on top of MSVBASE)

| # | Patch | Touches | Classification | Port note (stock PG 17 + pgvector) |
|---|---|---|---|---|
| 1 | `hnsw_wal_durability.patch` | fork vectordb (`hnswindex_scan.cpp`, new `tridb_hnsw_wal.*`) | **fork-only mechanism** | WAL-couples the fork's sidecar hnswlib index files. pgvector's HNSW is a real PG AM: WAL/crash-safety is native; the whole patch's problem class disappears. |
| 2 | `l2_distance_scalar.patch` | fork vectordb (`operator.cpp`) | **fork-only mechanism** (host module) | Scalar `<->` distance for the fork's operator. pgvector ships its own `<->` (`vector_l2_ops`); nothing to port. |
| 3 | `sptag_optional_build.patch` | fork build (`CMakeLists.txt`, `thirdparty/`, `sql/vectordb.sql`) | **fork-only mechanism** | Build-system surgery of the fork itself; meaningless outside it. |
| 4 | `tridb_fix_double_scan_snapshot.patch` | fork vectordb (`topk.cpp`, `multicol_topk.cpp`, `tjs_operator.cpp`) | **fork-only mechanism** (fix) + operator logic | Snapshot-lifecycle fix for fork operators (ADR-0010). The snapshot discipline carries over conceptually to any re-hosted operator; the code is fork-module code. |
| 5 | `tridb_hnsw_am_entry_guards.patch` | fork HNSW AM (`hnswindex*.cpp`, `operator.cpp`, `util.cpp`) | **fork-only mechanism** | Hardens the fork's HNSW AM entry points. pgvector has its own (mature) guards. |
| 6 | `tridb_hnsw_costestimate_no_orderby.patch` | fork HNSW AM (`hnswindex.cpp`) | **fork-only mechanism** | Costestimate fix for the fork AM. pgvector has its own costestimate. |
| 7 | `tridb_hnsw_rebuild_on_recovery.patch` | fork HNSW AM + `tridb_vector_iter.cpp` | **fork-only mechanism** | Rebuild-on-recovery for the fork's non-WAL sidecar index. Not needed on pgvector (WAL-logged AM). |
| 8 | `tridb_hnsw_reloptions.patch` | fork HNSW AM (builder/scan/lib) | **fork-only mechanism** | `ef_construction`/`M`/`ef_search` reloptions for the fork AM. pgvector already exposes equivalents (`m`, `ef_construction`, `hnsw.ef_search` GUC). |
| 9 | `tridb_hnsw_scan_no_orderby.patch` | fork HNSW AM (`hnswindex*.cpp`) | **fork-only mechanism** | Scan-path fix specific to the fork AM. |
| 10 | `tridb_neon_l2_distance.patch` | `hnswlib/space_l2.h` | **portable** (library-level) | ARM NEON L2 kernel inside hnswlib. pgvector has its own SIMD dispatch (incl. NEON); patch is only needed wherever hnswlib itself is kept. |
| 11 | `tridb_relaxed_order_executor_guard.patch` | **Postgres core** (`genam.c`, `nodeIndexscan.c`) + fork `hnswindex.cpp` | **fork-only mechanism** | Hardens the base-patch mechanism (zero-init `xs_inorder`; gate `is_index_inorder` and the restored wrong-order error on `amcanrelaxedorderbyop`). Exists only because the fork mechanism exists. |
| 12 | `tridb_remove_pgmain_rewriter.patch` | **Postgres core** (`tcop/postgres.c`) | **fork-only mechanism** (removal) | Removes MSVBASE's main-loop query rewriter. On stock PG there is nothing to remove. |
| 13 | `tridb_tjs_filter_first.patch` | fork vectordb (`tjs_operator.cpp`, `sql/vectordb.sql`) | operator logic, **PG-13-API usage** | Filter-first strategy is host-agnostic logic; code currently lives in the fork module and uses SPI + executor structs. |
| 14 | `tridb_tjs_open_operator.patch` | fork vectordb (new `tjs_open_operator.cpp`) | operator logic, **PG-13-API usage** + 2 fork fields | Reads `xs_orderbyvals[0]` (stock) and `xs_heaptid_orig` (**fork-added field** — port must re-derive the pre-fetch TID, see Step 3 gap list). |
| 15 | `tridb_tjs_operator.patch` | fork vectordb (new `tjs_operator.cpp`) | operator logic, **PG-13-API usage** + 2 fork fields | Same consumption contract as #14: per-tuple distance via `xs_orderbyvals[0]` (sole rank authority), TID via fork-only `xs_heaptid_orig`. |
| 16 | `tridb_tjs_predicate_termination.patch` | fork vectordb (`tjs_operator.cpp`) | operator logic, **portable** | The predicate-aware `term_cond` fix (DEV-1169) is pure operator logic; carries to any host verbatim in design. |
| 17 | `tridb_vector_index_seam.patch` | fork vectordb (`hnswindex_scan.cpp`, new `tridb_vector_index.hpp`) | **portable** (by design) | The backend-seam header (plan 025 lineage) — compile-time traits, no virtual dispatch. This is the intended porting seam for option B/C: implement the seam over pgvector's scan instead of hnswlib. |
| 18 | `tridb_vector_iter.patch` | fork vectordb (new `tridb_vector_iter.*`, probe) | operator logic, **PG-13-API usage** | The resumable ordered-candidate iterator the TJS body consumes. API is host-agnostic; the implementation binds to the fork's `HNSWIndexScan::BeginScan` internals. |

Count check: 18 patches classified. Fork-only mechanism: 10 (#1-9, 11, 12 minus overlaps — strictly
#1,2,3,5,6,7,8,9,11,12; #4 is a fork-module fix with portable discipline). Operator logic needing
re-host: #4 (partially), 13, 14, 15, 16, 18. Portable as-is: #10, 17.

## C. The 3 PGXS extensions (`src/`)

| Extension | LOC | Classification | Evidence |
|---|---|---|---|
| `src/graph_store` (`graph_am.c`, `gph_page.h`) | 909 + header | **PG-13-API usage** + one hard build constraint | Uses only stable, still-present-in-17 server APIs: `ReadBufferExtended`, `GenericXLogStart/RegisterBuffer/Finish` (unchanged since 9.6), `PageInit`, `PG_FUNCTION_INFO_V1`, SRF funcapi (`heap_form_tuple`). **Hard constraint:** `gph_page.h:27` `StaticAssertDecl(BLCKSZ == 32768, ...)` — stock PG 17 packages are 8KB; compile fails by design unless PG is self-built `--with-blocksize=32` (measured in Step 2). |
| `src/graph_store_ext` (`graph_store.c`) | 152 | **PG-13-API usage**, expected near-portable | SPI + ValuePerCall SRF + array utils only; no fork symbols, no BLCKSZ assumption. |
| `src/planner` (`join_order.c`, `join_order_legstats.c`) | 209 + 168 | **PG-13-API usage** | `join_order.c`: fmgr + GUC only. `join_order_legstats.c`: `relation_open`, `RangeVarGetRelid`, `ReadBufferExtended`, and includes `gph_page.h` → inherits the BLCKSZ==32768 static assert. |

## D. PG 13 → 17 symbol-drift watchlist for the graph AM / extensions

Candidate symbols actually used by the three extensions, checked against PG release-note API
history (PG 14-17). Measured ground truth is Step 2's compiler output; this table is the reading
pass:

| Symbol / area | Used in | 13→17 status |
|---|---|---|
| `GenericXLogStart/RegisterBuffer/Finish/Abort` | graph_am.c | unchanged (stable since 9.6) |
| `ReadBufferExtended(rel, MAIN_FORKNUM, P_NEW, RBM_NORMAL, NULL)` | graph_am.c, join_order_legstats.c | signature unchanged; NOTE PG 16+ deprecates `P_NEW`-style extension in favor of `ExtendBufferedRel()` — old form still compiles/works in 17 |
| `PageInit(page, BLCKSZ, special)` | graph_am.c | unchanged |
| `LockBuffer` / `UnlockReleaseBuffer` / `BufferGetPage` | graph_am.c, legstats | unchanged (`BufferGetPage` lost its snapshot-test variant pre-13; no further drift) |
| SRF multi-call (`SRF_FIRSTCALL_INIT`, `funcctx->tuple_desc`, `heap_form_tuple`) | graph_am.c, graph_store.c | unchanged |
| SPI (`SPI_connect/execute/getbinval`) | graph_store.c, tjs (fork side) | unchanged for these entry points |
| `relation_open` / `RangeVarGetRelid` | legstats | unchanged |
| `StaticAssertDecl` | gph_page.h | unchanged |
| `TupleDescAttr` | (fork operators) | PG 17: still a macro; becomes function-like in 18 — non-issue for 17 |
| `BLCKSZ` | gph_page.h, all page math | **environmental break**: stock packages = 8192; assert fires at compile |
| `shmem_request_hook` (PG 15+ requirement for RequestAddinShmemSpace) | none of the three | n/a — none uses shared memory |
| `MemoryContext` API deltas (PG 17 `MemoryContextReset` behavior, bump allocator) | none directly | additive in 17; no source change required |

## E. Measured compile results (Step 2, PG 17.10 / Debian, gcc 14.2, stock 8KB BLCKSZ)

| Extension | Errors | Warnings | Log |
|---|---|---|---|
| `src/graph_store_ext` | **0** | 0 | `compile_ext.log` — builds `graph_store.so` clean, unmodified |
| `src/graph_store` | **1** | 0 | `compile_graph_store.log` — the single error is `gph_page.h:27` `static assertion failed: "graph store requires --with-blocksize=32 (BLCKSZ 32768)"` |
| `src/planner` | **1** | 0 | `compile_planner.log` — same single error (inherits `gph_page.h` via `join_order_legstats.c`) |
| `src/graph_store` (assert neutralized, spike-only) | **0** | **0** | `compile_graph_store_noassert.log` — builds `graph_store_am.so` clean |
| `src/planner` (assert neutralized, spike-only) | **0** | **0** | `compile_planner_noassert.log` — builds clean |

Measured conclusion: PG 13 → 17 API drift for the three extensions is **zero** — every server API
they use (`GenericXLog*`, `ReadBufferExtended`, `PageInit`, SRF/SPI/fmgr, `relation_open`) compiles
unchanged and warning-free against PG 17 headers. The ONLY bind is the 32KB block size, and it is
an intentional compile-time gate (our own `StaticAssertDecl`), not an API incompatibility.

Compile vs runtime consequences of 8KB stock pages (assert removed): all page math is
BLCKSZ-derived, so the AM would RUN on 8KB pages, but capacity drops 4x — edge slots/page
`(BLCKSZ - SizeOfPageHeaderData - GPH_SPECIAL_SIZE)/32` goes from ~1022 (32KB) to ~254 (8KB),
quadrupling adjacency-chain page reads for high-degree vertices (the exact I/O the 32KB choice in
ADR-0002 exists to avoid). A stock-compatible port therefore either (a) accepts 8KB pages with a
measured traversal-I/O regression, or (b) requires a self-built PG 17 `--with-blocksize=32` — which
forfeits managed-Postgres compatibility, the main strategic reason to leave the fork (recorded per
the plan's STOP condition instead of performing the source build here).

**Bottom line (reading pass):** the three PGXS extensions have no fork-symbol dependency at all;
their only hard bind is `BLCKSZ == 32768`. Everything that is genuinely fork-only lives in (a) the
executor/AM-API relaxed-monotonicity mechanism and (b) the MSVBASE vectordb module hosting the HNSW
AM and the TJS operators. The TJS operators additionally consume one fork-added scan-desc field
(`xs_heaptid_orig`) beyond the stock `xs_orderbyvals`.

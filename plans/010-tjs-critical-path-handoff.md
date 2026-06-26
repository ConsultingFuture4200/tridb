# TJS critical-path handoff — DEV-1168 / 1166 / 1169 / 1167

Resumable execution plans for the four issues on the path to the TJS operator (the TriDB thesis),
produced by a parallel scoping sweep on 2026-06-25 and synthesized into one strategy. DEV-1165
(graph traversal iterator) is already shipped (PR #5); DEV-1228 (vector-index decouple) merged
(PR #4). Everything below builds + tests on the **x86 standin**; GX10/ARM is deferred.

## Cross-cutting synthesis (read first)

- **DEV-1167 and DEV-1169 are one engine.** Both extend `topk.cpp`'s `execFagins` (the existing
  Fagin-style streaming top-k with `consecutive_drops ≥ term_cond` early termination). Build **one**
  engine: DEV-1169 = the `tjs()` operator (a **vendor patch** into `vectordb.so` — `execFagins`
  depends on vectordb-private symbols `create_queryEnv`/`convert_array_to_vector_str`/direct
  `IndexScanState->iss_ScanDesc->xs_orderbyvals` access that are NOT exported to extensions, so it
  cannot be copied in-tree). DEV-1167 = the `GRAPH_TABLE(...)` front-door (**in-tree** parser) that
  lowers the canonical query into a `tjs(...)` call. ⇒ **build 1169 before 1167.**
- **Distance contract (hard):** scalar `<->`/`l2_distance` returns 0 outside an index scan, so the
  ONLY real per-candidate distance is the HNSW scan's `xs_orderbyvals[0]` (== `QueryResult::GetDistance()`).
  The vector leg is the **sole rank authority**; graph + relational legs are **predicates** on the
  single ordered stream (this also sidesteps order-merge). No SQL re-rank; exactness is proven
  empirically (≥99% parity vs a full-drain oracle), never via an executor recheck.
- **Top-k is approximate** — reuse VBASE's relaxed-monotonicity `consecutive_drops` stop; do NOT
  invent a new stop rule.
- **Build split:** in-tree PGXS (1166 tests, 1167 parser+SQL) vs vendor patch (1168, 1169). Vendor
  patches ship under `scripts/patches/` wired through `scripts/lib/msvbase_patches.sh` (sentinel-
  guarded, idempotent, `verify_patches` grep) because `vendor/MSVBASE/` is gitignored + re-cloned.
- **ADR numbering:** 1165→0005 (done), 1168→0006, 1169→0007, 1167→0008; 1166→an ADR-0003 addendum.
- **Recommended order:** 1168 → 1166 → 1169 → 1167. (1166 is test-only, no vendor rebuild — could
  go first as a fast win.)
- **Process per issue:** branch `dustin/dev-NNNN` from `origin/master` (local master goes stale
  after each remote merge — always branch from `origin/master`), implement in bounded increments,
  rebuild (`scripts/x86build.sh --docker` for vendor patches; `scripts/graph_am_test.sh` for the
  in-tree graph store), test, **Linus review before merge**, PR.

---

## DEV-1168 — HNSW relaxed-monotonicity Open/Next/Close iterator (FR-3) — VENDOR PATCH

**Finding:** ~60% already exists. `hnsw_gettuple` (`vendor/MSVBASE/src/hnswindex.cpp`) is a full
relaxed-monotonicity AM scan (Iterator A), and `HNSWIndexScan::BeginScan/GetNet/EndScan`
(`hnswindex_scan.cpp`) already drives `hnswlib::ResultIterator` exposing internal distance. The gap
(Iterator B): a **tridb-owned C iterator** the TJS operator can call **without** an `IndexScanDesc`,
that lifts the relaxed-monotonicity *stop* out of `hnsw_gettuple`'s hardcoded constants into a
caller-controlled bound, and surfaces internal distance per `Next()`.

**Files:** new `vendor/MSVBASE/src/tridb_vector_iter.{hpp,cpp}` (+ add to `CMakeLists.txt`
unconditional/hnswlib source list, NOT behind `WITH_SPTAG`); `scripts/patches/tridb_vector_iter.patch`
+ wire into `msvbase_patches.sh` (apply + `verify_patches` sentinel `tridb_vec_open`); optionally
extend `tridb_vector_index.hpp` seam with the Result shape; `test/vector_relaxed_mono_test.sql`;
`Makefile` ENGINE_TESTS; ADR-0006.

**API (extern "C"):** `tridb_vec_open(Relation index, const float *q, int dim, int k) → TridbVectorIter*`;
`tridb_vec_next(it, TridbVectorCand{ItemPointerData tid; float distance}*) → bool`;
`tridb_vec_set_kth_bound(it, float kth_best_global_distance)` (the **DEV-1169 seam** — TJS pushes its
current k-th-best *surviving* distance down so the iterator stops once the ANN stream can't beat it);
`tridb_vec_close(it)`. Maps onto `LoadIndex`→`BeginScan`→`GetNet`(decode `GetLabel()`→tid, `GetDistance()`)→`EndScan`.

**Increments:** (1) skeleton over the seam, distances approx-non-decreasing + examined <25% corpus;
(2) relaxed-mono stop + caller bound (lift `hnswindex.cpp` queue logic, make `distanceThreshold`/`queueThreshold` params not magic constants); (3) parity vs full-drain oracle ≥99%; (4) graph-leg
composition driver (per-vertex open); (5) ADR + Makefile + full `make test-all`.

**Hard decisions:** D1 lift the k-queue stop but parametrize k + inversion tolerance; D2 keep
sufficiency upstream via `set_kth_bound`, internal k-queue as fallback; D3 internal distance is
authoritative (no recheck), exactness = empirical parity gate.

**Blocker/watch:** add `tridb_vector_iter.cpp` to the UNCONDITIONAL source set; derive L2 vs IP from
`hnsw_ParaGetDistmethod`; no SPTAG/ARM/SIMD added.

---

## DEV-1166 — Verify single shared txn manager across all three stores (FR-7) — IN-TREE, TEST-ONLY

**Finding (important):** the existing "PASS FR-7" tests (`test/graph_store_test.sql`) exercise the
**v0 heap-backed** `graph_store_ext` (atomic for free), NOT the v1 native AM. The v1 AM
(`graph_store_am`, `gph_insert_*`) has only a single-store rollback assertion. So FR-7 is **not yet
proven on the keystone.** The concurrency audit found **7 items but ZERO required code fixes** — all
by-design/deferred. FR-7 = **atomicity (SM-5), not isolation** — the suite must say so.

**Concurrency audit (graph_am.c), all verify-and-document, no fix:** C1 `RowExclusiveLock` doesn't
serialize writers (single-writer is convention); C2 same-vertex concurrent first-edge mostly closed
by the under-lock re-read; C3 `gm_next_vid` is non-transactional (vid gaps, but visibility correct —
assert post-rollback vid not reused); C4 ext-lock nested in metapage buf lock — no deadlock cycle
(document lock order); C5 locate→lock TOCTOU mitigated by re-read; **C6 no snapshot isolation**
(commit visible to an already-open txn — `gph_xmin_visible` has no snapshot check; ADR-0003 defers) —
the suite must assert only the TRUE property (uncommitted/aborted invisible), NOT snapshot stability;
C7 none.

**Files (all in-tree):** `test/txn_atomicity_test.sql` (true tri-store COMMIT/ROLLBACK across
relational + HNSW index + graph AM in one txn; in-txn self-visibility; C3 vid-gap; SM-5 randomized
single-session commit/rollback loop with an expected-set oracle, assert exact equality across all
three stores); `test/crash_recovery_test.sh` + `test/crash_recovery_assert.sql` (replace the clean
`pg_ctl restart` with `stop -m immediate`/`kill -9`: CHECKPOINT baseline, commit a tri-store txn,
crash before checkpoint, restart → committed row present in all three = WAL redo proven; second
scenario: uncommitted-then-crash → nothing visible); `test/graph_concurrency_test.sh` (two psql
sessions with an advisory-lock sync point: uncommitted-invisible, commit-then-visible, aborted-
invisible, same-vertex edge boundary probe labeled KNOWN-LIMITATION); ADR-0003 addendum (lock order
C4 + FR-7=atomicity-not-isolation + file C6 as the snapshot-isolation follow-on). x86-buildable now
(docker supports immediate-stop/kill + restart against the persistent `$D`).

---

## DEV-1169 — TJS operator (Traversal-Join-Similarity) — VENDOR PATCH — the URGENT keystone

**Architecture:** a C **SRF** (`tjs(...)`, registered like `topk`), body = a generalized `execFagins`
(`TJSState`). NOT a CustomScan (couples to the unfinished parser + planner hooks; the SRF reuses
`execFagins` verbatim and is testable standalone). NOT SQL nesting (the `trimodal_*` shape gives only
pipeline-level early termination — issue anti-requirement). Structure `execTJS` as a pure
`TupleTableSlot* execTJS(PlanState*)` so a later CustomScan reuses it as `ExecCustomScan`.

**Legs:** A (vector) = the HNSW `IndexScanState` built via SPI w/ `enable_seqscan=off`, distance from
`iss_ScanDesc->xs_orderbyvals[0]` (the only real distance) — the rank driver. B (graph) = reachability
**predicate** inserted at the seen-set check (v0 stub: `graph_store.neighbors`/`gph_traverse` via SPI;
v1: DEV-1165). C (relational) = filter pushed into leg A's SQL `WHERE`. Single global top-k + early
stop live in `execTJS` (bounded PQ size `ef`; `consecutive_drops ≥ term_cond` → finish → `Close()`
children, propagating the stop into the HNSW beam + graph cursor).

**Files:** new `vendor/MSVBASE/src/tjs_operator.cpp` (fork from `topk.cpp`) + `CMakeLists.txt`
`VECTORDB_SOURCES` + `sql/vectordb.sql` `CREATE FUNCTION tjs(...)`; all in
`scripts/patches/tridb_tjs_operator.patch` (sentinel `TRIDB: TJS operator`, wired + verified in
`msvbase_patches.sh`); `test/canonical_e2e_test.sql`; `scripts/tjs_test.sh`; `Makefile`; ADR-0007.

**Increments (land 0–2 TODAY against v0 stubs → FR-4 "single plan"):** 0 patch/build skeleton
(`tjs`==`multicol_topk` clone, prove patch/registration); 1 vector leg + relational filter (real HNSW
scan = DEV-1168 stand-in); 2 graph-reachability predicate (v0 `neighbors` = DEV-1165 stand-in) →
canonical query as one `tjs()` call matching `trimodal_compose.sql`; 3 swap in real DEV-1165/1168
iterators behind frozen call sites; 4 `tjs_candidates_examined()` counter + SM-3 (<25% corpus) + SM-4
(≥99% parity) + no-blocking assertion (first row before child EOF).

**Hard decisions:** D1 vector leg is sole rank authority, graph/relational are predicates; D2 reuse
VBASE `consecutive_drops` (over-provision `ef≥k`), prove empirically, claim approximate top-k; D3 SRF
now / CustomScan later (shared `execTJS` core).

**Dependency reality:** NOT hard-blocked — increments 0–2 use v0 surfaces. Final swap (inc 3) needs
DEV-1168 to expose per-`Next()` distance via stable API + DEV-1165 traversal callable in-backend
(both now satisfied). DEV-1164 is NOT a blocker for the vector-driven path.

---

## DEV-1167 — SQL/PGQ canonical-query surface — IN-TREE

**Recommendation:** NO grammar fork. Register `GRAPH_TABLE(matchspec text, columns text)` as a
**function** so stock PG13 parses the canonical text verbatim (it already treats it as a
`RangeFunction`); parse only the MATCH/COLUMNS payload in `src/parser/graph_table.c` with a
hand-written recursive-descent matcher for the ONE locked template (reject off-template → scope guard,
AC-3). Lower it into a `tjs(...)` call (DEV-1169's engine). A `gram.y` patch = the banned "new query
language" + fragile under re-clone.

**Files (in-tree):** `src/parser/graph_table.c` (payload parser + lowering); fold the SQL surface +
scope-guard wrappers into the `graph_store` extension SQL (avoid a third extension);
`test/parse_canonical.sql`; update `docs/sqlpgq_logical_plan_v0.1.0.md`; ADR-0008. Touch vendor only
if a needed symbol isn't header-exported (then a 1-line `extern` patch, not logic).

**Single plan shape:** `GRAPH_TABLE(...)` → `tjs(k, term_cond, ef)` over a driver `SELECT dst.chunk,
(src.embedding <-> :q) FROM <graph-restricted set> WHERE dst.timestamp IN :window ORDER BY
src.embedding <-> :q` — graph leg as LATERAL-under-driver (v0) / DEV-1165 amgettuple (v1), NEVER a
top-level FROM-list SRF (materializes → kills TR-1).

**Increments:** 1 canonical-text parse + scope guard (zero runtime deps, buildable now); 2 lowering +
EXPLAIN proves 3-leg single plan (AC-2); 3 wire to `tjs()` → correct top-k, filter load-bearing
(reuse `trimodal_compose.sql:63-80` proof: stale exact-match entity 40 dropped → returns 30 with
filter, 40 without); 4 DEV-1170 join-order hook + swap seams.

**Hard decisions:** A function-call rewrite (not grammar); B reuse DEV-1169's `tjs` engine (don't
duplicate execFagins); C graph leg LATERAL-under-driver, assert early-termination via
`visits()`/ANN-rows probe (the easiest TR-1 trap).

---

## Status snapshot (2026-06-25)
- DEV-1228 ✅ merged (PR #4) · DEV-1165 ✅ PR #5 (Linus ACCEPT) · DEV-1234 filed (ARM SIMD, deferred).
- DEV-1168 / 1166 / 1169 / 1167 → scoped above, not yet executed.

## RECONCILED 2026-06-26 — all four DONE
All four critical-path issues have shipped; this plan is **closed**:
- **DEV-1168** ✅ `scripts/patches/tridb_vector_iter.patch` (sentinel `tridb_vec_open`), ADR-0006.
- **DEV-1166** ✅ `test/txn_atomicity_test.sql` + `scripts/{crash_recovery,graph_concurrency}_test.sh`,
  ADR-0003a. Verified on the GX10 (FR-7 atomicity green in the engine suite).
- **DEV-1169** ✅ `scripts/patches/tridb_tjs_operator.patch`, ADR-0007. Plus the **predicate-correct
  early-termination scale fix** (`tridb_tjs_predicate_termination.patch`, DEV-1169 follow-on) found on
  the first 100k/dim-768 GX10 run — SM-4 restored to 100% (see `docs/STATUS.md`).
- **DEV-1167** ✅ implemented as ADR-0008's SQL-function front door `graph_store.graph_query(text)`
  (NOT the `src/parser/graph_table.c` C parser this plan sketched — the ADR chose the lower-risk
  whole-statement text surface). `test/parse_canonical.sql`, merged **PR #10**.

Remaining TriDB work is NOT in this (completed) backlog — it now lives in: the **128 GB headline
benchmark**, **DEV-1284** (SM-2 latency re-measurement at the correct TJS operating point), **DEV-1170**
join-order C port (`src/planner/join_order.c`, GX10-gated, Python ref model done), HNSW index-quality
tuning (widen the 20%-examined margin), and the `crash_recovery` suite-ordering flake. Run a fresh
`/improve` audit to re-plan these against current reality rather than executing this stale set.

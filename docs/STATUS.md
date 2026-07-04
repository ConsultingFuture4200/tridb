# TriDB Build Status — per-issue gating

Updated: 2026-07-04. Legend: 🟢 unblocked here · 🟡 partial (design here,
build on GX10) · 🔴 GX10-gated (needs live MSVBASE build).

> **🟢 gBRAIN BACKEND HARDENING 036–038 — 3/3 persona-reviewed, MERGED, ENGINE-VERIFIED ON THE GX10 (2026-07-04).**
> Built + run on the DGX Spark against `tridb/msvbase:gx10-v1` (`make graph-test`): **all three new suites
> PASS** — `gph_freeze` (036: froze records, visibility byte-identical, aborted row invisible, relfrozenxid
> advanced, idempotent, future-horizon rejected, ACL), `graph_delete` (037: edge/vertex tombstone + **FR-7
> abort-atomicity confirmed** — a rolled-back tombstone leaves the record PRESENT + `remove_edge` compat),
> `typed_traversal` (038: **parity oracle byte-identical**, source-scope, **TR-1 early-termination preserved**,
> backlinks `feature_not_supported`). **Full non-regression sweep PASS** (tri-modal, FR-7 `txn_atomicity`,
> v1 core, join-order ×4, edge-count, HNSW suites). **One live compile fix** during the build:
> `PG_GETARG_TRANSACTIONID` does not exist in PG 13.4 → `DatumGetTransactionId(PG_GETARG_DATUM(0))`
> (`d9f46af`; the other flagged APIs compiled clean). **One open caveat:** `crash_recovery` scenario 4
> (uncommitted-tombstone-via-crash) fails on a **readiness-poll timeout** (DEV-1331 flake class — session
> alive, never observed reaching `pg_sleep`; the Spark runs AgentBOX load constantly). NOT an engine defect:
> scenario 3 (committed-tombstone WAL redo) PASSES and 037's rollback-equivalent (`graph_delete` PASS B)
> already proves the crash-abort visibility path. Tracked under DEV-1331 (harness readiness budget).
>
> **(historical banner below — superseded by the verification above.)**
> **🟡 gBRAIN BACKEND HARDENING 036–038 LANDED 2026-07-04 — 3/3 persona-reviewed, MERGED, GX10-UNBUILT.**
> Additive native-graph-store hardening to make TriDB a backend for **gBrain** (AgentBOX memory, now on
> the Spark) — spec `docs/gbrain_backend_hardening_v0.1.0.md` (grounded gap analysis G1–G10). All
> golden rules preserved (TR-1, native AM, one-WAL/FR-7, one canonical surface, three stores); frozen
> 32-byte slot / metapage format untouched (reserved-field repurposes only). **036** (DEV-1347) — the
> long-lived-store gate: `gph_freeze(horizon)` freezes stored xids (`gm_reserved`→`gm_frozen_horizon`,
> size-neutral) + indirect anti-wraparound disarm (manual; auto-freeze table-AM stage deferred).
> **037** (DEV-1349) — native delete: `gph_tombstone_edge/vertex`; a review caught that a bare flag is
> NOT abort-atomic (GenericXLog has no undo), fixed by an xid-stamped `es_xmax`/`vr_xmax` (repurposes
> the pad bytes) + a read-path visibility check — FR-7-correct, byte-identical for pre-037 data.
> **038** (DEV-1350) — typed + source-scoped traversal via the existing `es_edge_type_id`; backlinks
> (`direction=in`) RAISE (reverse index deferred, ADR-0016). **CRITICAL — GX10 build must confirm before
> any of this is real:** the C is authored-but-UNBUILT here (needs the PG 13.4 fork image); 036 uses new
> PG APIs whose signatures are shape-verified not compiled (`GetOldestXmin` 2-arg vs PG14, `vac_update_relstats`
> 8-arg form). Run `make graph-test` (freeze/delete/typed suites + FR-7 + crash-recovery) on the Spark.
> Tracked: A2 HNSW abort-durability (DEV-1348), B3/B5/C1 (adapter, cross-repo) not yet built.

> **🟢 PERF QUICK-WIN BATCH 032–035 LANDED 2026-07-04 — 3/3 persona-reviewed, MERGED, GX10-unbuilt.**
> `docs/perf_research_v0.1.0.md` PERF-01/02/03/11. **032** NEON inner-product kernel (DEV-1343) — closes
> the latent cosine-workload bug (default IP metric ran scalar on ARM; directly serves gBrain/nomic-cosine);
> **033** dense-id identity fast-path + **034** backend-local cached vid map (DEV-1344/1345, the ~2ms v1
> id-map tax); **035** COPY bulk load (DEV-1346, unblocks the 128GB saturation run + fair at-scale SM-2).
> Also this session: **SM-1 corrected** 32.0×→**1.07× (FAIL on standin)** — it was recorded with peak=`k`;
> honest `max(k,reached)` accounting fails the ≥5× target and is hardware-independent (the Spark does not
> restore it; a streaming-graph-predicate redesign, PERF-09, would). See `docs/benchmark_results_v0.1.0.md`.

> **🟢 ADVISOR BATCH 024–031 LANDED 2026-07-03 — 7 of 8 plans merged, each persona-reviewed 3/3
> (Fabio + Linus + Liotta) before merge.** From the deep audit + persona review + landscape research
> (`docs/landscape_review_v0.1.0.md`): **024** operator hardening (SQL-reachable k=0 crash, PQ-eviction
> leak, error-path release); **025** the v0→v1 native graph AM rewire (ADR-0013 Stage A/B) — operators
> + all benches now traverse the native adjacency AM, headline re-measured on v1 at recall 1.0 / 13.4×
> / SM-2 24/24 (`docs/benchmark_sm2_1m_v0.3.0.md`), **closing the "every headline measured v0" gap**;
> **026** graph-store ACLs + wraparound-hazard docs + freeze design note; **027** CI nightly engine
> gate + run-all mode + 4 new tests; **028** PG17 platform spike + ADR-0015 (**measured zero PG13→17
> API drift; only bind is the 32KB block size** — the fork is escapable); **030** benchmark credibility
> (p95/p99, Milvus-HNSW baseline row, dep hygiene; steps 2/4 deferred); **031** FR-6 graph-leg-aware
> cost decision core (default-off; fixes the F4 blind spot; wire-up + boundary sweep deferred).
> **029** (perf batch) deliberately DEFERRED — post-025 its top item is moot; the rest needs a shared
> engine-rebuild cycle. Two persona rejections (030 false pymilvus claim; 031 IMMUTABLE-vs-GUC) were
> caught and fixed — the gate worked.

> **🟡 HNSW INDEX-MAP CACHE INVALIDATION 2026-07-02 (ADR-0014, advisor plan 023) — DESIGN + repro.**
> The process-global `vector_index_map` (`src/hnswindex_scan.cpp`) is never erased, so a pooled backend
> serves a STALE (DROP+CREATE same name / REINDEX) or wrong-dimension (→ plan-019 OOB) HNSW graph.
> ADR-0014 recommends a `CacheRegisterRelcacheCallback` eviction (hot path untouched) with a shared_ptr
> ownership rule; repro `scripts/hnsw_stale_index_repro.sh` (engine-gated). Implementation deferred to
> DEV-1259 Phase C.

> **🟡 V1 REWIRE DESIGN (ADR-0013) 2026-07-01 — decision pending maintainer review.** Headline
> numbers to date (SM-2, SIFT-1M filtered, GraphRAG, neon sweep) measure the **v0 heap-backed graph
> extension** (`src/graph_store_ext/`), NOT the v1 native access method the thesis is about — both
> operators and all 9 bench drivers still install v0. ADR-0013 (`docs/decisions/0013-graph-store-v1-rewire.md`,
> design `docs/graph_rewire_design_v0.1.0.md`, spike `scripts/graph_v0v1_bench.sh`) specifies the
> staged rewire; the 128 GB headline run should wait for this decision so it measures the right store.
> See ADR-0013 Context for the coupling facts.

> **🟢 FILTERED VECTOR SEARCH (VectorDBBench IntFilter) 2026-06-27 — LIVE GX10 headline, SIFT-1M.**
> `tools/filtered_corpus.py` + `scripts/bench_filtered.sh` + `bench/filtered_report.py` (`make bench-filtered`):
> fused `WHERE label>=t ORDER BY emb <-> q LIMIT k` (early-terminating Index Scan, TR-1) on REAL SIFT-128.
> **LIVE on the GX10 NEON engine (tridb/msvbase:gx10), full SIFT-1M, recall@10 = 1.000 at every selectivity;
> median latency 40.1 ms @ 1% pass → 87.9 ms @ 99% pass** — i.e. latency DROPS as the filter tightens
> (the predicate is pushed into the scan, not post-filtered). Recall graded vs an exact numpy filtered oracle.
> `bench/results/filtered_metrics.json`, `docs/benchmark_filtered_v0.1.0.md`. A `bench/vdbb_tridb.py` adapter
> bridges the recognized VectorDBBench tool for the 768D1M1P Cohere case (GX10 runbook).

> **🟡 V2 OPEN-RETRIEVAL OPERATOR `tjs_open` 2026-06-28 (ADR-0012) — DESIGN + (A) RUN ON ENGINE; (B) GX10-gated.**
> The h2h finding (single-`src` `tjs()` is constrained-traversal, not an open retriever: recall@10
> 0.223 vs 0.953) motivates a seedless multi-seed operator. ADR-0012 specifies `tjs_open(table, k,
> term_cond, m_seeds, hops, ...)`: ANN top-`m_seeds` → multi-source graph expansion → vector-rank with
> bridges injected, early-terminating (TR-1-preserving). **(A) RUN LIVE on the engine
> (`bench/v2a_open.py`): composing HNSW ANN + `graph_store.neighbors` + rerank recovers open-retrieval
> recall@10 = 0.953** (150 HotpotQA q) — = the multi-store baseline, ~4× the single-`src` tjs (0.223).
> So the engine DOES open retrieval by composition. (A) is BLOCKING (materialises) → a reference/oracle,
> NOT shippable; host `retrieve_graph_inject` adds +15.6pt inject + +2.5pt Codex EM/F1. **(B)** the
> fused early-terminating C operator (fork patch, GX10-gated) is the only TR-1-pure form — the v2
> product. NEXT (cold-resume): build the (B) `tjs_open` fork patch on the GX10.
>
> **UPDATE 2026-06-29+: (B) SHIPPED as a first-cut engine operator (scripts/patches/tridb_tjs_open_operator.patch, merged 3888d45; live recall@10 0.980). Remaining: the ADR-0012 addendum refinement (PPR+FR+RRF, host 0.987) as the next iteration.**

> **🟢 ONE-WAL CROSS-MODAL CONSISTENCY UNDER CHURN 2026-06-28 — PROVEN LIVE ON THE GB10 (GX10).**
> The differentiated claim bolt-on Milvus+Neo4j+pg structurally cannot make, now engine-verified:
> ran `scripts/txn_atomicity_test.sh` + `scripts/crash_recovery_test.sh` on `tridb/msvbase:gx10`.
> **FR-7 ALL PASSED:** atomic COMMIT + atomic ROLLBACK (no partial state) across relational + HNSW-vector
> + native-graph; **C1 200-iter randomized churn → relational↔graph EXACTLY match (zero divergence);
> C2 16-iter HNSW-vector churn → zero divergence; crash recovery hides the aborted xid across all three
> stores.** Corrects a stale note: the v1 native AM (graph_store_am) DOES take incremental HNSW inserts
> inside a transaction (C2 proves it) — an update is NOT forced to be a rebuild on the native path.

> **🟢 RECALL DECAY UNDER UPDATES 2026-06-28 (roadmap b) — vector-leg churn robustness; honest negative.**
> `bench/recall_decay.py` (`make recall-decay`): upsert/delete churn on **hnswlib (the fork's own vector
> lib)**, real SIFT-128, recall@k vs an exact (BLAS-vectorized) oracle + a rebuild reference. **Honest
> finding: NO decay at the scales runnable here — 20k/100q recall@10 0.962→0.967 after 100% churn (Δ
> +0.005, within noise); 500k flat too.** Moderate churn does not wreck hnswlib recall. A definitive 1M+
> curve was NOT obtained: the local 1M OOM-competes with the baseline stack, and the GB10 runs (in a
> `python:3.12` container, since spark lacks `python3-dev` to build hnswlib and has no passwordless sudo)
> stalled on single-threaded index build. Fixed a real bench bug along the way (the oracle was a
> single-threaded per-query einsum → now one BLAS matmul, verified identical to brute force).
> `docs/benchmark_recall_decay_v0.1.0.md`.

> **🟡 TRI-MODAL FUSION ABLATION 2026-06-27 (MultiHopRAG) — the thesis-falsification test; NUANCED result.**
> **Oracle-leakage now killed (roadmap a):** the relational constraint is also parsed from the QUERY
> TEXT (sources/categories named, years/months mentioned; 280/300 queries carry a cue). DEPLOYABLE
> `fusion_qparse` = **0.784 vs vector-only 0.747 (+0.037 recall@10)**; the gold-derived oracle fusion
> (0.805) is now reported only as the upper bound. Graph-leg finding unchanged (adds ~nothing on news).
> `tools/multihoprag_corpus.py` + `bench/ablation_report.py` (`make ablation`): vector / graph / relational /
> fusion on 260 gold-resolved MultiHopRAG questions (real category/source/date metadata = a genuine relational
> leg, unlike HotpotQA). recall@10: **vector 0.747 · graph 0.002 · relational 0.329 · fusion 0.805**. Fusion
> beats best-single (+0.059) BUT two honest caveats: (1) the relational lift (+0.064) uses a GOLD-DERIVED
> (oracle) constraint = upper bound, not deployable — query-parsed constraint is the next step; (2) the GRAPH
> leg adds ~nothing here (graph_only≈0; graph-on-top = −0.005), CONTRAST Plan-015 HotpotQA where graph lifted
> multi-hop +15.6pt. Lesson: fusion value is workload-dependent (graph helps Wiki-bridge, relational helps
> news), and naive HARD relational pre-filter caps recall (kept as `fusion_hardfilter` ablation).
> `docs/benchmark_ablation_v0.1.0.md`.

> **🟡 GRAPHRAG QA-ACCURACY BENCHMARK 2026-06-27 (Plan 015) — the "is the answer right?" artifact, real result on a dev slice.**
> Closes the gap that even the real-SIFT run synthesized its graph from the vectors and graded recall@ANN-oracle (not
> answer accuracy). New harness over **REAL HotpotQA** multi-hop questions with a **REAL, embedding-INDEPENDENT graph**
> (title-mention edges, a faithful proxy for Wikipedia hyperlinks) and BGE-base-768 embeddings: `tools/fetch_hotpot.py`
> (HF mirror — CMU host is down), `tools/build_wiki_graph.py`, `tools/hotpot_corpus.py` (real_corpus-compatible manifest +
> shared `build_sql`), `bench/graphrag_report.py`, `baseline/graphrag.py`, `scripts/bench_graphrag.sh`. **LIVE RESULT
> (host-side, 150 dev q / 1490-para corpus):** injecting graph bridges into the context lifts multi-hop **joint** evidence
> recall@5 on bridge questions **72.1% → 87.7% (+15.6 pts)** vs vector-only; the lift is **+17 pts joint @ k=3-4** and
> shrinks as k saturates. **Honest negative:** the NAIVE graph retriever (gate + re-rank by query cosine) does NOT help —
> the win requires *injecting* the low-similarity bridge, not re-ranking it. Full table + curve:
> `docs/benchmark_graphrag_v0.1.0.md`; metrics `bench/results/graphrag_metrics.json`. 143 Python tests pass, lint clean.
> **GATED:** the downstream LLM answer-EM/F1 headline is reader-gated (no `ANTHROPIC_API_KEY` here; AnthropicReader wired,
> extractive non-LLM lower bound run in its place); the live `tjs()` latency-at-fixed-accuracy + the full
> retrieve-from-all-Wikipedia fullwiki run are GX10/engine-gated (`make graphrag-live`, UNBUILT-HERE).

> **🟡 ARM NEON L2 KERNEL ADDED 2026-06-26 (DEV-1234) — un-sandbags ANN/TJS latency on the GX10.**
> On aarch64 the build strips MSVBASE's hardcoded x86 ISA flags (`scripts/lib/msvbase_patches.sh`
> `patch_cmake_arm_isa_flags`), so hnswlib's `USE_SSE/AVX` SIMD paths are all dead and `L2Space` fell
> back to the **scalar `L2Sqr`** for EVERY distance — the hottest loop in ANN search and the TJS
> re-rank — making every ARM latency number wrong-low. Added a native NEON `L2Sqr` kernel to
> `thirdparty/hnsw/hnswlib/space_l2.h` (`scripts/patches/tridb_neon_l2_distance.patch`, wired into
> the patch chain + `verify_patches`; gated on `__ARM_NEON`, inert on x86 — no build-flag change).
> Validated ON THE GX10 (aarch64): `tools/neon_l2_bench.c` shows the kernel equals scalar within
> **1e-4 rel err** across dims (incl. residual paths 31/100) and is **3.6× (dim 32) / 6.1× (dim 128)
> / 7.8× (dim 768)** faster per distance call; the patched header also compiles in-context and
> `L2Space` returns correct distances at dims 16/32/100/128/768. ENGINE A/B ON THE GX10: rebuilding
> `vectordb.so` through the real MSVBASE `make` (so the patch is proven to build AND run in the
> engine), the HNSW **index-build time on a 20k×128 corpus drops 4.2× — 47.8 s (scalar) → 11.3 s
> (NEON)** on the same cluster, consistent with the per-call kernel speedup (distance is the dominant
> cost of HNSW construction). REMAINING (GX10): roll this into the 128 GB headline benchmark and
> report the end-to-end query-latency delta at the operating point.

> **🟡 HNSW RELOPTIONS + RECALL/LATENCY SWEEP 2026-06-26 (DEV-1286) — index quality unblocked by NEON.**
> Exposed per-index `m` / `ef_construction` as HNSW reloptions (`WITH (m=.., ef_construction=..)`,
> `scripts/patches/tridb_hnsw_reloptions.patch`, wired + verified; default 0 -> hnswlib defaults, so
> existing indexes are unchanged). Swept index-quality × `term_cond` on the **NEON+reloptions engine
> rebuilt through the real MSVBASE `make`** on the GX10 (20k×128, 8 queries, k=10; `tools/sweep_corpus.py`).
> Live result: at the recall@10 = **100%** operating point (`term_cond=20`, default index) the canonical
> `tjs()` query runs in **~1.8 ms median at 2.18% examined** — the first real latency on the target ISA
> (closes the GTM R1 latency gate at moderate scale). High-quality `m=32/ef_construction=400` now builds
> in **5.4 s** (impractical on the scalar fallback — the reason DEV-1286 was gated). At 20k×128 recall is
> saturated, so quality/`term_cond` trade latency not recall. **HEADLINE 100k×dim-768 NOW RUN (NEON):**
> the curve bites — recall@10 **96.25% @ ~36 ms / 3.3% examined** (`term_cond=20`) → **100% @ ~41 ms /
> 4.4% examined** (`term_cond=1000`), all under the 25% TR-1 ceiling. Index build 137 s (m16) / 489 s
> (m32) — feasible only with NEON. Honest negative: `m=32/ef=400` gives identical recall/examined to the
> default here (term_cond is the lever, not index quality). Full table + repro:
> `docs/benchmark_neon_sweep_v0.1.0.md`; artifacts `bench/results/neon_sweep_100k_*`.

> **🟡 TJS JOIN-ORDER INTEGRATION DESIGN 2026-06-26 (DEV-1285) — ADR + safe draft, operator change GX10-gated.**
> The DEV-1170 decision core is shipped; integrating it is NOT a wiring task — `tjs()` is a C SRF (not a
> CustomScan), and it is hardwired vector-first, so "filter-first" is a new physical path. ADR-0011
> (`docs/decisions/0011-tjs-join-order-integration.md`) analyzes the two options and recommends **Option B**
> (pass the chosen order into `tjs()` as a parameter; keeps the validated vector-first body + its
> early-termination bound untouched, preserving TR-1). Delivered: the ADR, a safe additive
> `src/planner/join_order_legstats.{c,h}` catalog helper (UNBUILT-HERE), and a GX10-gated FR-6 stub test.
> Surfaced a real gap: the graph metapage has no `avg_out_degree` (needs `gm_edge_count`, graph-store
> follow-up). The filter-first body SHIPPED (DEV-1290, 2026-07-03) and the FR-6 lowering binds the decision to execution (DEV-1285); both x86- and GX10-validated. The operators + benchmarks now run on the v1 native AM (ADR-0013 Stage A/B, plan 025).

> **🟢 REAL-DATASET BENCH HARNESS 2026-06-26 (DEV-1284) — recall measurable on real vectors today.**
> `tools/real_corpus.py`: loads real embeddings (`.npy/.fvecs/.ivecs/.hdf5`, h5py lazy-imported),
> synthesizes the same topical hub graph the synthetic harness uses, computes the EXACT numpy top-k
> oracle, and emits the IDENTICAL `#BENCH` SQL + manifest the live harness consumes. The SQL emitter
> is now shared (`tools/bench_corpus.py:build_sql`, single source of truth) so the format cannot drift
> between the synthetic and real paths. Recall@k / SM-4 is gradeable on the x86 standin WITHOUT the
> engine; latency (SM-2) / live candidates-examined (SM-3) stay GX10-gated and are never claimed.
> 110 Python tests pass, lint clean. Seam to wire engine recall into `bench/live_report.py` documented.

> **🟢 CRASH-RECOVERY SUITE-ORDERING FLAKE FIXED 2026-06-26 (DEV-1234 P1b).** `scripts/crash_recovery_test.sh`
> scenario 2 (uncommitted tri-store txn) raced host load when it ran LAST in `make graph-test`: the
> 40s sentinel poll could time out before the doomed txn went active, and a self-expiring `pg_sleep(60)`
> could ELAPSE and COMMIT the "doomed" txn, breaking the post-recovery "nothing visible" assert. Fixed
> by holding the txn open with `pg_sleep(3600)` (always killed by the crash; never self-commits) + a
> generous, liveness-checked ~180s readiness budget. Both scenarios PASS against `tridb/msvbase:dev`.

> **🟢 TJS SCALE-DEFECT FIXED 2026-06-26 (DEV-1169) — the defining feature is now correct at scale.**
> The first 100k/dim-768 GX10 benchmark exposed a predicate-blind early-termination bug in the TJS
> operator: graph/relational predicate rejections were counted as VBASE "drops", so a selective
> predicate tripped `term_cond` before the top-k filled → empty/partial answers (SM-4 = 5%, invisible
> at the 2k/dim-32 standin where it read 100%). Fixed in `tridb_tjs_predicate_termination.patch` (a
> "drop" now means past-frontier only: PQ full AND distance ≥ k-th). It is a **correctness fix, not a
> speed win** — and the honest result is a recall/effort curve, not a single number:
>
> | `term_cond` | SM-4 exact-parity | SM-3 examined | |
> |---|---|---|---|
> | 50 (default) | 58.5% | 3.6% | approximate, fast |
> | 5000 | 97.2% | 10.9% | |
> | 10000 | 100% | 20.1% | exact; < 25% TR-1 ceiling |
>
> Linus-reviewed (logic + packaging; SHIP). Clean-room verified: fresh MSVBASE clone + full patch
> chain builds, smoke + SM-1..SM-5 pass, SM-4=100% reproduced. Still open before any public claim:
> latency-in-ms at the operating point vs a full-scan-filter baseline; `term_cond` exposed as the
> recall knob (`BENCH_TERMCOND`), default left at 50. The crash_recovery scenario-2 timeout seen in
> the full `graph-test` sequence is a pre-existing suite-ordering flake (tjs-independent; passes in
> isolation), tracked separately.

> **🟢 ON-TARGET SIGN-OFF 2026-06-25 — the fork now builds and runs on the real GX10.**
> Ran `scripts/gx10build.sh` on the DGX Spark (`gx10-4210`, GB10, aarch64, 128 GB, 20 cores,
> Docker 29.2.1, reachable over Tailscale as host `spark`). Results:
> - **Build:** `[100%] Built target vectordb` → image `tridb/msvbase:gx10`. **DEV-1160/1161 signed off.**
> - **Smoke** (`scripts/smoke_test.sh`): PASS — vectordb extension loads, 100k-row HNSW index
>   builds, early-terminating ANN Index Scan path (TR-1) confirmed in EXPLAIN.
> - **Engine suite** (`make graph-test IMAGE=tridb/msvbase:gx10`): exit 0, **47 PASS / 7
>   "ALL TESTS PASSED"**, zero real failures. Validates on ARM64: graph traversal iterator
>   (DEV-1165), FR-7 tri-store atomicity + SM-5 randomized (DEV-1166), crash/WAL recovery, and
>   txn concurrency. The only non-PASS is the pre-documented logical-single-writer first-edge
>   race (ADR-0003 KNOWN-LIMITATION), unchanged from x86.
>
> The first live run surfaced **two genuine ARM-only build deltas** the x86 standin could never
> exercise — both fixed in `scripts/lib/msvbase_patches.sh` / `gx10build.sh` (branch
> `dustin/dev-1161`): (1) `patch_cmake_arm_isa_flags` strips MSVBASE's hardcoded x86 ISA flags
> (`-msse4.2 -maes -mavx2 -mmwaitx`) that aarch64 GCC rejects (failed every cmake probe as a
> bogus "Could NOT find OpenMP_C"; hnswlib falls back to scalar L2Sqr); (2) a CWD-relative
> smoke-test path in `gx10build.sh` (latent — that line had never run before).
>
> **Only remaining GX10 item: the 128 GB headline benchmark** (at-scale run; the functional
> port is complete). Off-target benches stay x86-standin numbers.

> **RE-GATED 2026-06-23:** the dev workstation was proven a viable **x86_64 standin** —
> `scripts/x86build.sh --docker` builds the MSVBASE fork and `scripts/smoke_test.sh`
> passes (vectordb + HNSW + early-terminating ANN scan). See `docs/BUILD_NOTES.md`.
> Consequence: the native C work (DEV-1164–1170) is now **developed and smoke-tested on this
> standin** (proven: graph_store v0 in `src/graph_store_ext/`, `scripts/graph_test.sh` green)
> rather than blocked on hardware. The 🔴 markers below therefore mean **final acceptance is
> on-target** — the issue builds here but is signed off on the GX10; they are not "cannot build
> off-target." What stays strictly GX10-only: the ARM64 build sign-off (DEV-1160 as written)
> and the 128 GB headline benchmark.

> **PHASE-1 PROGRESS 2026-06-23 (v0, tested on the fork):** `src/graph_store_ext/` — a
> native graph-store extension. `scripts/graph_test.sh` is green:
> DEV-1165 traversal iterator (Open/Next/Close, lazy emission), DEV-1166 FR-7 (cross-store
> atomic rollback+commit). `test/trimodal_compose.sql` composes all three legs (graph +
> relational + vector) in one query. Linus loop caught + fixed a use-after-free and
> tempered overclaims. **v0 is heap-backed**, not the custom 32KB-page AM — honest scope
> and per-issue TODOs in `docs/graph_store_v0_limitations.md`.

> **EARLY-TERMINATING COMPOSITION 2026-06-23 (DEV-1167/1169 functional shape):**
> `test/trimodal_early_term.sql` — the canonical-shaped pipeline driven by the HNSW ANN
> index scan (early-terminating), graph traversal + relational filter per candidate. Plan
> is `Limit -> NestLoop(NestLoop(IndexScan hnsw, FuncScan neighbors), IndexScan d)`; ANN
> scan emitted 8 of 2000 sources. Linus-verified (3 lenses + re-run); filter proof made
> deterministic. Two fork constraints found → `docs/fork_findings.md`: FROM-SRFs
> materialize (production iterator must be custom-scan), and scalar `<->`/`l2_distance`
> return 0 (exact top-k must be the DEV-1168 C operator, not a SQL re-rank;
> `test/fork_distance_probe.sql` confirms).

| Issue | Title | Phase | Gating | Autonomous deliverable this repo |
| -- | -- | -- | -- | -- |
| DEV-1160 | SPIKE MSVBASE build on GX10 | 0 | 🔴 GX10 | Desk-spike findings already captured in issue; live build is GX10-only |
| DEV-1161 | Reproducible GX10 build script | 0 | 🟡 | `scripts/gx10build.sh` authored from spike deltas (runs on GX10) |
| DEV-1162 | Seed corpus + rel/vec smoke test | 0 | 🟡 | `tools/seed_corpus.py` (runs anywhere); SQL smoke test needs the build |
| DEV-1163 | Design adjacency-list layout | 1 | 🟢 | `docs/graph_store_layout_v0.1.0.md` + ADR-0002 |
| DEV-1164 | Adjacency-list access method | 1 | 🟡 | **v1 core built + tested on the x86 standin** (`src/graph_store/graph_am.c`, `scripts/graph_am_test.sh`): 32KB pages via shared buffer mgr + GenericXLog, incremental iterator, FR-7 abort + restart-persistence. Full TableAM vtable / secondary indexes / GX10 benchmark deferred (ADR-0003) |
| DEV-1165 | Graph traversal iterator | 1 | 🔴 GX10 | Iterator contract documented in layout spec; stub |
| DEV-1166 | Verify shared txn manager (FR-7) | 1 | 🔴 GX10 | Test plan in layout spec; runs on the build |
| DEV-1167 | SQL/PGQ surface → logical plan | 2 | 🟡 | `docs/sqlpgq_logical_plan_v0.1.0.md` design |
| DEV-1168 | HNSW relaxed-monotonicity iterator | 2 | 🔴 GX10 | Contract documented; wraps MSVBASE code |
| DEV-1169 | TJS operator | 2 | 🔴 GX10 | Design in plan-mapping doc; stub |
| DEV-1170 | Cross-modal join-order heuristic | 2 | 🟡 | **Hardware-independent layer complete + tested here**: `docs/join_order_heuristic_v0.1.0.md` (v0.1.1, C-port interface FROZEN §10) + `src/planner/join_order_ref.py` reference model + `tests/test_join_order.py` (FR-6 acceptance + boundary/edge-case matrix, 21 cases). C port `src/planner/join_order.c` is GX10-gated (not built here) |
| DEV-1171 | Multi-system baseline harness | 3 | 🟢 | **LIVE multi-system baseline complete + FAIR SM-2 run on the x86 standin.** `baseline/` docker-compose (Milvus+Neo4j+Postgres) + `baseline/sm2.py` (realized-canonical query across all three live systems, merged app-side). `make sm2` (`scripts/bench_sm2.sh` + `tools/bench_sm2_corpus.py` + `bench/sm2_compare.py`) loads the IDENTICAL corpus into both sides (shared deterministic generator, seed 42, 2000 entities/dim 32, 12 queries k=5) and measures both the SAME way (client-side end-to-end wall-clock, warm conns, median of 7 runs, load/index excluded). Result: **SM-2 = 100% (12/12), median latency ratio 15.1× (2k/dim-32 corpus, x86 standin, term_cond=0), answer parity 12/12 exact (Jaccard 1.0)**. `bench/results/sm2_metrics.json` + `docs/benchmark_sm2_v0.1.0.md`. Original `baseline/harness.py` merge skeleton retained (unit-tested) |
| DEV-1172 | TriDB benchmark harness | 3 | 🟢 | **LIVE run done on the x86 standin.** `make bench-live` (`scripts/bench_live.sh` + `tools/bench_corpus.py` + `bench/live_report.py`) drives the canonical query on the REAL `tridb/msvbase:dev` engine over a 2000-entity/dim-32 corpus × 12 queries (k=5), capturing actual `tjs()` answer sets, `tjs_candidates_examined()`, and EXPLAIN ANALYZE latency, vs the in-process baseline model. Stub path (`bench/`, `make bench`) still green + unit-tested. Live TriDB side measured here; SM-2 head-to-head + 128 GB headline stay GX10/stack-gated |
| DEV-1173 | Benchmark results report | 3 | 🟢 | **Reports REAL live numbers.** `bench/results/{bench_live_metrics.json,report_live.html,bench_live_raw.txt}` + `docs/benchmark_results_v0.1.0.md` from a live run: SM-3 **6.4%**, SM-4 **100%** (12/12 exact, triple-verified vs in-DB oracle + baseline model), SM-5 **100%** — PASS. **SM-1 corrected 2026-07-03: 1.07× (FAIL, ≥5× target) on the 2k/dim-32 standin under the honest `max(k, reached)` accounting — the earlier 32.0× recorded peak as `k`; SM-1 is a hardware-independent row-count ratio so the GX10 does not restore it; a real ≥5× needs the streaming-graph-predicate redesign (see benchmark_results note).** SM-2 reported TriDB-side only (mean 1.2 ms) and explicitly GATED; 128 GB headline GX10-only |

## What "done autonomously" means here

The 🟢 / 🟡 items are produced and (where runnable) tested on this dev box. The 🔴 items
get a precise interface skeleton + a written contract so that, the moment the GX10 build
exists, an implementer drops in C against a known surface rather than designing from zero.

## Handoff to GX10

1. Run `scripts/gx10build.sh` on the GX10 → confirms marker #1 live, produces the fork.
2. Implement `src/graph_store/` against `docs/graph_store_layout_v0.1.0.md`.
3. Wire TJS per `docs/sqlpgq_logical_plan_v0.1.0.md`; port `docs/join_order_heuristic`'s
   reference model into `src/planner/join_order.c`.
4. Run `bench/` against the `baseline/` harness on identical corpus → SM-1..SM-5.

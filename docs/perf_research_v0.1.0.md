# TriDB — Performance Research & Improvement Roadmap (v0.1.0)

**Date:** 2026-07-03. **Method:** code-grounded audit of every speed lever in the engine
(four parallel researchers, `file:line` evidence), reconciled against the committed benchmark
docs. **Constraint on every item below:** the golden rules are non-negotiable — TR-1 (no
blocking operator, Open/Next/Close + early termination), native graph AM (not relational joins),
one Postgres process / one WAL. A "speedup" that breaks any of these is not on this list.

## TL;DR

The engine's *measured* wins (filter-first 13.4× at 1M / recall 1.0, SIFT-1M recall 1.0, NEON
L2 4.2× build) are real. This audit finds the **next tier of speed** — and, more importantly,
three things the headline numbers quietly depend on that would break on a real workload:

1. **Inner-product/cosine distance has no NEON kernel → runs scalar on the GX10.** Every
   benchmark pins `l2_distance` to dodge it; a real cosine embedding (BGE-768, GloVe) hits the
   slow path silently. **Highest-ROI fix in the repo: S effort, zero recall risk.** (PERF-01)
2. **HNSW index build is single-threaded** on a 20-core ARM box — it stalled the recall-decay
   benchmark. Parallel insert ≈ 10–15×. (PERF-04)
3. **The 1M flagship forces `--join-order filter_first` by hand** — the automatic decision is
   still graph-blind and would pick vector-first (36× slower) on that query. (PERF-05)

Two corrections to the record surfaced during the audit:
- **SM-1** is a row-count ratio, hardware-independent; the honest value is **1.07× (FAIL)**, not
  32× (already fixed in `benchmark_results_v0.1.0.md`; the structural cause is PERF-09).
- **Advisor-024's "PQ-eviction leak" is a *priority-queue* leak, not product quantization.**
  There is **no vector quantization anywhere in the engine** — the largest un-started memory lever.

## Priority roadmap (impact × tractability)

| ID | Lever | Impact | Effort | Where it builds | Risk | State today |
|----|-------|--------|--------|-----------------|------|-------------|
| **PERF-01** | NEON inner-product / cosine kernel | ~6–8× per-distance on ANY cosine/default-metric index; removes silent scalar fallback | **S** | patch x86, validate GX10 | none (bit-equal ~1e-4) | not started |
| **PERF-02** | Dense-id identity fast-path (skip id translation) | recovers the full ~2 ms v1 tax on dense-id loads | **S–M** | x86 (SQL + loader) | low (detected fast-path only) | not started |
| **PERF-03** | Backend-local cached vid map (C hash) | recovers most of ~2 ms for the general (sparse-id) case | **M** | x86 | low (read-only cache; invalidation hook for ingest) | named, not built |
| **PERF-04** | Parallel `addPoint` HNSW build (per-node locks) | ~10–15× build on 20 GX10 cores; unblocks 1M/768 builds that stall today | **M** | GX10 | slight graph-quality variance (needs recall A/B) | not started |
| **PERF-05** | Bind FR-6 cost model to execution (per-vertex `deg(src)` accessor) | makes the 13.4× filter-first win *automatic* instead of hand-forced | **M** | GX10 (graph-store C) | low | cost core shipped default-off; not bound |
| **PERF-06** | `gph_locate_vertex` dense vid→(blk,slot) directory | O(V/1021 pages)→O(1) source resolution; ~980→~1 page reads/query at 1M | **M** | GX10 | low | not started (unmeasured cost) |
| **PERF-07** | SIMD the filter-first exact-distance kernel (reuse NEON L2) | ~4–8× on the drain distance loop (CPU-bound for large drains) | **S–M** | GX10 | none (squared-L2 monotone, order-preserving) | scalar today |
| **PERF-08** | GPU/CAGRA offline index build (cuVS) | minutes→seconds (151 s → ~2 s at 100k/768); zero serving-path GPU footprint | **M** | GX10 / CUDA | med (external dep; fork-hnswlib format-compat unproven) | validated spike, not wired |
| **PERF-09** | Streaming graph predicate (`has_edge`) + sorted adjacency | fixes SM-1 (peak `max(k,reached)`→`k`, ~30× headroom) | **L** | GX10 | med (touches frozen graph format; needs ADR + recovery re-validation) | design; CSR-lite is a GO-leaning spike |
| **PERF-10** | RaBitQ 4-bit in-engine + in-scan rerank | 7.5× memory at recall@10 = 1.0 (proven in sim); fits ~7× corpus in 128 GB | **L** | GX10 | med (rerank MUST be in-scan per ADR-0006, never SQL) | host numpy sim only |
| **PERF-11** | COPY rework (loader + COPY-capable baseline PG) | unblocks the 128 GB saturation run + a fair at-scale SM-2 | **M** | x86 | low | INSERT-bound; non-goal for v1 launch |
| **PERF-12** | DEV-1259 Phase B: WAL-backed shared-buffer HNSW pages | retires per-backend O(heap) rebuild tax + ~3 GB/backend RAM duplication | **L** | GX10 | high (correctness-critical) | Phase C design only |

## Findings by subsystem

### 1. TJS operator — materialization & early termination
- The single-source `tjs()` is a **C SRF, not a CustomScan** (ADR-0007): deliberate, to avoid
  coupling to the unfinished SQL/PGQ parser. The merge body is factored so a future CustomScan
  reuses it verbatim.
- **SM-1 root cause:** `graphReachableT(src)` at Open runs an *unbounded* `SELECT dst FROM
  gph_neighbors_ext(src)` into a `std::unordered_set` — peak intermediate = `max(k, reached)`.
  This is the only full materialization in the operator, and it is what fails SM-1 (PERF-09).
- **The honest tension:** dropping the reachable-set precompute for a per-candidate `has_edge`
  probe makes SM-1 clean, but adjacency is stored **unsorted**, so `has_edge` is O(degree) — it
  can *regress latency* on high-degree hubs. That is why PERF-09 pairs the streaming predicate
  with **sorted/indexed adjacency** (the CSR-lite spike). Do not ship one without the other.
- **The pragmatic path is already partly shipped:** route selective-predicate queries to the
  **filter-first** body (small driven set, no SM-1 blowup, recall 1.0 by construction) — that is
  PERF-05, and it is the real at-scale answer, not a redesign.
- **`term_cond` is the recall/effort knob**, not an optimization target: 58.5% @ 3.6% examined →
  100% @ 20.1% examined. Every latency comparison in this roadmap must be reported **at a fixed
  `term_cond`**, and SM-4 must be a curve, never a bare headline.

### 2. Vector leg — HNSW, SIMD, GPU, quantization
- **NEON L2 is shipped and validated** (3.6–7.8× per-distance; 4.2× build). **Inner-product has
  no NEON path at all** (`space_ip.h`), and IP is the *default* metric (`hnswindex.cpp:52`) —
  PERF-01. `L2SqrI` (uint8) is scalar everywhere too.
- **Build is single-threaded** (`hnswindex_builder.cpp:135`, one serial `addPoint`/tuple).
  hnswlib supports concurrent insert with per-node locks; 20 ARM cores sit idle — PERF-04.
- **GPU/CAGRA build validated on the GB10** (cuVS 26.06): ~2 s vs 151 s CPU, exports a CPU-native
  hnswlib file so **serving stays GPU-free** (TR-1 intact; GPU batched top-k search is explicitly
  rejected as a blocking operator). Open blocker: the fork's older hnswlib must load the cuVS
  export (format-compat) — PERF-08.
- **Quantization: none in-engine.** RaBitQ is a host simulator. Measured on real SIFT: **1-bit is
  unusable (0.07 even with rerank)**; **4-bit + in-scan rerank = recall 1.0 at 7.5× memory** — the
  honest headline, and the biggest un-started footprint lever — PERF-10.

### 3. Graph access method — the id-map tax and vertex lookup
- The **~2 ms v1 tax is a SQL/plpgsql shim**, not the native AM. `gph_neighbors_ext` issues **one
  reverse B-tree probe per emitted neighbor** (O(out-degree), ~2000 at fanout 2000) against the
  `gph_vid_map` heap side-table. The native traversal itself is already lean (read-once-per-page
  shipped; no WAL on reads). Fixes: PERF-02 (identity fast-path), PERF-03 (cached map).
- **`gph_locate_vertex` is a linear vertex-page chain scan** — ~980 page reads to resolve a source
  at 1M vertices, a *second* per-query cost nobody has measured. Dense vid directory → O(1)
  (PERF-06). Worth flagging: this may rival the id-map tax at scale.
- **CSR-lite (sorted/contiguous adjacency)** is the durable locality win (enables PERF-09's
  streaming predicate as a sorted-merge) but is a **GX10-gated spike**, blocked on hub-relayout
  crash-atomicity (GenericXLog's 4-page cap).
- **Incremental/concurrent ingest is contract-blocked** (single-writer + a hot metapage buffer
  lock per edge on `gm_edge_count`), independent of the map — don't tune it before resolving the
  contract.

### 4. Join order & benchmark harness
- **Two decision cores.** The *threshold* core (shipped, execution-bound) is **graph-blind** —
  it decides on relational selectivity alone and would send the 1M headline query to vector-first
  (36× slower); the flagship dodges this by **forcing `filter_first` on the command line**. The
  *FR-6 cost core* (plan 031) prices both bodies with graph cardinality but is **default-off and
  never called by the lowering**. Binding it needs a **per-vertex `deg(src)` accessor** (the
  store-wide `gm_edge_count` average landed, but the cost model needs per-vertex) — PERF-05.
- **`R=4.0` is calibrated from a single operating point**; the boundary sweep (and re-adding the
  dropped `A·deg` adjacency-enumeration term, and wiring `term_cond` into the `examined` bound)
  was deferred. Needed before flipping the default to cost-mode. (F4 doesn't need precise `R` —
  filter-first holds for any `R>0.29` at the 1M point, ~14× slack — so *binding* > *calibration*.)
- **128 GB run is INSERT-bound:** per-edge `SELECT add_edge(...)` SPI calls + text `INSERT`s, and
  the fork can't insert after index build. **COPY rework** (loader) + a **COPY-capable baseline
  PG** (PGlite can't `COPY FROM STDIN`) unblock both the saturation run and a fair at-scale
  head-to-head — PERF-11.
- **The 1M head-to-head is 2/3 measured:** filter-first vs correct baseline is live and fair
  (13.4×); **vector-first on v1 is stale** (carried from v0.1.0) and should be re-run. The corpus
  is synthetic — the R3 credibility gate (public dataset) still stands.

## Sequencing recommendation

**Quick wins first (small, low-risk, mostly x86-authorable):** PERF-01 (NEON IP kernel),
PERF-02/03 (id-map tax), PERF-11 (COPY rework). These improve real-workload honesty and unblock
at-scale measurement without touching the frozen graph core.

**Then the GX10 build-cycle batch:** PERF-04 (parallel build), PERF-05 (auto-bind filter-first),
PERF-06 (vertex directory), PERF-07 (SIMD drain). These need one shared engine-rebuild cycle on
the Spark and together make the automatic path as fast as the hand-forced flagship.

**Structural bets (own ADR each):** PERF-08 (GPU build), PERF-10 (4-bit quantization), PERF-09
(streaming predicate + sorted adjacency), PERF-12 (WAL-backed HNSW). Highest ceilings, highest
blast radius — each is a project, not a patch.

## Honesty box

- Every C-level impact figure for GX10-gated items (PERF-04/05/06/07/08/09/12) is a **projection**,
  not a measured result — the native access-method and operator changes build only on the Spark.
- The id-map arithmetic (PERF-02/03) reproduces the observed 2 ms but is reasoned, not re-profiled.
- SM-1's 1.07× and the filter-first 13.4× are from committed docs, not re-measured this session.
- The single biggest *architectural* drag is **PERF-12** (the vector leg lives outside the one-WAL
  design, so HNSW is rebuilt per-backend from heap — tens of minutes per fresh connection at
  1M/768, plus ~3 GB/backend RAM). It is the heaviest item and the one that most limits real
  multi-connection deployment; treat it as the north star, not a quick win.

# TriDB @ full-Wikipedia scale — spec (v0.1.0)

**Dual goal, one artifact:**
1. **A personal offline English Wikipedia** — full ~7M articles, semantic + multi-hop retrieval, runs on
   the Spark, no internet. (Wanted for its own sake.)
2. **TriDB's at-scale, recognized-workload benchmark** — the one regime where TriDB's differentiators
   (fused `tjs_open` early termination + native-graph page-locality) turn from *latent* to *decisive*,
   because full-wiki is genuinely I/O-bound (exceeds the Spark's 128 GB working set). This is the **GTM
   R3 credibility artifact** the launch plan is missing (`docs/gtm_opensource_v0.1.0.md`).

Date: 2026-07-04. Supersedes the personal-scale gBrain conclusion (`docs/benchmark_gbrain_graph_v0.1.0.md`):
gBrain fit in RAM and fused app-side, so TriDB's speed advantage stayed latent. Full-wiki fixes *both* —
it's I/O-bound **and** it uses the fused operator. See [[tridb-gbrain-backend]].

## Locked decisions (maintainer, 2026-07-04)
- **Full 7M articles** (personal-use requirement; also the maximally I/O-bound target).
- **Article-level embeddings FIRST, then chunk-level** (article-level = simpler/smaller to prove the
  pipeline; chunk-level = better recall + pushes decisively past RAM, forcing quantization).
- **HotpotQA FIRST, then 2WikiMultihop** (Hotpot: existing Plan-015 harness + bridge multi-hop;
  2Wiki: harder, more hops).

## Why full-wiki is I/O-bound (the whole point)
Article-level, TriDB stores vectors as `float8[]` (double = 8 B/dim, vs pgvector's float32):

| Component | Size (article-level, dim 768) | Notes |
|---|---|---|
| Raw vectors | 6.8M × 768 × 8 B ≈ **42 GB** | `float8[]`; dim-384 model halves this to ~21 GB |
| HNSW index | ~1.5–2× vectors ≈ **60–80 GB** total vector leg | the tight part on a 128 GB box |
| Hyperlink graph | ~200M edges × 32 B slot ≈ **6.4 GB** + vertex recs | native `graph_store_am` |
| Relational (title/redirect/cat/ts) | a few GB | plain Postgres |

Article-level already stresses the 128 GB working set; **chunk-level (~25M chunks → 100–250 GB) exceeds
it decisively** and makes RaBitQ quantization essentially mandatory. Either way, queries touch cold
regions → real reads → the regime where native "3 pages vs 85" and `tjs_open` early termination pay off.

## Architecture — the three legs, fused via `tjs_open`
This is TriDB-native (NOT the gBrain pgvector shim — `tjs_open` runs on the fork's `vectordb` HNSW):
- **Vector**: article/chunk embeddings in `vectordb` HNSW (`float8[]`, `<->`; cosine via normalize-at-write).
- **Graph**: Wikipedia inter-article hyperlinks in native `graph_store_am` — a **real, embedding-independent**
  topology (rebuild embeddings with any encoder, edges don't move; the property the GTM plan wants).
- **Relational**: title, redirect target, categories, timestamp, section — filters + citation payload.
- **Query**: `tjs_open(table, k, term_cond, m_seeds, hops, attr, filter, order)` — seedless open-domain
  (matches Wikipedia QA, which is query-driven, no source anchor): ANN top-`m_seeds` from the question
  embedding → graph-reachable bridges injected past the vector frontier → early-terminating top-k. ONE
  query plan, one WAL. The baseline (Milvus+Neo4j+pgvector) must materialize + ship the reachable set and
  merge app-side — the intermediate blowup TriDB avoids.

## Phases (each independently useful; Phase 0 is the personal-wiki artifact regardless of the benchmark)

**Phase 0 — Extraction (hardware-independent; the offline-wiki foundation).**
enwiki `pages-articles` dump → (a) clean article text, (b) the article→article hyperlink graph
(resolve redirects, drop non-article namespaces), (c) metadata (title, redirect, categories, ts).
Tools: `mwparserfromhell`/`wikiextractor` + a link extractor. Output: a portable corpus manifest the
downstream load consumes (mirror the existing `tools/build_wiki_graph.py` shape at full scale).
*Deliverable even if the benchmark never runs: a queryable offline wiki corpus.*

**Phase 1 — Embeddings (article-level first).**
Local model on the Spark GPU (fastembed / bge-base-768 or bge-small-384 to trade recall for storage —
DECISION POINT: dim vs storage). Normalize at write (cosine on the L2 `<->` path). ~6.8M vectors.

**Phase 2 — Prerequisites this test FORCES (the perf roadmap becomes load-bearing).**
- **PERF-11 (COPY bulk load, DEV-1346)** — millions of rows; per-row INSERT is a non-starter.
- **A bulk native-graph loader** — 200M edges via per-edge `gph_insert_edge` would take days
  (benchmark showed per-edge is slow); needs a batched/COPY-staged edge load into `graph_store_am`.
  (New work; relate to PERF-11.)
- **HNSW build on 6.8M vectors** — single-threaded is tens of hours (recall-decay bench stalled on it):
  needs **PERF-04 (parallel `addPoint`)** or **PERF-08 (GPU CAGRA build, plan 008)**. GPU CAGRA is the
  right tool at this scale (minutes vs hours).
- **NEON IP kernel (PERF-01, DEV-1343)** if a cosine/IP model is used on ARM.

**Phase 3 — HotpotQA fullwiki head-to-head (the core result).**
Extend the Plan-015 harness (`tools/fetch_hotpot.py`, `tools/hotpot_corpus.py`, `bench/graphrag_report.py`,
`baseline/graphrag.py`) from a slice to full wiki. Run `tjs_open` open-domain retrieval vs the tuned
**Milvus+Neo4j+pgvector** baseline (`baseline/`). Metrics:
- **Multi-hop joint evidence recall@k** (the retrieval quality — the +15.6pt inject effect at scale).
- **Latency at FIXED accuracy** (the GTM metric — a faster wrong answer is worthless).
- **SM-3 candidates examined / pages touched** (the I/O-bound proof: early termination + page-locality).
- Both sides measured the SAME way (client-side end-to-end, warm conns, median), corpus pinned.

**Phase 4 — Answer accuracy (reader-gated).**
LLM reader over retrieved context → EM/F1 (Anthropic API or a local model on the Spark GPU; extractive
lower bound if none). Report answer accuracy at fixed latency.

**Phase 5 — 2WikiMultihop.** Harder, more hops → stresses the graph leg + early termination further.

**Phase 6 — Chunk-level embeddings (~25M chunks).** Exceeds RAM decisively → **forces RaBitQ 4-bit
quantization (PERF-10, plan 008)** (42 GB → ~6 GB) with in-scan rerank. Re-run Phases 3–5; this is the
regime where the storage-locality thesis should be most decisive.

**Phase 7 — Personal offline-wiki surface.** NL question → `tjs_open` retrieval → LLM answer + citations,
fully offline on the Spark. (The everyday artifact; falls out of Phases 0–4.)

## What "winning" looks like — and the honest failure mode
- **Win:** at 6.8M I/O-bound scale, `tjs_open` (one system, fused, early-terminating) beats
  Milvus+Neo4j+pgvector on **latency-at-fixed-accuracy**, because the stack materializes + ships the
  reachable set while TriDB terminates early in one plan — AND the native store's page-locality shows in
  SM-3/pages-touched. That's the launch headline (recognized workload, real data, honest repro).
- **Honest failure mode (report it either way):** if `tjs_open` is STILL not faster at this scale (as at
  personal scale), then TriDB's speed thesis is dead and its value is *purely* one-WAL consistency — a
  definitive result. Given full-wiki is genuinely I/O-bound (unlike gBrain), this is the fair test of
  whether the speed thesis survives. Do NOT pre-announce a win; let the numbers decide.

## Hardware / gating
- Phase 0–1 (extract, embed): hardware-independent + Spark GPU; buildable anywhere for the extraction.
- Phase 2–6 (TriDB load, HNSW build, `tjs_open` runs): **GX10/Spark** (native engine).
- Baseline stack (Milvus+Neo4j+pgvector): any Docker host; heavy at 6.8M — budget disk/RAM.
- Spark budget: 128 GB RAM, ~3 TB disk (447 GB used). Full corpus + both engines is disk-feasible;
  the RAM pressure is the point.

## Risks
- **Build time / storage at 6.8M** — the gating cost; mitigated by GPU CAGRA (PERF-08) + quantization
  (PERF-10) + COPY load (PERF-11). These stop being "roadmap" and become prerequisites.
- **`float8[]` doubles vector storage** vs pgvector's float32 — pushes toward a smaller dim and/or
  quantization sooner.
- **enwiki extraction fidelity** (redirects, templates, disambiguation) — reuse/scale the Plan-015
  link-graph builder; validate against known article link sets.
- **Baseline fairness at scale** — Milvus/Neo4j tuning is outcome-determining; commit configs, invite
  "beat it" (per the GTM anti-strawman rule).

## Sequencing (recommended)
Phase 0 (extract → offline-wiki foundation) → 1 (article embeds) → 2 (COPY load + GPU build + bulk graph
loader) → 3 (Hotpot recall+latency) → 4 (reader EM/F1) → 5 (2Wiki) → 6 (chunk-level + RaBitQ) →
7 (offline-wiki surface). Phase 0 alone delivers the personal wiki; Phase 3 alone delivers the launch
benchmark.

## Tracking
Linear: this spec = the parent; sub-work reuses PERF-11 (DEV-1346), PERF-04, PERF-08 (plan 008),
PERF-10, plus a new bulk-native-graph-loader item and the Hotpot-fullwiki harness scale-up.
Docs to extend: `tools/fetch_hotpot.py`, `tools/build_wiki_graph.py`, `bench/graphrag_report.py`,
`baseline/graphrag.py`, `docs/benchmark_graphrag_v0.1.0.md`.

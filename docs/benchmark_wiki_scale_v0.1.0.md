# TriDB full-Wikipedia scale benchmark — build + validation (v0.1.0)

**What this is.** The Phase-0 (extraction) + Phase-3 (retrieve-from-all-wiki recall) *pipeline* for
the full-Wikipedia benchmark (`docs/wiki_scale_benchmark_spec_v0.1.0.md`, DEV-1354), plus an honest
account of what was built and validated on the x86 standin vs what runs on the GX10/Spark. **This is
NOT a headline result** — no latency win is claimed or pre-announced (spec §"honest failure mode").

TL;DR
- **Built + validated HERE (hardware-independent):** the streaming extractor, the HotpotQA→full-wiki
  title linker, and the retrieve-from-all-wiki recall harness. Validated on a real 40k-article
  simplewiki slice + a controlled end-to-end harness run.
- **Honest slice finding:** a bounded slice resolves FEW HotpotQA questions (a 40k simplewiki slice
  hits 9% of gold titles, 0 questions with *both* gold present). Real-coverage recall grading needs
  full enwiki — whose 6.8M embeddings are a Spark-GPU (Phase 1) job. This is the expected, documented
  behavior, not a defect.
- **GX10/Spark-gated (contracts written, UNBUILT-HERE):** the live `tjs_open` latency + SM-3
  pages-touched, the COPY / bulk-native-graph / HNSW loads (`docs/wiki_scale_load_design_v0.1.0.md`),
  and the at-scale embeddings.

---

## Built + validated on the x86 standin

| Component | File | Validated here |
|---|---|---|
| Streaming extractor (Phase 0) | `tools/wiki_extract.py` | `tests/test_wiki_extract.py` (10 cases) + a real 40k simplewiki slice (below) |
| HotpotQA → full-wiki title linker | `tools/wiki_hotpot_link.py` | `tests/test_wiki_hotpot_link.py` (5 cases: resolution, redirect chain, coverage, manifest round-trip) + real 40k run |
| Retrieve-from-all-wiki recall harness | `bench/wiki_scale_report.py` | `tests/test_wiki_scale_report.py` (end-to-end: manifest→link→grade→emit SQL) + a controlled CLI run |
| Link-prediction track (cosine-only LOWER BOUND) | `tools/wiki_linkpredict.py` | `tests/test_wiki_linkpredict.py` (5 cases: set-subtraction, overlap metric, artifact round-trip); host CPU run on the simplewiki slice |
| Makefile targets | `Makefile` (`wiki-fetch` / `wiki-extract` / `wiki-scale` / `wiki-linkpred`) | guards mirror `fetch-hotpot`/`graphrag`; network + engine gated, not in CI |

**Real 40k simplewiki slice (`make wiki-extract WIKI_MAX=40000`).** Streamed the simplewiki dump
(two-pass, bounded RAM) into a portable manifest:

| articles | edges | categories | redirects | pages scanned (pass 1) |
|---:|---:|---:|---:|---:|
| 40,000 | 727,808 | 119,025 | 15,490 | 69,435 |

The manifest counts reconcile with the shard files (asserted by the extractor tests), and the linker
rebuilds the exact id space from the shards (round-trip test).

**Linking the HotpotQA dev slice (150 q) against the 40k slice** (`make` /
`tools.wiki_hotpot_link`): **9.0% of gold titles resolved, 0 questions fully-resolved** (both gold
titles present). This is the honest fullwiki reality at slice scale — the retrieve-from-all-wiki
metric is only defined on questions whose gold is *in* the corpus, and a 40k-article Simple-English
slice contains few HotpotQA gold pairs. Coverage rises with corpus size; **full enwiki (6.8M) is the
corpus where most questions resolve** — and that embedding step is the Spark-GPU Phase-1 gate.

**Harness grading path** — validated end-to-end on a controlled micro-corpus with precomputed
embeddings (so the BGE encode is not on the critical path): the harness reads the manifest, resolves
gold into the real-wiki id space, grades multi-hop joint evidence recall@k over the WHOLE corpus with
the SAME `vector_only` / `graph_inject` retrievers as the dev-slice report (`bench/graphrag_report.py`,
reused — single source of truth), and emits the GX10-gated `tjs_open` SQL. `graph_inject` recovers the
bridge article via the real `0->1` hyperlink edge (joint recall 1.0 by k=3 in the smoke).

Reproduce:
```bash
make wiki-fetch WIKI=simple                  # network-gated (skips if present)
make wiki-extract WIKI_MAX=40000             # Phase 0 slice -> data/wiki/simplewiki_slice
make fetch-hotpot HOTPOT_Q=150               # HotpotQA dev slice (HF mirror)
make wiki-scale                              # retrieve-from-all-wiki recall (host-side)
```
(On the 40k simplewiki slice `make wiki-scale` reports 0 gradeable questions and stops — expected;
point it at a full-enwiki manifest with GPU-precomputed `WIKI_CORPUS_EMB`/`WIKI_QUERY_EMB` for the
real grade.)

**Link-prediction track (`make wiki-linkpred`).** A second host-side track: candidate links are pairs
that are semantically close (high BGE cosine) but NOT already a hyperlink. For each article we
take its top-k cosine neighbours and subtract self + in-slice out-edges; the
remainder is the ranked "should-probably-be-linked" set. (Redirect equivalence needs no separate
subtraction: the extractor resolves every wikilink through the redirect map before emitting edges, so
redirect targets are already canonical out-edges — and redirect pages are never emitted as articles.) This is the **cosine-only LOWER BOUND** — it
sees semantic proximity but not multi-hop topology; the production predictor fuses it with graph
structure via `tjs_open` (GX10-gated), so the numbers here are a floor, not the fused-engine result.
The reported **overlap** metric (fraction of top-k neighbours already linked) is honestly *deflated* on
a bounded slice because most true out-edges point outside the slice; `wiki-linkpred` emits
`mean_out_edges_in_slice` alongside it to quantify that ceiling. `WIKI_LP_LIMIT=0` embeds the whole
corpus; `WIKI_LP_EMB_OUT=<path>` persists the normalized embeddings (`.npy` + `.ids.npy` + `.meta.json`)
so the Phase-2 engine load reuses them instead of paying the 7M GPU embed twice.

---

## GX10 / Spark-gated (contracts written; NOT run here)

| Gated work | Contract | Why gated |
|---|---|---|
| Article COPY bulk load (PERF-11 / DEV-1346) | `docs/wiki_scale_load_design_v0.1.0.md` §1 | needs the fork COPY path + a COPY-capable baseline PG |
| Bulk native-graph edge load (200M edges) | load design §2 (dense-id + `gph_set_identity_mode` → staged `gph_insert_edge`, NOT per-edge `add_edge`) | native `graph_store_am`, engine-only; per-edge is days |
| HNSW build on 6.8M vectors | load design §3 (PERF-04 parallel `addPoint` or PERF-08 GPU CAGRA) | tens of hours single-threaded; GPU is ARM64/cuVS-only |
| Chunk-level RaBitQ 4-bit (Phase 6) | load design §4 (footprint sim `make rabitq-sim` here; in-scan rerank on engine) | in-engine quantized storage is GX10-pending |
| Live `tjs_open` latency + SM-3 pages-touched | `bench/wiki_scale_report.py --emit-sql` → run on Spark (`scripts/graph_test.sh`) | needs `tridb/msvbase:dev|gx10`; NEVER fabricated off-target |
| At-scale (6.8M) embeddings | spec Phase 1 (Spark GPU, fastembed/BGE) → id-aligned `.npy` | GPU throughput; `float8[]` footprint is the I/O-bound point |

---

## The honest win / failure-mode framing (unchanged from the spec)

- **Win (if it holds):** at 6.8M I/O-bound scale, fused `tjs_open` (one system, one WAL, early
  terminating) beats Milvus+Neo4j+pgvector on **latency-at-fixed-accuracy** because the multi-store
  stack must materialize + ship the reachable set while TriDB terminates early in one plan — and the
  native store's page-locality shows in SM-3 / `gph_page_reads()`.
- **Honest failure mode (report either way):** if `tjs_open` is STILL not faster at this genuinely
  I/O-bound scale (as it was at personal/gBrain scale where everything fit in RAM), then TriDB's speed
  thesis is dead and its value is *purely* one-WAL cross-modal consistency — itself a definitive result.
  This document reports RETRIEVAL-QUALITY plumbing built here; it does **not** pre-announce a latency
  win. The numbers decide, on the Spark.

Tracking: DEV-1354. Extends `tools/fetch_hotpot.py`, `tools/hotpot_corpus.py`, `bench/graphrag_report.py`
to full-wiki scale; the load contracts relate to PERF-11/04/08/10.

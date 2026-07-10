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

---

## Addendum A (2026-07-06) — Phase-2 AT-SCALE LOADER + VERIFIED bounded proof on the real engine

**What this adds.** The Phase-2 load tooling of `docs/wiki_scale_load_design_v0.1.0.md` is now BUILT
(`tools/wiki_engine_load.py` + `scripts/wiki_engine_load.sh`) and RUN, bounded, against the LIVE
`tridb/msvbase:gx10-v1` engine on the Spark (GB10). This is a **verified bounded proof of the loader +
tri-modal fusion**, plus an honest extrapolation to full 6.9M/224M — **not** a latency head-to-head (that
still needs the baseline stack at scale; see gaps).

**Engine surface actually probed (gx10-v1, 2026-07-06).** The batched C entry point
`gph_insert_edges(src, dst[])` that load-design §2 calls "new engine work to specify" is **NOT** in the
shipped SQL. The available bulk lever is therefore what the loader implements: materialize N dense
vertices in id order (vid == article id), then bulk-insert edges by calling `gph_insert_edge(src,dst)`
**directly by vid** from a COPY-staged relation `ORDER BY src` — bypassing the per-edge id-map tax that
`add_edge()` pays (two `gph_upsert_vertex` descents/edge). `gph_set_identity_mode(true)` is flipped only
**after** the verified dense in-order load so the `tjs_open` read path skips the map too.

**Bounded run — 100,000 articles + their 3,444,031 induced edges + a 100k-vector HNSW** (dim-64
deterministic synthetic vectors; see honesty note). Driven the repo way
(`scripts/wiki_engine_load.sh tridb/msvbase:gx10-v1 <prep>` → PGXS-build `graph_store_am` into a
throwaway cluster, `\copy` + native load + asserts). **All assertions passed, exit 0.** Raw transcript:
`bench/out/wiki_engine_load_100k_gx10v1.log`; prep manifest `bench/out/wiki_engine_load_100k_prep.json`.

| Load step | Rows | Wall time | Rate |
|---|---:|---:|---:|
| `\copy articles (id,title,ts,embedding)` (PERF-11) | 100,000 | 1.72 s | 58,200 art/s |
| HNSW build, dim-64 synthetic (`USING hnsw`) | 100,000 | 34.49 s | 2,900 vec/s |
| Dense vertex materialize (`gph_upsert_vertex` 0..N-1 in order) | 100,000 | 1.86 s | 53,800 v/s |
| `\copy edge_stage` (staging) | 3,444,031 | 0.75 s | 4.6 M edges/s |
| **Native edge insert** (`gph_insert_edge` direct-by-vid, `ORDER BY src`) | 3,444,031 | **182.0 s** | **18,900 edges/s** |

**Verified (engine-asserted, not modelled):**
- `#WL ASSERT articles=100000 OK` — relational count == slice.
- `#WL ASSERT edges=3444031 vertices=100000 OK` — native `gph_edge_count()` / `gph_vertex_count()`
  == slice exactly.
- `tjs_open('articles', 10, 64, 8, 1, …)` → 10 ids, **candidates examined = 128 ≪ 100,000** (TR-1 early
  termination holds at 100k).
- `gph_neighbors_ext(0)` under identity mode = `{11208,47112,95129,2924,13883,2390,8725,10525}` — all
  in the loaded induced out-neighbor set → the plan-033 identity-mode read is **correct for this
  dense-in-order load** (DEV-1352 not triggered here).
- Bridge injection over REAL topology: `bridges_injected = 148`, top-60 overlaps seed 0's induced
  out-neighbors → the graph leg fuses into the vector top-k.

**Honest extrapolation to full enwiki (6,900,039 art / 224,475,283 edges), at the measured rates:**

| Leg | Full-scale estimate | Basis / caveat |
|---|---:|---|
| Article COPY | ~2 min | 6.9M / 58.2k·s⁻¹ (id+title+ts only; body excluded per design §1) |
| Vertex materialize | ~2.2 min | 6.9M / 53.8k·s⁻¹ |
| Edge staging COPY | ~50 s | 224M / 4.6M·s⁻¹ |
| **Native edge insert** | **~3.4 hours** | 224M / 18.9k·s⁻¹ — **the gating load cost** |
| Embeddings (real) | ~84 h CPU **(blocked)** | 23.7 docs/s measured on this box; no GPU path (below) |
| HNSW build (real dim-768) | tens of hours **(blocked)** | design §3; the dim-64 34.5 s number does **not** extrapolate |
| Vector-leg footprint | ~44 GB raw + ~tens GB index | 6.9M × 768 × `float8`(8 B); the 128 GB tight point (spec) |

**Remaining gaps (honest, this is the acceptance criterion):**
1. **The per-edge path is ~3.4 h at 224M.** The staged direct-by-vid loader removes the SQL
   round-trip and the `add_edge` map tax, but **not** the per-edge C call + per-edge WAL record.
   Load-design §2's true batched `gph_insert_edges` (one page-extend + one GenericXLog per adjacency
   run) — the thing that turns hours into minutes — is **still unbuilt** in the gx10 images.
2. **Real embeddings are blocked on this GB10.** `onnxruntime.get_available_providers()` on the
   Blackwell/sbsa box = `['AzureExecutionProvider','CPUExecutionProvider']` — **no
   `CUDAExecutionProvider`**, so fastembed runs CPU-only (23.7 docs/s → ~84 h for 6.9M). The GPU
   CAGRA build (PERF-08) needs either a working Blackwell `onnxruntime-gpu` wheel or a cuVS path;
   neither installs here without sudo/root. This bounded proof therefore used **deterministic dim-64
   synthetic vectors** — sufficient to prove the LOADER, the HNSW build path, TR-1 early termination,
   the native counts, and graph→vector fusion, but **not** recall quality (Phase-1/Phase-3).
3. **No at-scale latency head-to-head yet.** The multi-system baseline (Milvus+Neo4j+pgvector) is not
   stood up at 7M, so the fused-`tjs_open` SM-2 win/loss (spec §"winning"/"failure mode") is still
   undecided at the I/O-bound scale. This addendum does **not** pre-announce a win.

**Reproduce:** `python tools/wiki_engine_load.py prepare --manifest data/wiki/enwiki --out <dir>
--limit 100000 --dim 64 --synthetic` then `scripts/wiki_engine_load.sh tridb/msvbase:gx10-v1 <dir>`.
**Full-scale status: BLOCKED, not "ready".** The loader is *validated on the shard-0 / 100k slice
only*. Unbounded (`--limit 0`) full-7M is blocked pending **manifest reconciliation**: the enwiki
extract has duplicate + truncated shards — 76 shard descriptors for 72 distinct files
(`articles-00071` ×3, `-00049`/`-00028` ×2), and on-disk articles (~7.14M) fall short of the
manifest count (6.9M) because the extractor reopens revisited shards in `w`/truncate mode
(`wiki_extract.py:301`, data loss). The loader now (a) dedupes shard paths so a full run no longer
double-reads a shard into a duplicate-key COPY abort, (b) materializes native vertices over the
dense `[0, max_id]` id range (article ids are sparse) instead of `generate_series(0, N-1)`, and
(c) in the unbounded case **aborts** if the loaded slice does not reconcile against ground-truth
manifest counts — rather than silently emitting a truncated corpus whose self-referential asserts
still pass. Once the extract is regenerated clean, drop `--limit` and supply real vectors via
`--emb corpus_emb.npy` (Phase-1 persisted) or `--embed` (fastembed) instead of `--synthetic`.

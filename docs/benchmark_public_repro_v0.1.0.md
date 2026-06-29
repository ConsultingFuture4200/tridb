# TriDB — Public-Dataset Reproduction (the launch artifact) v0.1.0

**TL;DR.** One command (`make bench-repro`) runs TriDB's retrieval against
**recognized public datasets**, grades **recall@k against an exact oracle**, and
emits a metrics JSON + a rendered table — **pinned data, pinned seeds**. The
headline number is **real and reproduces on a commodity x86 box, no engine, no
GPU**: on the **HotpotQA** dev slice, injecting real graph bridges lifts multi-hop
**joint** evidence recall@5 by **+15.6 pt** over vector-only (72.1% -> 87.7%).
Live `tjs()` **latency** stays GX10-gated and is **never fabricated here**. This is
the artifact [`docs/gtm_opensource_v0.1.0.md`](gtm_opensource_v0.1.0.md) names as
the make-or-break item that retires the "synthetic corpus" credibility gap.

This doc is the thing a stranger reads. It is structured on the GTM doc's "preempt
every attack" table: dataset, baseline, the metrics that matter (reported as a
**curve**, not a peak), the one-command repro, and the honest scaling caveats.

---

## Lead honest: what is real here vs gated vs prototype

| Claim | State | Where |
|---|---|---|
| HotpotQA graph-inject lifts multi-hop **joint recall@5 +15.6 pt** | **REAL — reproduces on this x86 box** (host numpy, graded vs gold supporting paragraphs) | `bench/graphrag_report.py`, `make graphrag` |
| `sift-128-euclidean` is a recognized public set, **pinned + SHA256-verified** | **REAL — verified here** | `tools/fetch_dataset.py` (pin), `make bench-repro` (verify) |
| **Exact numpy top-k oracle** over the real SIFT vectors | **REAL — built here** | `tools/real_corpus.py: exact_oracle` |
| Live `tjs()` answer set vs the SIFT oracle (recall@k on the engine) | **GX10/engine-gated** | `scripts/bench_public.sh` (image-guarded) |
| Any `tjs()` **latency** (ms) | **GX10/engine-gated** | live `EXPLAIN ANALYZE`, not run here |
| 100k / dim-960 GIST headline curve, 128 GB saturation | **GX10-gated** | `PUBLIC_LIMIT=100000 make bench-public` |
| The open-domain multi-hop *retrieval operator* (multi-seed `tjs_open`) | **PROTOTYPE** (host-side inject; not an engine operator in v1) | ADR-0012, [`benchmark_h2h_v0.1.0.md`](benchmark_h2h_v0.1.0.md) |

The honest one-line summary: **the recall mechanism, on data strangers recognize,
is real and reproducible on this box; the live latency at the operating point is
the gated headline item, run on the GX10.** The `+15.6 pt` graph lift is produced
by a **host-side graph-inject prototype**, not by the v1 single-source `tjs()`
operator — see "Scope & non-goals" below. We say that plainly because the GTM doc
demands it and because honesty is the differentiator that survives HN.

---

## Preempt every attack

| Attack | Neutralizer (in this artifact) |
|---|---|
| **"Synthetic corpus."** | Two **recognized public** datasets: HotpotQA (the standard multi-hop QA benchmark) and `sift-128-euclidean` (a canonical ann-benchmarks corpus). The harness **downloads** them; the SIFT file is **pinned by SHA256** and verified before use. |
| **"You wrote both sides."** | **One command** (`make bench-repro`) runs from a clean checkout. Recall is graded against an **exact oracle** — gold supporting paragraphs (HotpotQA) and an exact numpy top-k (SIFT) — not against our own engine's output. The whole pipeline is in the repo. |
| **"Strawman baseline."** | The comparison baseline is the real multi-store stack people actually run — **Milvus + Neo4j + Postgres**, app-side merge — and it is **tuned, configs committed** (`baseline/TUNING.md`, every value a constant in `baseline/sm2.py`). **Beat it:** change the configs, re-run `make sm2`, send the diff. |
| **"Toy scale."** | The at-scale recall/effort curve is run on the GX10 (NEON): recall@10 **96.25% -> 100%** across `term_cond`, every point under the 25% TR-1 ceiling ([`benchmark_neon_sweep_v0.1.0.md`](benchmark_neon_sweep_v0.1.0.md)). The 100k/dim-960 GIST headline is the remaining GX10 run; it is labeled gated, not claimed. |
| **"Speed, but is the answer right?"** | recall@k + downstream answer accuracy, **reported as a curve**: graph-inject lifts multi-hop **joint** evidence recall +15.6 pt @ k=5 on a real graph ([`benchmark_graphrag_v0.1.0.md`](benchmark_graphrag_v0.1.0.md)). The SM-4 exact-parity oracle is reported honestly as a curve, never a bare peak. |
| **"The graph just re-encodes the vectors."** | The graph is **real title-mention topology** (an embedding-INDEPENDENT proxy for Wikipedia hyperlinks): rebuild the embeddings with any encoder and the edges do not move. The naive `graph_rerank` is shown to **NOT help** — only injecting real bridges does. So *topology*, not the vectors, carries the multi-hop signal. |

---

## The datasets (cited, downloaded by the harness)

### HotpotQA dev slice (the recall headline)

- **What:** [HotpotQA](https://hotpotqa.github.io/) — the standard multi-hop QA
  benchmark. The slice is the `distractor` dev pool (gold supporting paragraphs
  guaranteed present), 150 questions / 1490 paragraphs.
- **Embeddings:** BGE-base-en-v1.5 (dim **768**), cosine.
- **Graph:** real **title-mention** edges (a faithful, embedding-independent
  stand-in for Wikipedia hyperlinks; the manifest records the edge source so an
  on-target run can swap in the official hyperlink dump).
- **Fetch:** `make fetch-hotpot HOTPOT_Q=150` (HF mirror; the CMU host is down).
  Network-gated; never run by tests/CI.

### sift-128-euclidean (the recognized public-ANN pin)

- **What:** SIFT1M, a canonical ann-benchmarks corpus. dim **128**, **L2** — L2
  matches the canonical `<->` ordering and the engine's `distmethod=l2_distance`
  (an *angular* set would rank by cosine and disagree with the L2 oracle).
- **Pinned:** SHA256 `dd6f0a6ed6b7ebb8934680f861a33ed01ff33991eaee4fd60914d854a0ca5984`
  in `tools/fetch_dataset.py`, verified on every fetch and by `make bench-repro`.
  This mirrors the "pin once, verify forever" discipline the build uses for
  Boost/CMake (`scripts/lib/msvbase_patches.sh`).
- **Honest scope note:** dim 128 is **below** the 768+ headline target. SIFT
  exercises the recognized-dataset + exact-oracle plumbing; the dim-960 GIST
  headline (`gist-960-euclidean`, `_PENDING` until its first networked `--pin`)
  is the GX10 run. We default the headline RECALL claim to HotpotQA (768-dim,
  real, host-gradeable) for exactly this reason.

---

## The metrics that matter (a curve, not a peak)

### 1. HotpotQA evidence recall vs k (REAL, graded host-side vs gold)

Group: `bridge` (the multi-hop case the graph targets). `graph_inject` is the real
GraphRAG mechanism (inject graph-reachable bridges regardless of query similarity);
`graph_rerank` is the naive ablation (kept to show it does **not** help).

| k | vector_only joint | graph_inject joint | graph_rerank joint | inject lift |
|---:|---:|---:|---:|---:|
| 2 | 0.443 | 0.443 | 0.443 | +0.000 |
| 3 | 0.607 | 0.779 | 0.607 | +0.172 |
| 5 | 0.721 | 0.877 | 0.721 | **+0.156** |
| 10 | 0.918 | 0.959 | 0.893 | +0.041 |

**Read it as a curve:** the lift is largest at the **tight, realistic** RAG budgets
(k=3-5) where vector-only misses the low-query-similarity 2nd hop; at loose k the
pool saturates and the lift shrinks; on single-hop `comparison` questions there is
no 2nd hop to recover, so graph ~ vector. That k/type dependence **is** the finding.
Full table (incl. any-gold recall, by-type, downstream EM/F1):
[`benchmark_graphrag_v0.1.0.md`](benchmark_graphrag_v0.1.0.md).

### 2. The at-scale recall/effort curve (GX10, NEON) — labeled gated

`tjs()` trades recall for effort via `term_cond`. **Pin a `term_cond` per metric;
never mix the default-`term_cond` latency with the high-`term_cond` recall.** On
the GX10 at 100k/dim-768 (NEON): recall@10 **96.25%** at `term_cond=20`
(~36 ms / 3.3% examined) -> **100%** at `term_cond=1000` (~41 ms / 4.4% examined),
every point under the 25% TR-1 ceiling ([`benchmark_neon_sweep_v0.1.0.md`](benchmark_neon_sweep_v0.1.0.md)).
On real SIFT clustered data the default `term_cond=50` is too shallow (recall ~16%)
and `term_cond ≈ 1000` is the real operating point ([`benchmark_public_v0.1.0.md`](benchmark_public_v0.1.0.md)).

### 3. SIFT exact oracle — recognized-corpus plumbing (REAL here), live recall gated

`make bench-repro` verifies the SIFT pin and builds the **exact numpy top-k oracle**
over the real vectors. The host-side **self-check** grades the oracle against itself
(1.000) and proves ONLY the dataset/oracle plumbing — it is **not** an engine recall
number. Grading the **live `tjs()`** answer set against this oracle, and the
**latency**, are GX10/engine-gated and are never produced on this box.

---

## The baseline (configs committed, "beat it")

The comparison baseline is **Milvus + Neo4j + Postgres**, merged app-side
(`baseline/`), **tuned with the configs committed** (`baseline/TUNING.md`): IVF_FLAT
`nlist=128` / `nprobe=64` (a deliberately high-recall point), a `k*32` ANN
over-fetch (the intrinsic multi-store penalty, set generously so the baseline does
not lose on under-fetch), an indexed Neo4j 1-hop, and a Postgres B-tree on the
timestamp. Every value is a constant in `baseline/sm2.py` — no hidden config.
**If you can tune it faster, the configs are in the repo: change them, re-run
`make sm2`, send the diff.** The fair SM-2 head-to-head at scale is GX10/stack-gated
([`benchmark_sm2_v0.1.0.md`](benchmark_sm2_v0.1.0.md)).

---

## The one-command repro

```bash
# 0. Setup (any x86_64/ARM64 dev box — no engine, no GPU needed for recall)
uv venv .venv && . .venv/bin/activate && uv pip install -r requirements.txt

# 1. Fetch the pinned public data (network-gated; NOT run by tests/CI)
make fetch-hotpot HOTPOT_Q=150                          # HotpotQA dev slice (HF mirror)
make graphrag                                           # build real graph + BGE-768 embeddings
make fetch-dataset PUBLIC_DATASET=sift-128-euclidean    # SIFT1M, SHA256-verified

# 2. THE one command — assemble, grade recall@k vs exact oracle, render a table
make bench-repro
#   -> bench/results/bench_repro_metrics.json
#   -> bench/results/bench_repro_report.md   (rendered, attack-preempt honest)
```

`make bench-repro` runs entirely host-side (recall is gradeable on the standin). It:

1. guards that the HotpotQA manifest is present (else points at `make fetch-hotpot`),
2. runs the HotpotQA evidence-recall sweep (vector-only vs graph-inject vs the naive
   graph-rerank ablation), graded against gold supporting paragraphs,
3. verifies the SIFT public dataset against its committed SHA256 pin and builds the
   exact numpy top-k oracle over the real vectors,
4. emits a combined metrics JSON + a rendered markdown table with the honesty split.

It **does not** fabricate a live `tjs()` latency or a SIFT engine-recall number —
those are GX10-gated and the rendered table says so.

### Reproducing the live (engine) half — GX10

```bash
scripts/x86build.sh --docker        # or scripts/gx10build.sh on the GX10
make bench-public                   # live tjs() recall@k vs the SIFT oracle (engine-gated)
make baseline-up && make sm2        # fair SM-2 latency head-to-head (stack-gated)
```

---

## Scope, non-goals, and scaling caveats (the honest limits)

- **The +15.6 pt lift is a host-side prototype, not a v1 engine feature.** v1's
  `tjs()` is a **single-source constrained-traversal** operator (ranks vectors
  within one `src`'s graph-reachable set); it is **not** an open-domain multi-hop
  retriever. The open multi-hop result here is produced by a host-side multi-seed
  graph-inject prototype. The engine operator (seedless multi-seed `tjs_open`,
  ADR-0012) is the **next build**, validated as an algorithm but not yet an engine
  operator. See [`benchmark_h2h_v0.1.0.md`](benchmark_h2h_v0.1.0.md) — do **not**
  launch v1 as a drop-in open GraphRAG retriever.
- **What v1 actually wins (lead with these):** (1) **source-anchored tri-modal
  queries** ("given entity X, find vector-similar entities reachable from X,
  filtered"): SM-2 = 12/12 at ~13× lower latency with exact parity
  ([`benchmark_sm2_v0.1.0.md`](benchmark_sm2_v0.1.0.md)); (2) **one system, one
  WAL, transactional across all three stores** (proven on the GB10) — the
  consistency story a bolt-on Milvus+Neo4j+pg stack cannot tell.
- **The steep `term_cond` recall/effort jump is a real limitation:** running graph
  / relational predicates on an HNSW ANN stream means the index ordering does not
  track predicate-passers, so a selective predicate forces a deeper scan. State it,
  don't hide it ([`benchmark_neon_sweep_v0.1.0.md`](benchmark_neon_sweep_v0.1.0.md), R1).
- **Graph is a mention-proxy here, stated plainly:** the official Wikipedia
  hyperlink dump was unreachable (CMU host down; HF mirrors gated). Title-mention
  edges are a faithful, embedding-independent stand-in; the manifest records the
  edge source so the on-target run can swap in the real dump.
- **SIFT is dim 128 (below the 768+ headline)** — it is the recognized-corpus +
  oracle plumbing pin. The dim-960 GIST headline run is GX10-gated.

---

## Files

- `make bench-repro` -> `bench/bench_repro.py` — the one-command assembler. Reuses
  `bench/graphrag_report.py` (recall) + `tools/real_corpus.py` (oracle) +
  `tools/fetch_dataset.py` (pin) so no number has a second implementation to drift.
- `bench/results/bench_repro_metrics.json` / `bench_repro_report.md` — emitted
  metrics + rendered table.
- `tools/fetch_dataset.py` — pinned public-ANN registry + SHA256-verified download.
- `tools/real_corpus.py` — real-embedding loader, topical-graph synthesis, exact
  oracle, recall grading.
- `bench/graphrag_report.py` — the HotpotQA GraphRAG recall sweep (the headline).
- `baseline/TUNING.md` + `baseline/sm2.py` — the committed tuned multi-store config.
- `tests/test_bench_repro.py` — offline unit tests (no network, no engine).

# SM-2 at 1M on the GX10 — live head-to-head vs Milvus+Neo4j+Postgres (DEV-1332)

> **Date:** 2026-07-02 (run executed on the GX10 / DGX Spark, `tridb/msvbase:gx10-batch`)
> **Status:** MEASURED — honest negative for v1 at this regime. TriDB answers the canonical
> query essentially correctly at 1M (0.958 recall@5 vs an exact oracle) but LOSES SM-2
> (0/24 queries) to the correctly-configured baseline. The regime is the **filter-first**
> regime the FROZEN FR-6 heuristic already identifies — and v1 `tjs()` only implements
> vector-first. **DEV-1290 (the filter-first physical path) is the at-scale SM-2 blocker.**
> **Issues:** DEV-1332 (this run), DEV-1284 (SM-2 re-measure), DEV-1290 (remediation).

## TL;DR

At 1M entities with a realistic single-src graph predicate (2 000 of 1M reachable, ~0.12%
joint selectivity), on identical corpora and identical client-side end-to-end methodology:

| Configuration | median ms | recall@5 vs exact oracle | fills k? |
|---|---:|---:|---|
| Baseline, committed config (fetch k×32, nprobe=128) | 55 | 0.233 | **no — <k on all 24** |
| Baseline, fetch k×2000, nprobe=128 | 56 | 0.208 | yes (wrong answers) |
| Baseline, fetch 16 380, nprobe=512 | 76 | 0.467 | yes |
| Baseline, fetch 16 380, nprobe=2048 | 76 | 0.892 | yes |
| Baseline, fetch 16 380, nprobe=4096 (exact IVF scan) | 88 | 1.000 | yes |
| **TriDB `tjs()` term_cond=10 000 (vector-first)** | **171** (132–275) | **0.958** | yes |

Three findings, in order of importance:

1. **The 2k-era "12–15× faster, exact parity" SM-2 headline does NOT survive 1M scale in
   this regime.** TriDB won 0/24 queries; the correctly-configured baseline is ~2× faster
   at comparable-or-better recall (88 ms @ 1.000 vs 171 ms @ 0.958). Do not quote the 2k
   number for at-scale claims.
2. **The multi-store baseline's committed configuration is structurally WRONG at 1M** —
   its k×32 ANN over-fetch finds ≈0.2 qualifying candidates per query in expectation
   (returned <k answers on every query, 0.233 recall). Correctness requires brute force:
   only nprobe=4096 (an exact scan of every IVF list) reaches recall 1.0. The "faster
   wrong answer" failure mode the GTM narrative predicts is real and measured — but so is
   the brute-force fix, and at 1M×128 it costs only ~33 ms over the broken config.
3. **This regime is the FR-6 filter-first regime, and v1 has no filter-first path.**
   Joint selectivity 0.12% ≪ the 10% `tridb.join_order_selectivity_threshold`; the FROZEN
   heuristic (and, as of `729dd30`, the live Stage-2 lowering) selects `filter_first` here.
   The vector-first body pays ~10 000 ANN-stream candidate probes (171 ms) to find ~1 200
   qualifying rows a filter-first drain would enumerate directly — in-process, without the
   baseline's ~23 ms out-of-process Neo4j hop. **DEV-1290 is not an optimization; it is
   the at-scale competitiveness requirement.**

## Setup

* **Corpus** (shared deterministic generator, seed 42): 1 000 000 entities, dim 128
  (unit-normalized gaussian), 24 hubs × fanout 2 000 (48 000 edges), ts uniform in
  [19 000, 20 000], window 600 (~60% pass), 24 queries (one per hub, query vector jittered
  0.35 around the hub centroid), k=5.
* **Joint predicate selectivity:** reachable 2 000/1M × window ≈ **0.12%** (~1 208
  qualifying rows/query, measured). This is the realistic shape of the canonical
  single-src query at 1M — real 1-hop neighborhoods do not grow with corpus size.
* **TriDB side:** `tridb/msvbase:gx10-batch` (the merged validated engine), canonical
  `tjs()` at `term_cond=10 000` (an operating point from the DEV-1169 100k×768 curve),
  psql `\timing` round-trip, warm connection, median of 7 runs.
* **Baseline side:** live Milvus 2.4.5 (IVF_FLAT nlist=4096 per the TUNING.md ~4·√N rule)
  + Neo4j 5.20 (indexed 1-hop) + Postgres 16 (indexed ts window), merged app-side,
  `perf_counter` end-to-end, warm clients, median of 7 runs. Same corpus, same queries.
* **Exact oracle:** the corpus is deterministic, so the TRUE top-5 (reachable ∩ window,
  ranked by exact L2) is computed offline per query (`bench/results/sm2_1m_exact_oracle.json`).
  All recall numbers above are against this oracle — NOT baseline-as-oracle. (Run 1
  scored "SM-4 parity 0.21" against the committed baseline; that number is meaningless
  because the committed baseline under-returns. The oracle is the honest referee.)

## Why each side costs what it costs

* **TriDB 171 ms:** the vector-first body walks the HNSW stream in distance order,
  probing graph+ts per candidate; at 0.12% selectivity it must examine ~10 000 candidates
  (1% of corpus — SM-3 fine) to fill k=5. Latency scales with 1/selectivity, not corpus
  size. Recall 0.958 comes from HNSW stream quality on unstructured vectors.
* **Baseline 88 ms (correct config):** ~23 ms Neo4j 1-hop (2 000 ids out-of-process)
  + ~57 ms Milvus exact IVF scan fetching 16 380 candidates + ~7–10 ms Postgres window
  + merge. Its correctness is bought by brute force: nprobe=4096 IS a full scan of the
  IVF lists; nprobe=2048 already degrades to 0.892, nprobe=128 to ~0.21.
* **The missing contender — filter-first TriDB (DEV-1290):** drain ~1 200 qualifying rows
  (native adjacency iterator + ts check, in-process) and rank them exactly. The baseline
  pays 23 ms for the same graph expansion over Bolt; the native AM does it without leaving
  the process, and 1 200 exact dim-128 distances are sub-ms. The projected operating point
  is recall 1.0 at a small fraction of the baseline's 88 ms — this run is the
  quantitative case for building it.

## Honesty box

* Corpus vectors are **synthetic uniform-random** — the hardest case for ANN structure
  (both for Milvus IVF recall and for TriDB's HNSW stream). The real-data companion
  (filtered SIFT-1M, `docs/benchmark_gx10_merge_validation_v0.1.0.md`) shows recall 1.000
  at 42–91 ms for the fused relational-filter scan; the number here isolates the
  *selective-graph-predicate* regime, which SIFT-filtered does not exercise.
* TriDB's 0.958 is one point on its own `term_cond` curve; higher tc buys recall with
  latency (DEV-1169 curve). No tc setting rescues vector-first here — filling k from a
  0.12%-selective predicate via a distance-ordered stream is the structural cost.
* Baseline legs run sequentially (as `baseline/sm2.py` measures them); an app could
  parallelize graph+vector, floor ≈ the 57 ms vector leg. Noted, not simulated.
* SM-1 (intermediate size) still favors TriDB structurally: the correct baseline moves
  ~19 600 rows/query app-side (16 380 ANN + 2 000 graph + ~1 200 filtered); TriDB's
  intermediate is its bounded k-PQ, in-process. The SM-2 loss is about *time*, not the
  intermediate-blowup thesis.
* One run, one box, 24 queries, 7 samples each; medians are tight (per-query spreads in
  `sm2_1m_metrics.json`).

## Repro

```bash
# full pipeline (TriDB side + committed-config baseline + compare), on the GX10:
BENCH_ENTITIES=1000000 BENCH_DIM=128 BENCH_HUBS=24 BENCH_FANOUT=2000 \
BENCH_QUERIES=24 BENCH_K=5 BENCH_TERMCOND=10000 SM2_RUNS=7 \
BASELINE_NLIST=4096 BASELINE_NPROBE=128 \
  scripts/bench_sm2.sh tridb/msvbase:gx10-batch

# baseline correctness sweep (stack stays loaded; manifest regenerated deterministically):
BASELINE_NLIST=4096 BASELINE_NPROBE={512|2048|4096} BASELINE_ANN_FANOUT=3276 \
  python baseline/sm2.py --manifest manifest.json --seed 42 --k 5 --runs 7 --no-load --out ...
```

Artifacts: `bench/results/sm2_1m_metrics.json` (run 1: TriDB samples + committed-config
baseline), `sm2_1m_baseline_ff2000.json`, `sm2_1m_baseline_np{512,2048,4096}.json`,
`sm2_1m_exact_oracle.json` (truth sets + per-side recall).

## Consequences

1. **DEV-1290 (filter-first `tjs()` body) becomes the top engine priority** — it is the
   difference between losing 2× and winning at 1M in the regime the canonical query
   actually inhabits at scale. The Stage-2 lowering (`729dd30`) already selects
   `filter_first` here; the operator just cannot execute it yet.
2. **GTM: do not lead with an at-scale SM-2 claim until DEV-1290 lands.** The provable
   claims today: one-WAL cross-modal consistency (FR-7, proven), SM-1 intermediate
   reduction (structural), correctness-at-selectivity where the bolt-on stack silently
   degrades (0.233 recall on its committed config — measured here).
3. After DEV-1290: rerun this exact corpus via the lowering (which will pick
   filter-first) and publish the three-way point: TriDB-ff vs TriDB-vf vs correct baseline.

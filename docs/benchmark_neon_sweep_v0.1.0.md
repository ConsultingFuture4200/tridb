# TriDB NEON Engine — Index-Quality × term_cond Sweep (DEV-1286) v0.1.0

**TL;DR.** With the ARM **NEON** distance kernel ([[DEV-1234]]) in place, the HNSW
**index-quality reloptions** (`m` / `ef_construction`, [[DEV-1286]]) and the `term_cond`
search-depth knob were swept on the **live forked-MSVBASE engine rebuilt with NEON +
reloptions**, on the GX10 (DGX Spark, aarch64), over a **20,000-entity / dim-128** corpus,
**8 queries**, **k=10**. Every cell is **live-measured** (real `tjs()` answer sets,
`tjs_candidates_examined()`, EXPLAIN ANALYZE Execution Time); recall is graded against an
**exact numpy top-k oracle** (host-side, so it needs no engine and sidesteps the fork's
double-scan bug). This is the first run that reports **latency in ms on real ARM SIMD** — the
gate the GTM plan ([[gtm_opensource_v0.1.0]] R1) named as blocking a public benchmark.

## Results (live, NEON engine, 20k×128, k=10)

Index build time (NEON kernel; on the scalar fallback these were impractical — see below):

| index config | build time |
|---|---|
| `m=16, ef_construction=200` (hnswlib default) | **3.17 s** |
| `m=32, ef_construction=400` (high quality) | **5.38 s** |

Recall / effort / latency curve:

| config | `term_cond` | recall@10 | corpus examined | median latency |
|---|---|---|---|---|
| m16/ef200 | 20   | **1.00** | 2.18% | **1.82 ms** |
| m16/ef200 | 50   | 1.00 | 2.34% | 1.78 ms |
| m16/ef200 | 200  | 1.00 | 3.10% | 2.29 ms |
| m16/ef200 | 1000 | 1.00 | 7.11% | 4.27 ms |
| m32/ef400 | 20   | **1.00** | 2.18% | 2.21 ms |
| m32/ef400 | 50   | 1.00 | 2.34% | 2.15 ms |
| m32/ef400 | 200  | 1.00 | 3.10% | 2.63 ms |
| m32/ef400 | 1000 | 1.00 | 7.11% | 4.97 ms |

Recall@10 was verified exact: for every query the live `tjs()` top-10 equals the numpy
oracle top-10 (8/8 queries, set-identical).

## What this shows (and what it does not)

1. **NEON un-sandbags the engine, end to end.** At the recall@10 = 100% operating point
   (`term_cond=20`, default index) the live query latency is **1.82 ms median** at **2.18%
   of the corpus examined**. This is the first *real* latency number for the canonical query
   on the target ISA; before NEON every distance was scalar and the figure was wrong-low.
   Higher `term_cond` only trades latency/examined for recall it has already saturated.

2. **The reloptions ([[DEV-1286]]) work and the higher-quality build is now affordable.**
   `CREATE INDEX ... WITH (m=32, ef_construction=400)` is accepted and built in **5.4 s**.
   `m=32/ef_construction=400` does ~4× the per-insert distance work of the defaults; on the
   pre-NEON **scalar** build DEV-1286 recorded that quality bump as single-core-bound and
   impractical to finish at 100k×768 — which is exactly why the lever was gated on NEON.
   NEON removes that gate.

3. **At this scale recall is saturated, so index quality buys latency, not recall.** Both
   configs return identical, exact top-10 at every `term_cond`; the `m=32/ef=400` index is
   *slower* to query (more neighbours per node) for no recall gain. The regime where a
   higher-quality index reaches 100% recall at a *lower* examined-% than the default is the
   **100k / dim-768** scale, where the default `M=16` index needed `term_cond≈10000` /
   20.1% examined for exact parity ([[DEV-1169]] curve). Reproducing that full recall curve
   *with NEON latency attached* is the **100k / dim-768 headline run, now done — see below.**

## Headline run — 100k × dim-768 (the recall curve at scale, NEON, k=10)

Run on the GX10 with the same NEON+reloptions engine, 100,000 entities × **dim 768**, 8 queries,
k=10, recall graded against the exact numpy oracle. Here the curve **bites** (it was saturated at
20k/128): `term_cond` trades real recall for real examined/latency.

| config | `term_cond` | recall@10 | corpus examined | median latency |
|---|---|---|---|---|
| m16/ef200 | 20   | **0.9625** | 3.28% | **36.3 ms** |
| m16/ef200 | 50   | 0.9625 | 3.31% | 36.4 ms |
| m16/ef200 | 200  | 0.9875 | 3.51% | 37.1 ms |
| m16/ef200 | 1000 | **1.0000** | 4.44% | 41.1 ms |
| m16/ef200 | 5000 | 1.0000 | 8.46% | 57.4 ms |
| m32/ef400 | 20   | 0.9625 | 3.28% | 39.4 ms |
| m32/ef400 | 1000 | 1.0000 | 4.44% | 44.7 ms |
| m32/ef400 | 5000 | 1.0000 | 8.46% | 62.3 ms |

Index build (NEON): m16/ef200 = **137 s**, m32/ef400 = **489 s** — both feasible only because of the
NEON kernel; on the scalar fallback these are single-core-bound and impractical at this scale (the
DEV-1286 thesis, now confirmed at 100k/768). Artifacts: `bench/results/neon_sweep_100k_metrics.json`,
`neon_sweep_100k_raw.txt`, `sweep100k_manifest.json`.

**What it shows:** the canonical tri-modal query reaches **recall@10 = 96.25% at ~36 ms / 3.3%
examined** (default index, `term_cond=20`) and climbs to **exact (100%) at ~41 ms / 4.4% examined**
(`term_cond=1000`) — a real recall/effort/latency curve on real ARM SIMD, well under the 25% TR-1
corpus-examined ceiling at every operating point. This is the proof-at-scale the GTM plan gated on.

**Honest negative finding:** the higher-quality index (`m=32, ef_construction=400`) gives **identical
recall and examined** to the default at every `term_cond` here — it only costs more to build (489 s vs
137 s) and slightly more to query. At this corpus the recall lever is `term_cond`, **not** index
quality; the m/ef reloptions are exposed and work, but do not move the curve on this workload. (A
clustered/real-embedding corpus may differ — that's the public-dataset run, `docs/benchmark_public_v0.1.0.md`.)

## Live-measured vs gated

- **LIVE (this run, real engine on the GX10):** index build time, per-query `tjs()` answer
  set, `tjs_candidates_examined()` (examined-%), and EXPLAIN ANALYZE Execution Time, for both
  index configs × four `term_cond` values. The engine was the forked MSVBASE `vectordb.so`
  **rebuilt in-image with both the NEON patch and the reloptions patch** (so this also proves
  both patches build and run in the real engine, not just standalone).
- **Modeled / not claimed:** no multi-system baseline here (SM-2 head-to-head stays gated, as
  in [[benchmark_results_v0.1.0]]); the recall oracle is the exact numpy top-k, not a second
  engine.
- **Gated (the headline, GX10):** the **100k / dim-768** run where `term_cond` and index
  quality actually move recall — that produces the full recall/effort/latency curve for the
  public writeup. The harness here scales to it directly (raise `--entities/--dim`), but the
  at-scale run is the documented 128 GB / headline item and is run on a quiet GX10.

## Reproduce

The whole sweep is one committed command, `make sweep` (`scripts/bench_gx10_sweep.sh`). It is the
sibling of `make bench-live`: it generates the sweep SQL + numpy oracle manifest with
`tools/sweep_corpus.py`, runs the recipe (build `graph_store_ext`, load the corpus, run the sweep in
one `psql` session capturing the `#SWEEP`/timing lines) on the LIVE engine in one container, and
grades the transcript back into `bench/results/`. The engine is used as the image built it from the
committed fork-patch chain (`scripts/lib/msvbase_patches.sh` — incl. `tridb_neon_l2_distance.patch`
+ `tridb_hnsw_reloptions.patch`), so no ad-hoc rebuild diverges from the chain. The SQL generation +
grading are hardware-independent and unit-checked; the live container run is GX10/engine-gated.

```bash
# Reproduce the committed 20k/128 result (defaults baked into the script):
make sweep

# Headline (GTM gate, DEV-1286 — run on a quiet GX10): same script, bigger args.
SWEEP_ENTITIES=100000 SWEEP_DIM=768 make sweep
```

The defaults match this run (`SWEEP_ENTITIES=20000 SWEEP_DIM=128 SWEEP_HUBS=16 SWEEP_FANOUT=200
SWEEP_QUERIES=8 SWEEP_K=10 SWEEP_INDEX_CONFIGS="16:200,32:400" SWEEP_TERMCONDS="20,50,200,1000"
SWEEP_SEED=42`); override any via the environment. Deterministic via the seed (default 42).
Artifacts for this run:
`bench/results/neon_sweep_metrics.json` (graded table), `bench/results/neon_sweep_raw.txt`
(auditable `#SWEEP` transcript + build timings), `bench/results/sweep_manifest.json`
(corpus/query/oracle manifest).

## Provenance / honesty notes

- 20k×128 is a **moderate** scale chosen so the sweep runs bounded on a shared box; it is
  NOT the 128 GB headline. Latency here is real; the recall *curve* is flat because the
  workload is easy at this scale. Do not quote these as the at-scale numbers.
- The NEON build-speedup figure (4.2× index build, same corpus) is from the controlled A/B
  in [[DEV-1234]] / STATUS, not this run (this run uses a different corpus per config-pair;
  its apples-to-apples comparison is m16 vs m32 build time, 3.2 s vs 5.4 s).

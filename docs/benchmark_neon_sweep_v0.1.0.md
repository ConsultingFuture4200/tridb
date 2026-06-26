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
   *with NEON latency attached* is the remaining GX10 headline run — see "Gated" below.

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

The sweep SQL + grader are hardware-independent and unit-checked; the live run is GX10/engine-gated.

```bash
# 1. generate the sweep SQL + numpy oracle manifest (runs anywhere)
python3 -m tools.sweep_corpus --entities 20000 --dim 128 --hubs 16 --fanout 200 \
  --queries 8 --k 10 --index-configs "16:200,32:400" --term-conds "20,50,200,1000" \
  --seed 42 --sql-out sweep.sql --manifest-out sweep_manifest.json

# 2. on the GX10: rebuild vectordb.so with the NEON + reloptions patches, build graph_store,
#    load the corpus and run sweep.sql in one psql session, capturing #SWEEP/timing lines.
#    (recipe: scripts/lib/msvbase_patches.sh applies tridb_neon_l2_distance.patch +
#     tridb_hnsw_reloptions.patch; see the in-image runner used for this result.)

# 3. grade the captured transcript (runs anywhere)
python3 -m tools.sweep_corpus --report sweep_raw.txt --manifest sweep_manifest.json
```

Deterministic via `--seed` (default 42). Artifacts for this run:
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

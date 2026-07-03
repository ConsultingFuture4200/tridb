# TriDB NEON Engine — Index-Quality × term_cond Sweep (DEV-1286) v0.1.0

> **REGENERATED 2026-07-02** on the merged-batch engine `tridb/msvbase:gx10-batch`
> (`master` @ the 010-023 remediation merge). **All tables below are the current numbers.**
> They differ from the original 2026-06-26 run because the synthetic **oracle** generator's
> tie-break changed (`np.argsort(d2)` → `np.lexsort((cd, d2))`, "ties broken by id like
> `ORDER BY d2, id`", commit `f604c27`) — a stricter, more-correct oracle, NOT an engine change.
> The engine's recall is unchanged across the merge: an A/B with vs. without the relaxed-mono
> executor guard (plan 022) is byte-identical. Full attribution + the SIFT-1M headline:
> `docs/benchmark_gx10_merge_validation_v0.1.0.md`. This regen ran the **m16/ef200** config only
> (index quality is not the recall lever — see the finding below); the extended `term_cond` grid
> now reaches the true saturation point under the stricter oracle.

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

Index build time (NEON kernel, m16/ef200): **2.79 s**. (m32/ef400 not re-run this regen; it
builds slower for identical recall — see the finding below.)

Recall / effort / latency curve (m16/ef200; extended `term_cond` grid):

| config | `term_cond` | recall@10 | corpus examined | median latency |
|---|---|---|---|---|
| m16/ef200 | 20    | 0.900 | 2.38% | **1.87 ms** |
| m16/ef200 | 50    | 0.9125 | 2.57% | 2.12 ms |
| m16/ef200 | 200   | 0.950 | 3.49% | 2.13 ms |
| m16/ef200 | 1000  | **1.000** | 8.58% | 4.52 ms |
| m16/ef200 | 5000  | 1.000 | 28.58% | 12.82 ms |
| m16/ef200 | 10000 | 1.000 | 53.58% | 23.39 ms |

Under the stricter (id-tie-broken) oracle, recall at 20k×128 is **no longer saturated at low
`term_cond`** (the original run's looser oracle read 1.00 everywhere): it climbs 0.90 → exact
**1.000 at `term_cond=1000` / 8.58% examined / 4.52 ms** — still under the 25% TR-1 corpus-examined
ceiling. `term_cond ≥ 5000` reaches the same exact recall but blows past the ceiling (28.6% / 53.6%
examined) for no gain — i.e. `term_cond=1000` is the operating point here.

## What this shows (and what it does not)

1. **NEON un-sandbags the engine, end to end.** At the recall@10 = 100% operating point
   (`term_cond=1000`, default index) the live query latency is **4.52 ms median** at **8.58%
   of the corpus examined** — well under the 25% TR-1 ceiling. This is a *real* latency number
   for the canonical query on the target ISA; before NEON every distance was scalar and the
   figure was wrong-low. (The original run reported 1.82 ms at `term_cond=20`, but that read
   100% only under the looser pre-`f604c27` oracle; the stricter oracle now puts exact recall
   at `term_cond=1000`.)

2. **The reloptions ([[DEV-1286]]) work and the higher-quality build is now affordable.**
   `CREATE INDEX ... WITH (m=32, ef_construction=400)` is accepted and built in **5.4 s**.
   `m=32/ef_construction=400` does ~4× the per-insert distance work of the defaults; on the
   pre-NEON **scalar** build DEV-1286 recorded that quality bump as single-core-bound and
   impractical to finish at 100k×768 — which is exactly why the lever was gated on NEON.
   NEON removes that gate.

3. **Index quality buys latency, not recall.** Under the stricter oracle the curve is no
   longer saturated at low `term_cond` (it climbs to exact recall at `term_cond=1000`), but the
   index-quality finding is unchanged and confirmed twice on the merged engine: `m=32/ef=400`
   returns **identical recall/examined** to `m16/ef200` at every `term_cond` — it only costs
   more to build and slightly more to query. At this corpus `term_cond` is the recall lever, not
   index quality. The full recall/latency curve at scale is the **100k / dim-768 headline run —
   see below.**

## Headline run — 100k × dim-768 (the recall curve at scale, NEON, k=10)

Run on the GX10 with the same NEON+reloptions engine, 100,000 entities × **dim 768**, 8 queries,
k=10, recall graded against the exact numpy oracle. Here the curve **bites** (it was saturated at
20k/128): `term_cond` trades real recall for real examined/latency.

| config | `term_cond` | recall@10 | corpus examined | median latency |
|---|---|---|---|---|
| m16/ef200 | 20    | **0.8625** | 3.06% | **42.1 ms** |
| m16/ef200 | 50    | 0.8625 | 3.10% | 45.5 ms |
| m16/ef200 | 200   | 0.9000 | 3.40% | 49.0 ms |
| m16/ef200 | 1000  | 0.9750 | 4.86% | 64.3 ms |
| m16/ef200 | 5000  | **1.0000** | 9.70% | 104.6 ms |
| m16/ef200 | 10000 | 1.0000 | 14.74% | 148.1 ms |

Index build (NEON, m16/ef200): **151 s** — feasible only because of the NEON kernel; on the scalar
fallback this is single-core-bound and impractical at this scale (the DEV-1286 thesis, confirmed at
100k/768). Artifacts: `bench/results/neon_sweep_regen100k768.json` (on the Spark).

**What it shows:** the canonical tri-modal query reaches **recall@10 = 86.25% at ~42 ms / 3.06%
examined** (default index, `term_cond=20`) and climbs to **exact (100%) at ~105 ms / 9.70% examined**
(`term_cond=5000`) — a real recall/effort/latency curve on real ARM SIMD, under the 25% TR-1
corpus-examined ceiling at every operating point (up to `term_cond=10000` = 14.7% examined). The
`term_cond` knob is the operating-point lever; `term_cond=1000` gives 97.5% at 64 ms if you accept a
near-exact answer for lower latency. (These numbers are lower than the original 2026-06-26 run because
the oracle got stricter — `f604c27`, see the top banner — not because the engine changed.)

**Honest negative finding (unchanged):** the higher-quality index (`m=32, ef_construction=400`) gives
**identical recall and examined** to the default at every `term_cond` — confirmed twice on the merged
engine (the with/without-plan-022 A/B ran both configs). It only costs more to build (~515 s vs 151 s)
and slightly more to query. At this corpus the recall lever is `term_cond`, **not** index quality; the
m/ef reloptions are exposed and work, but do not move the curve on this workload. (A clustered/real-
embedding corpus may differ — that's the public-dataset run, `docs/benchmark_public_v0.1.0.md`.)

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

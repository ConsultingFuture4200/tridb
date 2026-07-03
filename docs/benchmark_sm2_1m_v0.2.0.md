> **⚠ Measured on the v0 heap graph store (ADR-0013), not the v1 native AM.** The v1 native-AM re-measurement is `docs/benchmark_sm2_1m_v0.3.0.md`; this doc's analysis of vector-first vs filter-first at 1M remains valid (the operator bodies are store-independent) but the absolute numbers ran on v0.

# SM-2 at 1M on the GX10, v0.2.0 — the three-way point: filter-first lands (DEV-1290)

> **Date:** 2026-07-02 (same corpus, queries, seed, box, and methodology as v0.1.0)
> **Status:** MEASURED — the v0.1.0 negative is REVERSED by the DEV-1290 filter-first body.
> **Supersedes the v0.1.0 verdict; keeps its data.** `docs/benchmark_sm2_1m_v0.1.0.md` measured
> the vector-first-only engine and correctly concluded "filter-first is the at-scale blocker."
> This report adds the missing row: the engine with `tridb_tjs_filter_first.patch`
> (`tridb/msvbase:gx10-ff`), driven at the SAME operating point the FR-6 heuristic selects
> automatically through the lowering.
> **Issues:** DEV-1290 (the operator), DEV-1285 (join-order integration), DEV-1332/DEV-1284
> (the v0.1.0 measurement this completes).

## TL;DR — the three-way at 1M

Identical 1M×128 corpus (24 hubs × fanout 2000, ~0.12% joint selectivity, ~1 208
qualifying/query), identical 24 queries and k=5, identical client-side end-to-end
methodology (warm connection, median of 7), all recall scored against the same EXACT
offline oracle:

| Engine / config | median ms | recall@5 vs exact | SM-2 wins |
|---|---:|---:|---|
| Baseline, committed config (k×32, nprobe=128) | 55 | 0.233 (<k answers) | — |
| Baseline, correct config (nprobe=4096 exact IVF, fetch 16 380) | 88 | 1.000 | — |
| TriDB `tjs()` **vector-first**, tc=10 000 (v0.1.0) | 171 | 0.958 | 0/24 vs baseline |
| **TriDB `tjs()` `filter_first` (DEV-1290)** | **4.7** (4.4–5.5) | **1.000** | **24/24 vs baseline** |

- **SM-2 = 100%** (24/24, target ≥80%) at a **median 18.3× latency advantage** over the
  baseline *configured to be correct* — not the strawman committed config (which is 12× slower
  than TriDB-ff AND wrong).
- **SM-4 = 100% exact-set parity**: both TriDB-ff and the correct baseline return exactly the
  oracle top-5 on all 24 queries, so the answers are not merely fast — they are the true ones.
- **SM-3**: the drain examines exactly the qualifying set (~1 208 rows/query = 0.12% of the
  corpus, ≪ the 25% TR-1 ceiling), reported by `tjs_candidates_examined()`.
- The FR-6 heuristic (frozen decision core + the Stage-2 lowering) selects `filter_first` at
  this selectivity on its own — no manual tuning. The operating point is chosen by the system,
  which is the whole point of DEV-1170/1285/1290.

## Why the margin is structural, not tuned

The baseline's correct configuration must brute-force the vector leg (nprobe=4096 IS an exact
scan of every IVF list: ~57 ms) and still pays ~23 ms to pull the 2 000-id graph expansion
out-of-process over Bolt, plus the relational round-trip and the app-side merge of ~19 600
intermediate rows. TriDB's filter-first body does the SAME logical work in-process: the native
reachability probe, one bounded-batch SPI drain of the ~1 208 qualifying rows, and 1 208 exact
128-dim distances into a k-bounded PQ — 4.7 ms end-to-end, no intermediate leaves the process
(SM-1). Each system returns the identical, exactly-correct answer; one of them crosses three
process boundaries and materializes 19.6k rows to do it. That is the tri-modal-in-one-process
thesis, measured.

## Honesty box

- Same synthetic uniform-random corpus caveats as v0.1.0 (hardest case for ANN structure;
  it is the vector legs this punishes — note it punished the BASELINE's IVF recall, not the
  filter-first drain, which is exact regardless of embedding distribution).
- The baseline legs run sequentially as measured; a parallelizing app floors at its ~57 ms
  exact vector leg — still 12× above TriDB-ff.
- Filter-first's cost scales with the qualifying-set size, not corpus size. At this corpus's
  0.12% selectivity that is ~1.2k rows; a far broader predicate flips the FR-6 decision back
  to vector-first (measured at ~80% selectivity in `test/join_order_integration_test.sql`) —
  the crossover is owned by the committed `tridb.join_order_selectivity_threshold` heuristic.
- Vector-first row reproduced from v0.1.0 (same corpus/queries; not re-run on the `gx10-ff`
  image — the patch leaves that body untouched, verified by the byte-identical answers in
  `test/tjs_filter_first_test.sql`).
- One box, one run, 24 queries × 7 samples; per-query spreads are tight (4.4–5.5 ms).

## Repro

```bash
# engine with the filter-first body (offline GX10 recipe):
scripts/gx10build.sh --skip-clone --image tridb/msvbase:gx10-ff

# TriDB side, filter-first pinned (same corpus/seed as v0.1.0):
python tools/bench_sm2_corpus.py --entities 1000000 --dim 128 --hubs 24 --fanout 2000 \
  --queries 24 --k 5 --window 600 --seed 42 --runs 7 --term-cond 10000 \
  --join-order filter_first --sql-out sm2_ff.sql --manifest-out manifest.json
# then the standard bench_sm2.sh TriDB container block over sm2_ff.sql.
# Baseline rows: v0.1.0 (sm2_1m_baseline_np4096.json etc.), same corpus + queries.
```

Artifacts: `bench/results/sm2_1m_ff_raw.txt` (this run's timing transcript),
plus the v0.1.0 set (`sm2_1m_metrics.json`, `sm2_1m_baseline_np*.json`,
`sm2_1m_exact_oracle.json`).

## Consequences

1. **The at-scale GTM claim is live again, now in its honest form:** at 1M with realistic
   graph selectivity, TriDB answers the canonical query exactly, 18× faster than the bolt-on
   stack configured to match its correctness — and the bolt-on stack's *default* tuning
   silently returns wrong answers (recall 0.233). Lead with correctness-per-millisecond, not
   a bare ×.
2. DEV-1290's done-criteria are met (join_order executes, TR-1 on both paths, FR-6
   end-to-end on the GX10, vector-first untouched); DEV-1285's "decision changes execution"
   criterion is met through the full lowering.
3. Remaining follow-ups: promote `gx10-ff` to the canonical engine tag after a full
   `make graph-test` soak, and re-run the public-dataset benchmarks through the lowering so
   published numbers always carry the heuristic-selected operating point.

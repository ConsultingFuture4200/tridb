# TriDB Phase-3 Benchmark Results — LIVE engine (DEV-1172 / DEV-1173) v0.1.0

**TL;DR.** The ONE canonical query (spec §5) was driven on the **LIVE forked-MSVBASE
engine** (`tridb/msvbase:dev`, x86 standin) over a **2000-entity / dim-32** corpus
across **12 queries** at **k=5**. Three of the five spec §7 success metrics are
**live-measured and PASS** (SM-3/4/5); SM-2 (latency head-to-head) is reported
TriDB-side only and is explicitly gated; **SM-1 FAILS on this standin corpus (1.07×
vs the ≥5× target)** under the corrected `max(k, reached)` accounting — see the note
below. Answer correctness is triple-verified: the live `tjs()` result equals the exact
in-DB SQL oracle on **12/12** queries AND the in-process baseline model.

> [!WARNING]
> **SM-1 correction (2026-07-03).** An earlier revision of this report headlined SM-1 =
> **32.0×** (PASS). That figure recorded TriDB's peak intermediate as `k` (~60 rows). The
> current harness (`live_report.py`) uses the honest peak `max(k, reached)` — the top-k heap
> PLUS the reachable-id set the SRF TJS precomputes at Open — which is **~1799 rows → 1.07×,
> a FAIL** on this 2k/dim-32 corpus. **SM-1 is a deterministic row-count ratio, not a timing:
> it is hardware-independent, so the GX10 does NOT restore 32×.** The ratio at the 128 GB / 1M
> headline scale is genuinely unmeasured and may differ (selectivity-dependent), but under the
> corrected accounting, not the retired `k` rule. A real ≥5× reduction requires the *streaming
> graph predicate* redesign (drop the reachable-set precompute), which is an engineering item,
> not a hardware one.

| SM | Metric | Target (spec §7) | LIVE result | Verdict | Basis |
|----|--------|------------------|-------------|---------|-------|
| SM-1 | Intermediate-result reduction | ≥ 5× | **1.07×** (baseline 1920 rows vs TriDB 1799) | **FAIL** (standin corpus) | live (TriDB) vs in-process baseline model; corrected `max(k, reached)` accounting |
| SM-2 | Latency vs multi-system baseline | lower on ≥ 80% | **1.199 ms mean** (TriDB-side only) | GATED | live TriDB EXPLAIN ANALYZE; no fair baseline runtime here |
| SM-3 | Corpus examined (k=5, worst case) | < 25% | **6.4%** (max 128 / 2000) | PASS | live `tjs_candidates_examined()` |
| SM-4 | Answer-set parity | ≥ 99% | **100.0%** (12/12 exact) | PASS | live `tjs()` vs exact in-DB oracle vs baseline model |
| SM-5 | Transaction atomicity | 100% | **100%** | PASS | FR-7 proven by `scripts/txn_atomicity_test.sh`, reused |

Artifacts (committed): `bench/results/bench_live_metrics.json` (full `BenchmarkReport`),
`bench/results/report_live.html` (read-once report), `bench/results/bench_live_raw.txt`
(auditable transcript: every `#BENCH` line + per-query EXPLAIN ANALYZE plan).

## What is LIVE-measured vs modeled vs gated

- **LIVE (real engine, this standin):** the TriDB side of SM-1 (peak intermediate),
  SM-3 (candidates examined), SM-4 (answer set), and per-query latency. Every query
  ran the canonical query through the real `tjs(...)` operator inside the forked
  Postgres process; numbers come from the engine, not a model.
- **Modeled:** the **baseline** side. The multi-system baseline (Neo4j + Milvus +
  Postgres, AkasicDB Scenario 2) is replayed by the in-process materialize-transfer-
  prune model (`bench/live_report.py:baseline_query_canonical`) on the **same corpus**.
  It over-fetches `k×32` on the ANN leg (no graph/time pushdown), materializes the full
  reachable pair set, and merges app-side — the intermediate blowup SM-1 measures
  (peak 160 rows/query vs TriDB's peak of **max(k, reached)**: the bounded top-k heap PLUS
  the reachable-id set the current SRF TJS precomputes once at Open, `graphReachableT`). The
  model's answer set is the realized-canonical ground truth, so SM-4 parity is meaningful.
- **Gated (NOT run here):**
  - **SM-2 head-to-head.** Comparing the live TriDB latency against a zero-runtime
    model is not fair, so no SM-2 win is claimed. A real SM-2 needs the multi-system
    stack (`make baseline-up` → Neo4j+Milvus+Postgres) with a wired live baseline
    driver, or the GX10 run. We report the live TriDB-side latency only (mean 1.2 ms,
    range 0.67–2.02 ms).
  - **128 GB headline scale** — GX10-only (ARM64 + CUDA, 128 GB). Not attempted.

## Correctness verification (three independent witnesses)

For every query the live `tjs()` answer set was checked against:
1. an **exact in-DB SQL oracle** — a plain seqscan computing true L2 over the same
   stored `float8[]` embeddings, restricted to `graph_store.neighbors(src)` and the
   timestamp window, run on a **clean backend before any `tjs` scan** (PHASE A of the
   generated SQL); and
2. the **in-process Python baseline model** on the rebuilt corpus.

Result: **12/12 exact match** across all three. SM-4 = 100%.

## Engine-specific findings (real, from this run)

- **Early termination is the efficiency thesis in action.** SM-3 worst case is 6.4%
  of the corpus — the `tjs` operator settles the top-5 after streaming 64–128 HNSW
  candidates of 2000, without materializing the full *filtered* candidate stream or a
  cross product. It is NOT a pure no-materialization graph predicate, though: the SRF
  TJS precomputes the source's reachable-id set once at Open (bounded by out-degree), so
  TriDB's peak intermediate is `max(k, reached)`, not `k`. SM-1 compares that against the
  out-of-DB baseline's fully-materialized pair set (160 rows/query). > [!NOTE] The committed
  SM-1 figure predates this corrected accounting (peak was recorded as `k`); regenerate with
  `make bench-live` (live_report.py now reports `max(k, reached)`) before quoting a number.
- **Recall is now predicate-correct, not corpus-dependent (DEV-1169 scale fix).** The original
  `consecutive_drops` bound counted graph-rejected candidates (ADR-0007), so on a sparse graph the
  scan could stop before collecting all k and return a partial/empty set — an earlier standin draft
  saw 85% parity, and the first 100k/dim-768 GX10 run saw SM-4 = 5%. That is **fixed**: a drop now
  means only past-frontier (PQ full AND distance ≥ the k-th), so graph/relational rejections no
  longer trip early termination. Recall is a function of scan depth (`term_cond`) and HNSW index
  quality, **not** of whether the corpus has topical locality. The benchmark still models realistic
  Omni-RAG **topical locality** (a hub's neighbours are embedding-clustered and queries target the
  neighbourhood) for fidelity, but the engine no longer *depends* on it for correctness.
- **Two fork bugs were hit and worked around in the harness (not the engine):**
  1. a `tjs` scan corrupts a subsequent plain scan of the same table in one session
     (`docs/fork_segfault_double_scan.md`) — so all oracles run FIRST, before any
     `tjs` (PHASE A / PHASE B split);
  2. `array_agg(id ORDER BY d2, ...)` re-evaluates the correlated-subquery column
     `d2` incorrectly and returns a WRONG ordering — the oracle ranks via
     `row_number() OVER (ORDER BY d2)` and aggregates by the integer rank instead.
  Both are MSVBASE-fork defects exercised by the test harness; the `tjs` operator
  itself returned exact results on every query.

## Reproduce

```bash
# needs the image (scripts/x86build.sh --docker) + repo .venv with numpy
make bench-live
# or directly (override corpus size etc. via env):
BENCH_ENTITIES=2000 BENCH_DIM=32 BENCH_QUERIES=12 BENCH_K=5 \
  bash scripts/bench_live.sh tridb/msvbase:dev
```

The run is deterministic (`BENCH_SEED`, default 42): same corpus, same queries, same
numbers every time. Output lands in `bench/results/`. The off-engine glue
(SQL generation, transcript parsing, baseline model, SM derivation) is unit-tested by
`tests/test_bench_corpus.py` + `tests/test_bench_live_report.py` (run anywhere via
`make test`).

## Status of the gated work

- SM-2 fair head-to-head: stand up `make baseline-up` and wire a live baseline driver,
  OR run on the GX10. (The in-process model is intentionally not a latency claim.)
- 128 GB headline benchmark: GX10-only.

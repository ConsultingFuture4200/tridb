# DEV-1354 wiki-scale — actions #1 & #2 combined summary

> **FINAL VERDICT (2026-07-08) — the wiki value story, both halves.** After #1/#2 (below), the
> fusion head-to-head and the consistency demonstrator close the investigation:
> - **Fusion SPEED — PROVEN at N=200,000** (`docs/benchmark_wiki_fusion_v0.1.0.md`): fused `tjs_open`
>   beats the app-side Milvus→Neo4j→pgvector pipeline at matched recall@10 — **loopback 11.5× /
>   3.26×** (hop-1/2), **real-network 16.7× / 10.6×**. Win = eliminating cross-system round-trips.
> - **1M BLOCKED** on the fork's single-threaded / non-reproducible HNSW vector iterator
>   (`examined=0`; 0/2 fresh builds healthy) — documented future work, unblock path in the fusion
>   doc. The **Wall-3 batched edge loader validated at 1M** (38.99M edges, ~35 s, reconciled).
> - **I/O-locality thesis DEAD at this scale**: dim-384 is RAM-resident on the 128 GB Spark, so the
>   SM-3 page-locality thesis is untestable here (see the Milestone-B memo below). The speed win
>   carries the story, not page-locality.
> - **Consistency DEMONSTRATED** (`docs/benchmark_wiki_consistency_v0.1.0.md`,
>   `bench/wiki_consistency.py`): S1 atomicity TriDB **0** vs multi-store **42/42** injected torn;
>   S2 crash → TriDB atomic+durable vs multi-store torn orphan; S3 torn reads TriDB **1.0%** (heap
>   legs **0.0%**) vs multi-store **76.7%**. Multi-store tear is **inherent** (no cross-system txn)
>   and mitigable app-side at real cost; residual TriDB graph-leg tears are the v1 commit-visible
>   read path (snapshot isolation = DEV-1166). **Total value = fusion speed (3–17×) + cross-modal
>   consistency — one story, two halves; ADR-0017 prior stands, consistency half now MEASURED.**

TL;DR (2026-07-07). Two follow-ups on the real 6.9M-article enwiki corpus:

- **#1 fused-vs-cosine link recovery** — topology adds a **small but statistically
  significant** signal. RRF-fused cosine+Adamic-Adar recovers **overlap@10 = 0.1261**
  vs the **0.1101 cosine-only lower bound (+14.6%)**, after killing a reconstruction
  leak and ruling out a popularity confound. **Predictive-signal test only, no latency.**
- **#2 tjs_open head-to-head (Milestone A, executed spike)** — **BLOCKED-AT-SPIKE (partial).**
  The baseline now **reconciles at N=1,000,000 on all three legs** and the engine's **vector
  leg is durable at N=1,000,000**, but the fused `tjs_open` operator **produces no
  latency@recall point at any N** (its `float8[] <->` leg seqscans and cancels at the statement
  timeout, `examined=0`), and the engine's 1M graph holds only **21,945,976** edges vs the
  oracle/Neo4j **38,991,320** (~44% missing). So there is **no matched head-to-head** — only the
  baseline executing at parity scale (~90 ms e2e, recall@10=0.80). Verdict: **blocked, wrong
  regime** (dim-384 RAM-resident = compute-bound, not the I/O-bound thesis), consistent with
  ADR-0017. No latency-win table fabricated.

Docs: [`docs/benchmark_wiki_linkpred_fused_v0.1.0.md`](../../docs/benchmark_wiki_linkpred_fused_v0.1.0.md)
· [`docs/benchmark_wiki_scale_h2h_v0.2.0.md`](../../docs/benchmark_wiki_scale_h2h_v0.2.0.md)
(supersedes [`_v0.1.0.md`](../../docs/benchmark_wiki_scale_h2h_v0.1.0.md))

---

## #1 — Fused (graph+vector) link recovery vs cosine-only

Re-run on Spark GB10, 5000 sources, seed 42, cosine top-50 pool, metric = **link
recovery / precision@10 reconstruction proxy** (NOT true link prediction).

| Reranker | overlap@10 | Δ vs cosine | Note |
|---|---|---|---|
| cosine-only | 0.10996 | — | reproduces 0.1101 baseline (residual −0.00014 = CAGRA approx-kNN recall) |
| popularity / in-degree | 0.0955 | −0.014 | **below cosine → not a degree prior** (confound ruled out) |
| common-neighbor (corrected) | 0.1222 | +0.012 | leaky 0.1275 |
| Adamic-Adar (corrected) | 0.1246 | +0.015 | leaky 0.1328; reconstruction inflation +0.0082 (~36% of raw lift) |
| **RRF-fused cosine+AA (corrected)** | **0.1261** | **+0.0161 (+14.6%)** | **best method** |

- **Significance** (paired AA_corr − cosine): mean +0.0146, bootstrap 95% CI
  **[+0.0118, +0.0176]** (excludes 0), Wilcoxon **p = 2.6e-24**. Significant.
- **Orthogonality**: Spearman(cos, AA) = 0.172 (low) over 4775 pools; AA surfaces
  **0.4984 links/src that cosine misses** vs 0.352 the other way → partly orthogonal ranker.
- **Corrections applied** vs the first draft: added a leave-out-the-positives leakage
  control (raw AA-alone +21% was inflated by reconstruction leak); the old "AA-alone beats
  fusion" claim was a **leaky-AA artifact** — fusion is now narrowly best. Headline moved
  from raw +21% to honest **corrected +14.6% (fused) / +13.3% (AA)**.
- **Verdict**: topology is a real, load-bearing signal here, but **modest in absolute
  terms** (ceiling ~0.21). node2vec not included. Commit `e106409` (script `53b0fd8`).

### Caveats (#1)
- **dim-384 float32** embeddings, not the spec's dim-768 — compute regime, not the
  ADR-0017 I/O regime, so **no latency claim** is made.
- Metric is a **reconstruction proxy**, not held-out link prediction; the multi-hop /
  `tjs_open` implication is a motivated hypothesis, not measured.

---

## #2 — Fused `tjs_open` vs tuned multi-store, Milestone A (executed spike)

**Milestone A ran the load + stood the baseline up at N=1M, but produced NO matched
head-to-head.** What actually happened:

| Item | State |
|---|---|
| Baseline (all 3 legs) | **RECONCILED at N=1,000,000** — Milvus 1M (dim-384 HNSW/COSINE) + Neo4j 1M nodes / **38,991,320** rels + pgvector 1M rows |
| Baseline latency | ~90 ms e2e (Milvus ~3 + Neo4j ~83 + pgvector ~1.5), **recall@10 = 0.80** vs exact oracle — baseline executing at parity, NOT a TriDB result |
| Engine vector leg | **Durable at N=1,000,000** (HNSW built ~751 s single-threaded, super-linear) |
| Engine tri-modal (both legs + tjs_open + reconciliation) | **Verified at N=200,000 / 8,208,179 edges only** (tjs_open examined=128, bridges=190) |
| Engine graph leg @ 1M | **21,945,976 edges vs oracle/Neo4j 38,991,320** — ~44% missing; 38.99M insert unfinished >117 min |
| TriDB `tjs_open` @ 1M | **WALLS** — `float8[] <->` seqscans 1M×384, cancels at statement timeout, `examined=0` (`ready_to_run=false`) |
| Latency @ fixed EQUAL recall | **n/a — no TriDB point exists at any N** |
| Pages-touched (SM-3) | engine-internal diagnostic only; non-comparable, kept out of any headline |

- **Two walls, precisely located** (see `docs/benchmark_wiki_scale_h2h_v0.2.0.md`): (1) the fork
  `vectordb` AM exposes **no external-index ingest** — the Phase-1 49 s cuVS CAGRA index is
  **not reusable**, only single-threaded `CREATE INDEX … USING hnsw`; (2) the `tjs_open`
  vector leg does not bind `articles_hnsw`; (3) per-row `gph_insert_edge` is super-linear
  (8.2M edges=11.5 min at 200k → 38.99M unfinished at 1M).
- **Verdict**: **blocked-at-spike, wrong regime.** Baseline executes at parity; no matched
  number is emitted. The harness (`bench/wiki_h2h.py`) now hard-gates the headline ratio on
  graph-leg reconciliation, timer-boundary parity, matched (not thresholded) recall, and
  `examined>0`. ADR-0017 prior (value = architectural one-WAL consistency, expected-fail on raw
  speed in this regime) stands, unrefuted and untested at wiki scale.

### Caveats / blockers remaining (#2)
- **Engine vector fast path**: fix the `float8[] <->` opclass binding (or wire a PERF-08
  CAGRA→hnswlib ingest) so the vector leg hits `articles_hnsw` — prerequisite for ANY TriDB
  point at ANY N. The 49 s CAGRA build cannot serve the AM.
- **Graph-leg mismatch**: reconcile engine 21.9M vs induced 38.99M (dedup / `ORDER BY src`
  drop / identity-mode / unfinished insert — echoes DEV-1352) before any graph-inclusive h2h.
- **Scale asymmetry**: match at **N=200k** for a graph-inclusive comparison; 1M is vector-leg
  only and currently has no TriDB point.
- **dim-384 not dim-768**: RAM-resident on the 128 GB Spark → **wrong regime** for the I/O-bound
  thesis (needs dim-768 float8 / chunk-scale > 128 GB = Milestone B).
- **Spark**: origin unreachable from Spark; commits land on origin from the local box.

---

## Milestone B decision memo — how to actually test the I/O-bound thesis

Milestone A confirmed the dim-384 regime is RAM-resident (~1.5 GB at 1M, ~10.6 GB at 6.9M) and
therefore **cannot** exercise the spec's I/O-bound early-termination thesis (SM-3 "3 pages vs
85", native page-locality). To force the working set past the Spark's 128 GB — the only regime
where TriDB's architecture is *supposed* to turn decisive — the maintainer must pick an
embedding strategy. **But note the prerequisite that dominates both options:** the engine vector
fast path is broken (Walls 1+2 above) and the graph leg is single-threaded super-linear. Neither
B option is worth funding until a TriDB `tjs_open` point is producible at even N=200k on the
current dim-384 assets. Fix that first; then choose:

| Option | Working set (6.9M) | RAM pressure (128 GB) | Re-embed cost | Forces the thesis? |
|---|---|---|---|---|
| **A — re-embed at dim-768 float8** | ~42 GB raw (~60–80 GB with HNSW) | **stresses but likely fits** — borderline, not decisively cold | ~1.5–2 h GPU re-embed of 6.9M | **Partially.** 768-d doubles per-vector I/O and pushes HNSW toward the RAM ceiling, but at 6.9M it may still be RAM-resident → a weak/ambiguous test of the cold-read win. |
| **B — chunk-level embeddings** | ~25M chunks → **100–250 GB** | **decisively exceeds RAM** — forces cold reads + compression | larger re-embed (chunk, not article) + a chunk→article id map | **Yes.** This is the only option that unambiguously puts the working set past 128 GB, forces **RaBitQ**-style compression, and actually exercises the I/O-bound page-locality claim. |

**Recommendation: Option B (chunk-level), but gated.** Only chunk-level decisively exceeds RAM
and tests the real thesis; dim-768 at article granularity risks landing back in the ambiguous
"borderline RAM-resident" regime and re-litigating Milestone A at 2× cost. **However**, do NOT
start B until three Milestone-A blockers are cleared, because B walls identically on all of them
and at larger scale: (1) the `tjs_open` vector-leg HNSW binding, (2) the 21.9M↔38.99M graph-leg
reconciliation, (3) a non-single-threaded vector build (PERF-08 CAGRA→hnswlib ingest or PERF-04
parallel `addPoint`) — a single-threaded HNSW build over ~25M chunks is many hours to days.
Sequencing: **fix the fast path + graph reconciliation at N=200k → prove a real matched point
in the compute regime → then re-embed chunk-level for the decisive I/O-bound B run.** If those
fixes prove infeasible on the shipped fork AM, the honest close is ADR-0017: publish the
architectural/consistency case, not a speed number.

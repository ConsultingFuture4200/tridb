# DEV-1354 wiki-scale — actions #1 & #2 combined summary

TL;DR (2026-07-07). Two follow-ups on the real 6.9M-article enwiki corpus:

- **#1 fused-vs-cosine link recovery** — topology adds a **small but statistically
  significant** signal. RRF-fused cosine+Adamic-Adar recovers **overlap@10 = 0.1261**
  vs the **0.1101 cosine-only lower bound (+14.6%)**, after killing a reconstruction
  leak and ruling out a popularity confound. **Predictive-signal test only, no latency.**
- **#2 tjs_open head-to-head** — **NO matched run happened, at any scale.** The corpus was
  *verified* near-full; the engine load was *attempted* at a 500k slice and did not
  complete; no fixed-accuracy latency table was produced. Verdict is **inconclusive /
  blocked**, not a win and not a loss.

Docs: [`docs/benchmark_wiki_linkpred_fused_v0.1.0.md`](../../docs/benchmark_wiki_linkpred_fused_v0.1.0.md)
· [`docs/benchmark_wiki_scale_h2h_v0.1.0.md`](../../docs/benchmark_wiki_scale_h2h_v0.1.0.md)

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

## #2 — Fused `tjs_open` vs tuned multi-store, at wiki scale

**No head-to-head was executed.** What actually happened this session:

| Item | State |
|---|---|
| Corpus scale | **VERIFIED near-full: 6,900,039 articles / 224,475,283 edges** (real, dim-384 f32, 10.6 GB) — of ~7.19M nominal, ~4% / 3 shards lost |
| Engine load (6.9M) | **NOT loaded** — blocked (single-threaded HNSW build = spec's "tens of hours"; no PERF-04/PERF-08 wired into load.sql) |
| Engine load (500k slice) | **Attempted, not completed** — 20,469,892 induced edges; host prep measured (106.3s articles + 11.3s edges); in-engine HNSW still building >29 min at session end, no completion record |
| Baseline stack | Healthy (Milvus 2.4.5 + Neo4j 5.20 + PG 16) but holds the **1M synthetic SM-2 corpus, not wiki**; no wiki-scale baseline loader exists |
| Matched query harness | Does not exist for wiki (existing drivers run the synthetic SM-2 corpus) |
| Latency @ fixed accuracy | **n/a — nothing measured** |
| Pages-touched (SM-3) | **n/a — gated on a completed load** |

- **Accuracy fixed at**: n/a (blocked before any numbers).
- **Verdict**: **inconclusive / blocked.** This exercise established only near-full
  verification + an incomplete 500k load attempt. Neither speed nor one-WAL consistency
  was tested at 6.9M. The carried ADR-0017 prior (value is **architectural** — one-WAL
  consistency + source-anchored fused `tjs_open`, not raw speed) is **unrefuted but
  UNRETESTED at wiki scale here**. No latency-win table was fabricated.
- Commit `2233cd5` (report `56c3639`).

### Caveats / blockers remaining (#2)
- **Vector leg**: 6.9M×384 single-threaded `CREATE INDEX ... USING hnsw` is multi-hour;
  needs PERF-04 (parallel build) or PERF-08 (GPU-CAGRA→hnswlib export) wired into
  `load.sql`, plus a dense id-aligned emb (`vectors.f32` is sparse).
- **Baseline leg**: 224M edges into Neo4j Community needs offline `neo4j-admin` bulk
  import (online Cypher = many hours); no wiki-scale Milvus/pgvector loader.
- **Harness**: no recall-tuned-to-equality `tjs_open`-vs-multi-store wiki query harness.
- **dim-384 not dim-768**: RAM-resident on the 128 GB Spark → **wrong regime** to test the
  spec's I/O-bound speed thesis (that needs dim-768 float8 / chunk-scale > 128 GB).
- **Spark**: origin unreachable from Spark; docs are scp'd/bundled there, commits land on
  origin from the local box.

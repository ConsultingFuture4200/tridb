# TriDB vs a tuned multi-store — wiki-scale head-to-head, Milestone A: EXECUTED RESULT (v0.4.0)

> **Status: PART EXECUTED, PART BLOCKED.**
> - **Point A (vector leg @ N=1,000,000): EXECUTED.** First matched latency@equal-recall point
>   at wiki scale. TriDB's warm plain HNSW scan **wins** the vector leg in this regime —
>   ~2.1x lower p50 than Milvus at equal recall@10 — but only after paying a **~823 s cold
>   index-load** that Milvus does not have.
> - **Point B (fused `tjs_open` @ 200k): BLOCKED.** `matched_200k_baseline = false` (no
>   200k-loaded engine exists). Engine-side-only at 1M walls: the fused operator's vector leg
>   does not bind the HNSW opclass and seqscans (>25 s/query). No equal-recall fused number.
>
> Supersedes `benchmark_wiki_scale_h2h_v0.2.0.md` (which reported the pre-execution blocker).
> v0.4.0 carries the executed Point-A numbers. Per ADR-0017 the standing prior is unchanged:
> TriDB's value is one-WAL consistency + source-anchored fused retrieval, **not** raw speed.
> The Point-A win is a **compute-bound-regime** result and does **not** vindicate the spec's
> I/O-bound thesis (see Regime below).

## TL;DR

| | Point A — vector leg @ 1M | Point B — fused `tjs_open` @ 200k |
|---|---|---|
| Ran? | **Yes (executed)** | **No (blocked)** |
| Matched to baseline at same N? | Yes (Milvus @ 1M, same query set) | No — `matched_200k_baseline=false` |
| Equal recall@10 | ~0.92 (TriDB 0.9194 vs Milvus/ef96 0.9281) | — |
| TriDB p50 / p95 (warm) | **1.182 ms / 1.836 ms** | — (seqscan wall, >25 s/query) |
| Baseline p50 / p95 | Milvus 2.508 ms / 2.927 ms | — |
| TriDB pages / examined | ~86 index-scan buffers / ~87 candidates | — |
| Winner (equal recall) | **TriDB ~2.1x faster** | no result |
| Cold-start asymmetry | **TriDB ~823 s LoadIndex; Milvus none** | — |

- **Held-out query set:** 320 queries (160 exact members, 80 in-manifold midpoints, 80
  out-of-slice non-members) — NOT a single trivial exact-member probe. Oracle = brute-force
  exact L2 top-10 over the raw 1,000,000 dense vectors (dim-384, RAM-resident matmul).
- **Regime:** dim-384 float32 over 1M ≈ 1.5 GB, fully RAM-resident on the 128 GB Spark =
  **compute-bound**. This is NOT the spec's I/O-bound early-termination thesis (SM-3
  "3 pages vs 85", native page-locality > 128 GB working set = Milestone B). Every latency
  figure here tests the compute regime and is labelled as such.
- **Engine image:** `tridb/msvbase:gx10-v1-hnswcap`, container `wiki-eng-1m`, port 5446.
  Baseline: isolated `tridb-wiki` Milvus :19531 / Neo4j :7688 / pgvector :5434.

---

## Point A — vector leg, N = 1,000,000 (EXECUTED)

**What runs.** TriDB warm plain relaxed-order HNSW scan
(`SELECT id FROM articles ORDER BY embedding <-> 'v' LIMIT 10`, `enable_seqscan=off`) vs Milvus
ANN (`wiki_articles`, HNSW/COSINE) over the **same** 320 held-out query vectors. Both timed
client-side over TCP (psycopg / pymilvus) so the timer boundary is identical. Recall@10 of each
side = overlap with the exact brute-force L2 oracle. Latency compared **only at equal recall**.

### Recall/latency curve (Milvus swept on `ef`; TriDB is a single operating point)

| System | knob | recall@10 | p50 (ms) | p95 (ms) |
|---|---|---|---|---|
| **TriDB** | HNSW scan (self-terminates ~87 examined) | **0.9194** | **1.182** | **1.836** |
| Milvus | ef=16 | 0.7778 | 1.987 | 2.538 |
| Milvus | ef=32 | 0.8547 | 2.120 | 2.601 |
| Milvus | ef=48 | 0.8875 | 2.183 | 2.620 |
| Milvus | ef=64 | 0.9084 | 2.318 | 2.677 |
| Milvus | **ef=96 (matched)** | **0.9281** | **2.508** | 2.927 |
| Milvus | ef=128 | 0.9444 | 2.644 | 3.127 |
| Milvus | ef=256 | 0.9669 | 3.048 | 3.742 |

### Matched-recall comparison (the honest number)

At the operating point where recalls match within eps=0.02 (TriDB 0.9194 vs Milvus ef=96
0.9281):

| Metric | TriDB | Milvus | TriDB advantage |
|---|---|---|---|
| recall@10 | 0.9194 | 0.9281 | — (matched) |
| p50 latency | **1.182 ms** | 2.508 ms | **2.12x faster** |
| p95 latency | **1.836 ms** | 2.927 ms | 1.59x faster |

The win is robust across the curve: even at Milvus ef=64 (recall 0.9084, *below* TriDB's
0.9194) Milvus is 2.318 ms > TriDB's 1.182 ms. TriDB is faster than Milvus at every recall
point up to ~0.93 in this regime.

### TriDB pages-touched / examined

From `EXPLAIN (ANALYZE, BUFFERS)` on the HNSW leg: `Index Scan using articles_hnsw`,
**actual rows = 87**, **Buffers: shared hit = 86**. The scan self-terminates at ~87 candidates
— the `vectordb.hnsw_max_examined` cap (default 1000) is **inert** here (flip terminates far
below it); it is **not** credited for any latency. (A 40-query buffers probe reported a summed
median of 267, which triple-counts nested plan nodes; the correct per-scan figure is the
Index-Scan node's ~86 buffers. One out-of-manifold query fell back to a full ~300k-page
seqscan — a rare HNSW-entry-guard fallback, noted honestly.)

### Cold-start asymmetry (MUST be stated)

TriDB's **first** query after a cold container is an `HNSWIndexScan::LoadIndex` that rebuilds
the 1M index from the heap (DEV-1235). Measured on this run by restarting `wiki-eng-1m` and
timing the first ANN query:

| | value |
|---|---|
| TriDB cold first-query (LoadIndex) | **822,963 ms ≈ 823 s ≈ 13.7 min** |
| TriDB second (warm) query | 1.43 ms |
| Milvus equivalent | **none** (collection already loaded; `col.load()` is sub-second) |

All Point-A latencies above are **warm steady-state**. The ~823 s one-time cold load is a real
TriDB liability with **no Milvus/Neo4j equivalent**; a fair systems comparison must carry it.

**Point A verdict.** In the compute-bound, RAM-resident dim-384 regime, TriDB's in-process
HNSW scan beats a tuned external Milvus ANN at equal recall (~2.1x p50) — one process, one
round-trip, no serialization across a gRPC boundary. This is a genuine, reproducible win **in
this regime**, offset by a ~13.7-min cold index load Milvus does not pay, and it does not speak
to the spec's I/O-bound thesis.

---

## Point B — fused `tjs_open`, N = 200,000 (BLOCKED)

`matched_200k_baseline = false`. No 200k-loaded engine exists — the running engine
`wiki-eng-1m` holds N=1,000,000, so there is no engine slice to match the id<200k baseline
against. Falling back to **engine-side-only @ 1M**, the fused operator walls:

- `tjs_open('articles', 10, term, m_seeds, hops, ...)` with the `embedding <-> 'v'` vector leg
  **does not bind the HNSW opclass** at N=1M on this image. The `float8[] <->` distance
  seqscans 1,000,000 × 384 — measured **>25 s per query** with no natural termination
  (backend cancelled). Same class of defect as Blocker 3 in v0.1.0: the fused vector leg
  seqscans while the *plain* scan (Point A) binds the index. `examined` could not be read (the
  backend never returned).

No equal-recall fused latency point could be produced. Bridges-injected / pages-touched for the
fused operator are therefore **not reported** — reporting them would fabricate a comparison.

**Point B verdict.** BLOCKED. A matched fused number needs either (a) fixing the `tjs_open`
vector-leg opclass binding so it uses `articles_hnsw` at 1M, or (b) a dedicated 200k-loaded
engine so even a seqscan vector leg is cost-bounded. Until then the fused head-to-head remains
unexecuted, exactly as v0.2.0 stated.

---

## Honesty ledger (reviewer checklist)

- **Warm vs cold:** warm steady-state reported; the ~823 s cold LoadIndex measured and
  disclosed alongside; Milvus has no equivalent — stated.
- **Recall:** recall@10 over 320 held-out queries (members + midpoints + non-members) vs a
  brute-force numpy L2 oracle over the 1M dense vectors — not one exact-member probe.
- **Equal recall:** latency compared only at matched recall (TriDB 0.9194 vs Milvus ef=96
  0.9281, eps=0.02).
- **Regime:** dim-384 float32 RAM-resident = compute-bound; explicitly **not** the I/O-bound
  spec thesis. No thesis claim made.
- **`hnsw_max_examined` cap:** inert (terminates ~87 << 1000); **not** credited for latency.
- **No fabricated win:** Point A win is real and reproducible in this regime; Point B is
  reported as blocked, not papered over. ADR-0017 prior (value = one-WAL consistency, not raw
  speed) stands.

## Reproduce

```
# on the Spark, engine warm (pay the ~823 s cold load once):
python bench/wiki_h2h_queryset.py --emb data/wiki/enwiki/emb/dense_id_aligned.npy \
    --n 1000000 --k 10 --members 160 --midpoints 80 --nonmembers 80 \
    --out bench/results/wiki_h2h_queryset.json
python bench/wiki_h2h_vecrun.py --queryset bench/results/wiki_h2h_queryset.json \
    --engine-port 5446 --milvus-port 19531 --milvus-collection wiki_articles \
    --milvus-metric COSINE --runs 5 --efs 16,32,48,64,96,128,256 \
    --out bench/results/wiki_h2h_vecleg_1m.json
```

Raw results: `bench/results/wiki_h2h_vecleg_1m.json`,
`bench/results/wiki_h2h_vecleg_1m_pages.json`,
`bench/results/wiki_h2h_pointB_engineside.json`,
`bench/results/wiki_h2h_cold_start.log`.

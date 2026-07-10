# TriDB fused `tjs_open` vs a tuned multi-store — wiki-scale head-to-head, Milestone A: HONEST BLOCKER REPORT (v0.2.0)

> **Status: NO MATCHED HEAD-TO-HEAD EXISTS — at any N.** Milestone A stood the baseline up at
> N=1,000,000 on all three stores and made the engine's vector leg durable at N=1,000,000, but
> **could not produce a single TriDB `tjs_open` latency@recall point at any scale**, so there is
> nothing to compare against. This is a blocker report naming exactly where the intended
> cuVS-CAGRA → fork-AM fast path failed and where the graph-edge leg walled. It does **not**
> emit a latency ratio. Per ADR-0017 the standing prior holds: TriDB's value is one-WAL
> consistency + source-anchored fused retrieval, **expected-fail on raw speed** in this regime.
>
> Supersedes `benchmark_wiki_scale_h2h_v0.1.0.md` (which reported the pre-load feasibility
> blocker). v0.2.0 is the executed-spike outcome: the load ran, the baseline reconciled, and the
> engine fast path walled at a precisely identified point.

## TL;DR

- **Milestone A outcome: BLOCKED-AT-SPIKE (partial).** The comparison did not run because the
  engine has **no producible `tjs_open` fixed-recall point** — the fused operator's vector leg
  seqscans and cancels at the statement timeout (`examined=0`) at N=1M, and the engine's graph
  leg holds only ~56% of the oracle/baseline edges. The only numbers the harness can currently
  emit are the **baseline alone**; a "1M head-to-head" from baseline-only numbers would be
  fabricating a comparison.
- **Baseline (all three legs, N=1,000,000, exact reconciliation):** Milvus `wiki_articles`
  1,000,000 (dim-384, HNSW/COSINE); Neo4j `Article` 1,000,000 nodes / `RELATED` **38,991,320**
  rels; pgvector `wiki_article` 1,000,000 rows. End-to-end **~90 ms** (Milvus ~3 ms + Neo4j
  ~83 ms + pgvector ~1.5 ms), **recall@10 = 0.80** vs the exact induced-subgraph oracle. This is
  the baseline **executing at parity scale** — it is NOT a TriDB result.
- **Engine (spike):** vector leg **queryable + durable at N=1,000,000** (HNSW built in ~751 s
  single-threaded). Fully-verified **end-to-end tri-modal** load (both legs + `tjs_open` +
  count reconciliation) proven only at **N=200,000 / 8,208,179 edges**. The 1M graph leg
  (38,991,320 induced edges) was **still inserting at >117 min** when reported and never
  finished in-window → no 1M `tjs_open`/reconciliation point.
- **Wrong regime (unchanged from v0.1.0, must not regress).** dim-384 float32 (~1.5 GB at 1M) is
  **RAM-resident** on the 128 GB Spark = the **compute-bound** regime. The spec's I/O-bound
  early-termination thesis (SM-3 "3 pages vs 85", native page-locality) needs dim-768 `float8[]`
  and/or chunk-level working sets > 128 GB = **Milestone B**. **Every Milestone-A latency figure
  tests the wrong thesis and is labelled as such.**

---

## Where the intended fast path failed (precise)

### Wall 1 — cuVS-CAGRA → fork-AM import is IMPOSSIBLE on the shipped engine

The Phase-1 GPU cuVS CAGRA build (49 s over 6.9M) **cannot serve the engine vector leg.** The
fork `vectordb` access method exposes **no external-index ingest entrypoint**:

- `grep -E 'cagra|import|from_file|load_index|bulk|ambuild' vectordb--0.1.0.sql` → **empty.**
- The AM surface is `hnsw_handler` / `pase_hnsw_handler` / `tjs*` / `topk` / `multicol_topk` /
  `inference` / `model_handler` / distance ops / `tridb_vec_probe` — **no** way to feed a
  prebuilt cuVS/CAGRA graph to the AM.
- The **only** build path is single-threaded `CREATE INDEX … USING hnsw` (CPU hnswlib inside
  the `gx10-v1` image). Measured: 200k = 81.2 s (~2,470 vec/s); 1M = ~751 s (~12.5 min,
  **super-linear**). Extrapolated to 6.9M this is multiple hours.
- PERF-04 (parallel `addPoint`) advertises no parallel lever in the PASE HNSW `ambuild`.

**Consequence:** the 49 s CAGRA number is NOT the engine's vector-build cost and must never be
cited as such. Serving the AM needs a real PERF-08 CAGRA→hnswlib export ingest entrypoint (not
wired) or an honest single-threaded 6.9M budget.

### Wall 2 — the `tjs_open` vector leg does not bind the HNSW index at N=1M

Even with `articles_hnsw` present and durable, the fused operator's `float8[] <->` distance
leg **does not use the index**: it seqscans 1M×384 and the statement cancels at the timeout
with `examined=0`. Bare ANN + `tjs_open(m4h1)` cancels at 120 s; minimal `tjs_open(m1h1)`
cancels at the 300 s statement timeout. `SET enable_seqscan=off` does **not** force it (the
opclass binding is the actual gap). **There is therefore no `tjs_open` latency/recall point at
N=1M — `ready_to_run=false`.** The harness now hard-gates on `examined > 0` so a silent
seqscan/timeout can never be published as a number.

### Wall 3 — the graph edge leg is the real scale wall, and the legs mismatch

Per-edge `gph_insert_edge` (single-threaded, `ORDER BY src`) is super-linear:

| Slice | Induced edges | Edge-insert time | State |
|---|---:|---:|---|
| 200k | 8,208,179 | 689.6 s (~11.9k edge/s) | **COMPLETE + reconciled + `tjs_open` verified** (examined=128 ≪ 200k, bridges=190) |
| 1M | 38,991,320 (target) | >117 min, 99.8% CPU | **UNFINISHED in-window** |
| 6.9M | 224,475,283 (full) | projects to many hours | infeasible in a bounded window |

At the 1M slice the engine reported `gph_edge_count = 21,945,976` while Neo4j / the oracle hold
**38,991,320** induced edges — the engine is **missing ~44% of the topology**. So `tjs_open`
would traverse a graph barely over half the baseline's, and its recall would be graded against
edges it does not contain. **This mismatch alone invalidates any graph-inclusive comparison at
1M** and echoes the prior DEV-1352 identity-mode / edge-dedup defect. Root-cause (dedup vs the
`ORDER BY src` insert path dropping rows vs identity-mode filtering vs the unfinished insert)
is unresolved.

---

## What was verified (real, this spike)

| Leg | Baseline (isolated `tridb-wiki`) | Engine (`tridb/msvbase:gx10-v1`) |
|---|---|---|
| Vector | Milvus 1,000,000 @ dim-384 HNSW/COSINE | HNSW durable @ **1,000,000** (751 s build) |
| Graph | Neo4j 1,000,000 nodes / **38,991,320** rels (offline `neo4j-admin` import) | **21,945,976** edges @ 1M (mismatch); **8,208,179** fully verified @ **200k** |
| Relational | pgvector 1,000,000 rows (`vector(384)`) | articles table @ 1M |
| End-to-end | ~90 ms e2e, recall@10=0.80 vs oracle | `tjs_open` verified @ **200k only**; **walls @ 1M** |

**Achieved scale is asymmetric — state it per-leg, per-side:**
- Baseline: **1M on all three legs.**
- Engine verified tri-modal (both legs + `tjs_open` + reconciliation): **200,000.**
- Engine vector-leg durable: **1,000,000** (no `tjs_open` point).
- Full 6.9M: **not run.**

The largest defensible **graph-inclusive matched point is N=200,000.** Even a **vector-only**
comparison at 1M has no TriDB point (Wall 2). A "1M h2h" framing implies a parity that does not
exist.

---

## Harness honesty hardening applied this session (`bench/wiki_h2h.py`)

The matched harness now **refuses to emit a headline latency ratio** until the blockers clear
(reviewer blocker + majors encoded as a hard `publication_gate`, not a caveat footnote):

1. **Graph-set reconciliation** — no ratio unless engine edge count == oracle/Neo4j edge count
   on the same slice (`WH_ENGINE_EDGES` == `WH_NEO4J_EDGES`).
2. **Timer-boundary parity** — TriDB `\timing` (server-side, local socket) vs baseline
   client-side wall-clock over 3 TCP round-trips is apples-to-oranges; the ratio is withheld
   until both are measured at the same boundary and `WH_BOUNDARY_PARITY=1` acknowledges it.
3. **Matched (not thresholded) recall** — the ratio is withheld when the two operating recalls
   differ by more than `WH_RECALL_EPS` (default 0.02).
4. **`examined > 0` gate** — a TriDB point that seqscanned/timed out (examined=0) is rejected.
5. **`candidates examined` (SM-3) demoted** — kept only as an engine-internal diagnostic in the
   TriDB curve, explicitly non-comparable to the baseline and out of the headline.
6. **Degenerate query filtering** — sampled query ids are restricted to non-zero-norm rows so
   sparse-corpus gap ids (289,612 phantom rows) cannot pollute the recall average.

---

## Verdict (honest)

Milestone A **executed the load and stood up the baseline at parity scale, but produced no
matched head-to-head** because the engine has no `tjs_open` fixed-recall point at any N and the
1M graph legs do not reconcile. The carried **ADR-0017 prior stands and is not overturned**:
TriDB's value is architectural (one-WAL consistency + source-anchored fused retrieval), and this
compute-bound dim-384 regime is the wrong test for the I/O-bound speed thesis regardless. The
baseline's ~90 ms / recall-0.80 is reported **only** as the multi-store executing at N=1M — it
is **not** framed as a TriDB context or a win/loss. To get a real Milestone-A point: (1) wire a
PERF-08 CAGRA→hnswlib ingest or fix the `float8[] <->` opclass binding so the vector leg hits
`articles_hnsw`; (2) reconcile the 21.9M-vs-38.99M edge gap; (3) match at N=200k for any
graph-inclusive comparison. Until then the honest headline is **"blocked, wrong regime"** per
ADR-0017 — see the Milestone B decision memo in `bench/results/wiki_scale_1_2_summary.md`.

_Numbers observed on the Spark; no result fabricated. The 1M engine edge-insert was left running
in an isolated container — the SM-2 baseline stack and the wiki reader were untouched._

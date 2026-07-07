# TriDB fused `tjs_open` vs a tuned multi-store — full-Wikipedia head-to-head (v0.1.0)

> **Status: BLOCKED before a fixed-accuracy latency table — by design, not by fabrication.**
> This document reports what a full-scale (6.9M-article / 224M-edge) head-to-head actually
> requires, what was verified and measured, and the two hard blockers that put a completed
> 6.9M matched run outside a single session's budget. It does **not** invent a latency win.
> Per ADR-0017 and `docs/benchmark_h2h_v0.1.0.md`, the honest prior is that this regime is
> **expected-fail on raw speed**: TriDB's value is one-WAL consistency + source-anchored
> fused retrieval, not beating a tuned multi-store on latency. The numbers below do not
> overturn that prior; they explain why the decisive test could not be run with the assets on hand.

## TL;DR

- **Corpus is real and verified at full scale:** 6,900,039 articles / 224,475,283 edges
  (`data/wiki/enwiki`, manifest counts; the extractor lost shards 28/49/71 — trust
  `wiki_manifest_verify`). Article-level embeddings are complete: `emb/vectors.f32` =
  10.6 GB = 6,900,039 rows × **dim-384 float32** (BGE-small-en-v1.5, normalized).
- **WRONG REGIME for the speed thesis.** The spec's I/O-bound win
  (`docs/wiki_scale_benchmark_spec_v0.1.0.md` §22) requires **dim-768 `float8[]`** (≈42 GB raw,
  60-80 GB with HNSW) and/or chunk-level (100-250 GB) to exceed the Spark's 128 GB working set.
  The available **dim-384** embeddings are ~10.6 GB float32 (≈21 GB as the engine's `float8[]`,
  ≈30-42 GB with the HNSW index) — **RAM-resident**. This is the compute-bound-ish regime, not
  the cold-read I/O-bound regime where `tjs_open` early-termination + native page-locality
  ("3 pages vs 85", SM-3) are supposed to turn decisive. Any latency result here would not test
  the actual thesis.
- **Blocker 1 (engine side): single-threaded in-engine HNSW build.** The loader
  (`tools/wiki_engine_load.py`) builds the vector leg with `CREATE INDEX … USING hnsw` —
  the single-threaded path the spec itself calls "tens of hours" at 6.8M (spec §"HNSW build",
  load-design §3). Neither PERF-04 (parallel `addPoint`) nor PERF-08 (GPU-CAGRA→hnswlib export)
  is wired into the loader's `load.sql`. A bounded 500k×384 slice was launched to measure the
  rate; it had not cleared the build phase in ~9.5 min (details below).
- **Blocker 2 (baseline side): 224M-edge Neo4j load + missing matched harness.** The baseline
  stack is up but currently holds the **1M synthetic SM-2 corpus** (Neo4j 1,000,000 nodes /
  48,000 rels; PG `entity` 1,000,000 rows), not wiki. Loading 224M edges into Neo4j Community
  via online Cypher `UNWIND … MERGE` (`tools/wiki_neo4j_load.py`) is many hours; and **no
  wiki-scale Milvus/pgvector loader or matched wiki `tjs_open`-vs-multistore query harness
  exists** — the existing `bench/h2h_report.py` + `baseline/sm2.py` drive the synthetic SM-2
  corpus, not the wiki corpus.
- **Net:** a completed fixed-accuracy 6.9M matched h2h needs (a) a mitigated vector-leg build,
  (b) a wiki-scale baseline load of all three stores, and (c) a new matched query harness with
  real embeddings on both sides + recall tuning. Each of (a) and (b) is a multi-hour-to-
  tens-of-hours job; together with (c) they exceed one session. Verdict + precise blocker below.

---

## What was verified (real, this session)

| Fact | Value | Source |
|---|---|---|
| Articles (full corpus) | 6,900,039 | `data/wiki/enwiki/manifest.json` counts |
| Edges (full corpus) | 224,475,283 | manifest counts (shards 28/49/71 lost; `wiki_manifest_verify`) |
| Embeddings | 6,900,039 × 384 float32, normalized | `emb/vectors.f32` = 10,598,459,904 B; `emb/meta.json` |
| Embedding id-map | sparse: row→id, min 0, max 7,189,650 | `emb/ids.i64.npy` — **not** identity-aligned |
| Baseline stack | Milvus v2.4.5 + Neo4j 5.20 + PG 16, all healthy | `docker ps` on Spark |
| Baseline currently loaded | 1M synthetic SM-2 corpus (Neo4j 1M nodes/48k rels; PG 1M) | live counts — NOT wiki |
| Engine images | `tridb/msvbase:gx10-v1`, `…-v1-pgv` present | `docker images` |

**Embedding id-alignment caveat.** `vectors.f32` is row-ordered with a separate `ids.i64.npy`
(row i → real article id, sparse to 7.19M because of the lost shards). The engine loader's
`--emb` path indexes `emb[aid]` (row == id), so feeding real vectors at full scale first
requires materializing a dense id-aligned `[max_id+1, 384]` array (~11 GB, gap ids zero-filled).
This is a prerequisite the current tooling does not do — noted, not hidden.

---

## Measured: in-engine load at a bounded 500k-article slice (the blocker, quantified)

To convert "tens of hours" from a spec claim into a measured extrapolation, a bounded slice was
loaded into the live `tridb/msvbase:gx10-v1` engine on the Spark. HNSW build cost depends on
N/dim/M — not on whether vectors are semantic — so a **synthetic dim-384** slice measures the
real single-threaded build rate while also exercising the **real induced-edge** insert path.

Slice: 500,000 articles → **20,469,892 induced edges** (dense low-id region, ~41 out-deg),
dim-384, `maintenance_work_mem=16GB`, into live `tridb/msvbase:gx10-v1`.

| Stage | 500k slice | Status |
|---|---:|---|
| prepare (host, articles) | **106.3 s** | measured |
| prepare (host, edges) → 20,469,892 induced | **11.3 s** | measured |
| in-engine load (graph_store build + COPY + HNSW + 20.5M edges) | launched, **still building at session end** | detached job `/tmp/WLENG.*` on Spark |

**Honest status of the in-engine measurement.** The bounded 500k load was launched as a
detached Spark job. After ~9.5 min of wall-clock it had **not yet emitted `COPY_ARTICLES_DONE`
or any HNSW timing marker** — the run was still inside the graph_store build + COPY + single-
threaded HNSW phase. It was left running (nohup; artifacts persist under `/tmp/wleng_500k` and
`/tmp/WLENG.log`) for a follow-up to harvest exact per-stage seconds. The session budget expired
before a captured HNSW-build number, so **no HNSW-build latency is asserted here** — the "tens
of hours at 6.8M" figure remains the spec's own estimate (`docs/wiki_scale_benchmark_spec…`
§"HNSW build" + `wiki_scale_load_design…` §3, "single-threaded `addPoint` on 6.8M×dim-768 is
tens of hours; the recall-decay bench stalled on it"). That a mere 500k×384 slice did not clear
the build phase in ~9.5 min is directionally consistent with — not a substitute for — that
estimate. The one thing measured and confirmed: the loader's `load.sql` uses the plain
single-threaded `CREATE INDEX … USING hnsw`, with **no** PERF-04 (parallel) or PERF-08
(GPU-CAGRA→hnswlib export) path wired in.

**To harvest the completed slice numbers (follow-up):**
```bash
ssh spark 'cat /tmp/WLENG.done; grep -E "#WL|Time: [0-9]" /tmp/WLENG.log'
```

---

## Why the baseline side is also not same-session

- **Neo4j 224M edges.** `tools/wiki_neo4j_load.py` loads online via batched `UNWIND … MERGE`
  (relationship MERGE against 6.9M nodes). The prior simplewiki full load handled ~3.9M edges;
  224M online-Cypher edges is many hours on Neo4j Community and risks store bloat. `neo4j-admin
  database import` (offline bulk) is the right path but is not wired up here.
- **Milvus 6.9M vectors** (dim-384) + HNSW index is tractable (~tens of minutes) — but **no
  wiki-scale Milvus loader exists** (`baseline/harness.py`/`sm2.py` load the synthetic seed CSV).
- **pgvector/PG metadata** at 6.9M is a minutes-scale COPY — but again no wiki loader is wired.
- **No matched query harness.** A fixed-accuracy comparison needs the SAME wiki query set
  (HotpotQA linked via `tools/wiki_hotpot_link.py`) run against BOTH the engine's `tjs_open`
  and the multi-store pipeline, each tuned to equal recall. `bench/h2h_report.py` drives the
  synthetic SM-2 corpus, not wiki — this harness does not exist yet.

---

## Verdict (honest)

**Scale achieved:** full-corpus **verification + a measured 500k-article engine-load slice**;
the fixed-accuracy 6.9M matched head-to-head was **not run** (blocked, see below). No latency@fixed-accuracy
table is emitted because none was honestly producible with the assets on hand.

**Verdict:** consistent with ADR-0017 and `docs/benchmark_h2h_v0.1.0.md` — **value is
architectural (one-WAL consistency + source-anchored fused `tjs_open`), not a raw-speed win.**
Two independent facts reinforce this here: (1) the only available embeddings are **dim-384
float32 (~10.6 GB, RAM-resident)**, so this is *not* the I/O-bound regime the speed thesis needs
(that regime requires dim-768 `float8[]` and/or chunk-level, which exceed 128 GB); and (2) the
canonical single-`src` `tjs()`/`tjs_open` is a source-anchored operator, already shown on the
dev slice to trade recall for latency vs a 5-seed multi-store (0.223 vs 0.953 recall@10 at
1.80 ms vs 6.74 ms) — a bare latency number at unequal recall is not a win.

**Blocker (precise):** a completed 6.9M fixed-accuracy h2h is gated on THREE unbuilt/multi-hour
pieces, any one of which exceeds a single session: **(1)** engine vector leg — the loader's
single-threaded `CREATE INDEX … USING hnsw` at 6.9M×384 is the spec's "tens of hours" (a bounded
500k slice did not clear the build phase in ~9.5 min; left running detached); PERF-04 (parallel
build) or PERF-08 (GPU-CAGRA→hnswlib export, from the
already-built CAGRA index) must be wired into `load.sql` first, plus a dense id-aligned emb.
**(2)** baseline graph leg — 224M edges into Neo4j needs offline `neo4j-admin` bulk import
(online Cypher is many hours); no wiki-scale Milvus/pgvector loader exists. **(3)** a matched
wiki query harness (`tjs_open` vs multi-store on HotpotQA-linked queries, recall-tuned to
equality) does not exist. Recommended next step: wire PERF-08 CAGRA-export into the loader
(kills blocker 1) and build the wiki-scale baseline loaders + matched harness — then the
fixed-accuracy table becomes a bounded overnight run.

_Numbers observed on the Spark (`tridb/msvbase:gx10-v1`); no result fabricated._

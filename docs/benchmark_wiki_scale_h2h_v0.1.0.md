# TriDB fused `tjs_open` vs a tuned multi-store — full-Wikipedia head-to-head: feasibility + blocker analysis, no matched run (v0.1.0)

> **Status: NO HEAD-TO-HEAD WAS RUN — at any scale.** This is a feasibility/blocker report,
> not a result. No matched `tjs_open`-vs-multi-store query executed at 6.9M, nor at the 500k
> slice (that slice was an engine *load* that had not completed), nor with real embeddings on
> either side. What follows is what a full-scale (near-full 6.9M-article / 224M-edge) head-to-head
> actually requires, what was verified and measured this session, and the hard blockers that put
> a completed matched run outside a single session's budget. It does **not** invent a latency win.
> (Filename kept as `benchmark_wiki_scale_h2h_*` for reference stability; read the title, not the
> stem — no h2h ran.)
> Per ADR-0017 and `docs/benchmark_h2h_v0.1.0.md`, the honest prior is that this regime is
> **expected-fail on raw speed**: TriDB's value is one-WAL consistency + source-anchored
> fused retrieval, not beating a tuned multi-store on latency. The numbers below do not
> overturn that prior; they explain why the decisive test could not be run with the assets on hand.

## TL;DR

- **Corpus is real and verified at NEAR-full scale:** 6,900,039 articles / 224,475,283 edges
  present — **of ~7.19M nominal** (id-map max is 7,189,650; the extractor lost shards 28/49/71,
  a ~290k / **~4% gap that is exactly those lost shards**). So 6.9M is *near-full (3 shards
  dropped)*, NOT the intact enwiki; edges and embeddings are likewise **net-of-loss**. Counts
  from `data/wiki/enwiki` manifest — trust `wiki_manifest_verify`. Article-level embeddings
  cover the present rows: `emb/vectors.f32` = 10.6 GB = 6,900,039 rows × **dim-384 float32**
  (BGE-small-en-v1.5, normalized). **This "near-full 6.9M-of-~7.19M (~4% lost)" caveat must
  travel with the count — never quote "full corpus verified" bare.**
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
  rate; it ran >29 min without completing the build phase and emitted no harvestable timing
(details below). Note the spec's "tens of hours" is a **dim-768** estimate; the dim-384 slice
here shows only "not fast," not "tens of hours" — see the regime caveat below.
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
| Articles (near-full: present rows) | 6,900,039 of ~7.19M nominal (~4% lost) | `data/wiki/enwiki/manifest.json` counts; id-map max 7,189,650 |
| Edges (near-full: net-of-loss) | 224,475,283 | manifest counts (shards 28/49/71 lost; `wiki_manifest_verify`) |
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

## Attempted: in-engine load at a bounded 500k-article slice (launched, not completed)

To convert "tens of hours" from a spec claim into a measured extrapolation, a bounded slice was
loaded into the live `tridb/msvbase:gx10-v1` engine on the Spark. HNSW build cost depends on
N/dim/M — not on whether vectors are semantic — so a **synthetic dim-384** slice measures the
real single-threaded build rate while also exercising the **real induced-edge** insert path.

Slice: 500,000 articles → **20,469,892 induced edges** (dense low-id region, ~41 out-deg),
dim-384, `maintenance_work_mem=16GB`, into live `tridb/msvbase:gx10-v1`.

| Stage | 500k slice | Status |
|---|---:|---|
| host-side prep — articles COPY buffer (measured) | **106.3 s** | host-side data-prep, single run, retained: `bench/results/wleng_500k_prep.log` |
| host-side prep — induced edges → 20,469,892 (measured) | **11.3 s** | host-side data-prep, single run, retained: `bench/results/wleng_500k_prep.log` |
| **in-engine** load (graph_store build + COPY + HNSW + 20.5M edges) | **null — never completed** | detached job `/tmp/WLENG.*` on Spark; no engine throughput number was harvested |

The only two measured rows are **host-side data-prep**, not engine performance. The actual
in-engine measurement is a **null** (see honest status below). Do not read the 106.3 s / 11.3 s
as engine build throughput — they are the cost of writing the COPY buffers on the host.

**Honest status of the in-engine measurement.** The bounded 500k load was launched as a
detached Spark job. It ran **>29 min of wall-clock without emitting `COPY_ARTICLES_DONE` or any
HNSW timing marker** — still inside the graph_store build + COPY + single-threaded HNSW phase
(psql `\timing` output is pipe-buffered and only flushes at job exit, so no intermediate
per-stage number was observable). It was left running (nohup; artifacts persist under
`/tmp/wleng_500k` and `/tmp/WLENG.log` on Spark). The host-side prep log is **retained in-repo**
at `bench/results/wleng_500k_prep.log` (the only committed artifact — it holds the 106.3 s /
11.3 s host-prep timings and `prep.json`; the in-engine stage never wrote a completion record to
harvest). So **no HNSW-build latency is asserted here** — the "tens
of hours at 6.8M" figure remains the spec's own estimate (`docs/wiki_scale_benchmark_spec…`
§"HNSW build" + `wiki_scale_load_design…` §3, "single-threaded `addPoint` on 6.8M×dim-768 is
tens of hours; the recall-decay bench stalled on it"). **This 500k slice does NOT corroborate
that dim-768 figure.** It is a different regime on two axes: **dim-384** (≈half the per-vector
build cost of dim-768) at **0.5M** (vs 6.9M). A naive O(N·log N) extrapolation of "not cleared
at 500k in >29 min" lands in the low single-digit hours at 6.9M, not tens of hours — and even
that is unreliable across a dim change. The slice therefore establishes only **"the in-engine
build is not fast"**, NOT "tens of hours." The tens-of-hours number stands on the spec's own
dim-768 estimate alone; treat the slice as non-extrapolable to it. The one thing measured and
confirmed: the loader's `load.sql` uses the plain
single-threaded `CREATE INDEX … USING hnsw`, with **no** PERF-04 (parallel) or PERF-08
(GPU-CAGRA→hnswlib export) path wired in.

**To harvest the completed slice numbers (follow-up).** The psql `\timing` + `#WL` stage markers
only flush to `WLENG.log` when the detached job exits (pipe buffering), so wait for the loader
PID to die, then grep:
```bash
ssh spark 'while kill -0 3316728 2>/dev/null; do sleep 60; done; grep -E "#WL|Time: [0-9]" /tmp/WLENG.log'
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

**What THIS exercise established (at wiki scale):** near-full-corpus **verification** (6.9M-of-
~7.19M, ~4% lost) and an **attempted, not-completed** 500k engine-load slice. It tested
**NEITHER speed NOR consistency** at 6.9M. No matched h2h ran; **no** one-WAL/consistency test
was run this session at all. So the honest wiki-scale verdict is **inconclusive**: the
speed-loss prior was not re-measured here, and the consistency win was not demonstrated here.
No latency@fixed-accuracy table is emitted because none was honestly producible with the assets
on hand.

**Carried prior (NOT re-established here):** ADR-0017 and `docs/benchmark_h2h_v0.1.0.md` argue
TriDB's value is **architectural** (one-WAL consistency + source-anchored fused `tjs_open`),
not a raw-speed win. **That thesis is unrefuted but also UNRETESTED at wiki scale in this
document** — read it as a standing prior, not a finding of this exercise. Two facts observed
this session are *consistent with* (do not prove) it: (1) the only available embeddings are
**dim-384 float32 (~10.6 GB, RAM-resident)**, so this is *not* the I/O-bound regime the speed
thesis needs (that regime requires dim-768 `float8[]` and/or chunk-level, which exceed 128 GB) —
i.e. even a latency run here would test the wrong regime; and (2) a **directional prior from the
synthetic SM-2 dev corpus** (NOT a wiki measurement): the canonical single-`src` `tjs_open`
traded recall for latency vs a 5-seed multi-store — **0.223 vs 0.953 recall@10 at 1.80 vs
6.74 ms**. That gap is driven **partly by seed-count asymmetry** (1 source-anchored seed vs 5),
and `h2h_metrics.json`'s term-cond sweep (0.223→0.227) shows it is not merely the old
early-termination bug. Keep it as a prior on a different corpus — do NOT conflate it with a wiki
result.

**Blocker (precise):** a completed 6.9M fixed-accuracy h2h is gated on THREE unbuilt/multi-hour
pieces, any one of which exceeds a single session: **(1)** engine vector leg — the loader's
single-threaded `CREATE INDEX … USING hnsw` is a multi-hour build at 6.9M (the spec's dim-768
"tens of hours"; at the available dim-384 a naive extrapolation is lower, low-single-digit hours
— but a bounded 500k×384 slice had still not completed the build after ~12 min, left running
detached, no completed number harvested). PERF-04 (parallel build) or PERF-08 (GPU-CAGRA→hnswlib
export, from the already-built CAGRA index) must be wired into `load.sql` first, plus a dense
id-aligned emb.
**(2)** baseline graph leg — 224M edges into Neo4j needs offline `neo4j-admin` bulk import
(online Cypher is many hours); no wiki-scale Milvus/pgvector loader exists. **(3)** a matched
wiki query harness (`tjs_open` vs multi-store on HotpotQA-linked queries, recall-tuned to
equality) does not exist. Recommended next step: wire PERF-08 CAGRA-export into the loader
(kills blocker 1) and build the wiki-scale baseline loaders + matched harness — then the
fixed-accuracy table becomes a bounded overnight run.

_Numbers observed on the Spark (`tridb/msvbase:gx10-v1`); no result fabricated._

# SM-2 at 1M on the GX10, v0.3.0 — the headline, finally measured on the v1 native graph AM (ADR-0013 / plan 025)

> **Date:** 2026-07-03 · **Engine:** `tridb/msvbase:gx10-v1` (the v1-rewired chain, offline-built on the DGX Spark)
> **Status:** MEASURED — the flagship 1M filter-first result now runs on the **v1 native adjacency-list
> access method** the README thesis is actually about, not the v0 heap-backed extension every prior
> headline used (see ADR-0013 / `docs/landscape_review_v0.1.0.md` F1). The moat is now benchmarked as
> the shipped path.
> **Supersedes** the store-provenance caveat on v0.1.0/v0.2.0. **Issues:** ADR-0013 (rewire), plan 025.

## Why this document exists

Every prior SM-2 headline — including the v0.2.0 "18.3× at 1M" filter-first flagship — was produced
with both operators (`tjs`, `tjs_open`) probing the **v0 heap-backed `graph_store` extension** (a
plain heap table + SPI), NOT the **v1 native access method** (`src/graph_store`, 32KB pages,
GenericXLog WAL) that the project sells as its differentiator ("native adjacency-list access method,
*not* relational join tables"). Plan 025 (ADR-0013 Stage A/B) rewired both operators and all
benchmark drivers onto v1 via a `gph_upsert_vertex` id-mapping layer + drop-in compat surface. This
is the re-measurement of the headline on that path.

## TL;DR — v1 native AM, 1M, filter-first

Identical 1M×128 corpus (24 hubs × fanout 2000, ~0.12% joint selectivity, ~1208 qualifying/query),
identical 24 queries and k=5, identical client-side end-to-end methodology (warm connection, median
of 7), **all recall scored against the same EXACT offline oracle** (`sm2_1m_exact_oracle.json`):

| Engine / config | median ms | recall@5 vs exact | SM-2 wins |
|---|---:|---:|---|
| Baseline, correct config (nprobe=4096 exact IVF, fetch 16 380) | 88 | 1.000 | — |
| TriDB filter-first on the **v0 heap store** (v0.2.0) | 4.7 | 1.000 | 24/24 |
| **TriDB filter-first on the v1 native AM (this doc)** | **6.66** (5.94–7.63) | **1.000** | **24/24** |

- **SM-2 = 100%** (24/24) on the native AM, at a **median 13.4× latency advantage** over the baseline
  configured to be correct (88 ms).
- **SM-4 = 100% exact-set parity**: the v1 answers are **byte-identical** to the v0 answers and to the
  exact oracle on all 24 queries (verified by direct diff of the `#SM2 RESULT` lines). The rewire
  preserved semantics exactly — the parity oracle (`test/graph_v0v1_parity_test.sql`) proves this
  structurally; this run proves it at 1M scale end-to-end.
- **The ~2 ms over v0 (4.7 → 6.66 ms) is the id-mapping indirection**, not a scaling problem: the v1
  compat surface (`add_edge`/`neighbors` → `gph_upsert_vertex`/`gph_neighbors_ext`) translates
  external entity ids ↔ dense vids through `gph_vid_map` on each probe. It is a constant per-query
  overhead (~40%), entirely expected, and the correctness/durability wins of the native AM (below)
  are what it buys.

## What the native AM buys (why the 2 ms is worth it)

The v0 heap store is a regular Postgres heap table — it inherits heap VACUUM/wraparound behavior and
is, per golden rule 3, "the path TriDB rejects" (topology as a relational structure). The v1 AM is
the thesis: 32KB pages through the shared buffer manager, GenericXLog WAL (one WAL, FR-7 atomic with
the relational + vector legs — re-proven on this path: `txn_atomicity` C1/C2 and `crash_recovery`
both scenarios PASS with the operators now sharing the v1 traversal). The headline number now
describes the architecture the project is actually selling.

## Honesty box

- Same synthetic uniform-random corpus as v0.1.0/v0.2.0 (hardest case for ANN structure; it punishes
  the baseline's IVF recall, not the filter-first exact drain).
- The vector-first row is unchanged (171 ms / 0.958, v0.1.0) — the DEV-1290 body is store-independent
  above the graph probe, and this run only re-measures filter-first. A full vector-first re-run on v1
  is a cheap follow-up but would move only by the same ~2 ms probe overhead.
- One box, one run, 24 queries × 7 samples; per-query spreads tight (5.94–7.63 ms).
- The id-mapping layer's `gph_upsert_vertex` uses an `ON CONFLICT` upsert; under the single-writer
  contract the v1 core assumes, the race branch (return the winner's vid) is covered but not
  stress-tested concurrently — noted for the incremental-ingest work (DIRECTION-04).

## Repro

```bash
# v1-rewired engine (offline GX10 recipe):
scripts/gx10build.sh --skip-clone --image tridb/msvbase:gx10-v1

# TriDB side, filter-first pinned, v1 native AM (the emitters + bench_sm2.sh now CREATE EXTENSION
# graph_store_am and route edges through gph_upsert_vertex + add_edge):
python tools/bench_sm2_corpus.py --entities 1000000 --dim 128 --hubs 24 --fanout 2000 \
  --queries 24 --k 5 --window 600 --seed 42 --runs 7 --term-cond 10000 \
  --join-order filter_first --sql-out sm2_v1.sql --manifest-out manifest.json
# then the bench_sm2.sh TriDB container block over sm2_v1.sql, mounting src/graph_store.
```

Artifacts: `bench/results/sm2_1m_v1_raw.txt` (this run's transcript), scored against the unchanged
`sm2_1m_exact_oracle.json` + `sm2_1m_baseline_np4096.json`.

## Consequences

1. **The landscape review's F1 ("every headline measured the v0 store") is closed.** The at-scale
   SM-2 claim now rests on the native AM. Publication of external numbers can proceed on v1.
2. **The correctness of the rewire is proven three ways**: the parity oracle (structural), the four
   operator suites + FR-7 on the v1 image (functional), and this 1M run (byte-identical answers at
   scale).
3. Follow-ups: archive v0 (ADR-0013 Stage C) after a release cycle on v1; re-run the public-dataset
   and filtered-SIFT headlines on v1 for full provenance consistency; the ~2 ms indirection is a
   candidate for a cached-vid fast path if it ever matters (it does not at these margins).

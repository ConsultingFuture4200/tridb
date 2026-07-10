# Wikidata-on-TriDB spike — report v0.1.0

**TL;DR.** Plan 060 routes around the plan-043 blocker (non-deterministic seedless/vector-first
HNSW leg) by proving TriDB's differentiator on **Wikidata** via the **filter-first** path (green at
1M, DEV-1290) plus the **one-WAL consistency** story on Wikidata's real edit firehose. This session
delivered the **design + tooling + two measurement harnesses, all host-verified**: ADR-0018, a
tri-modal Wikidata ingest, Harness A (edit-firehose consistency), Harness B (filter-first KBQA
h2h), and 32 host unit tests. **The measured scale curve is not yet taken** — it needs the Wikidata
dump ingested and the live legs run on the GX10/Spark. **Verdict: INCONCLUSIVE — GO-pending-one-
measurement-pass**, because every functional piece is proven and only the gated numbers remain.

---

## Regime & honesty (read first)

- **Compute-bound at 1M** (RAM-resident dim-D floats). The I/O-locality thesis is dead (wiki-scale
  memory); the demonstrated value is (1) **fusion speed** (early-terminating tri-modal
  co-iteration) and (2) **one-WAL cross-modal consistency**. No number here is framed as an
  I/O-locality win.
- **Filter-first only.** Every Harness B headline runs the filter-first `tjs_open` mode (selective
  typed-edge + entity-type constraint, then vector rank). The **seedless / vector-first** mode is
  **blocked on plan 043** and is never quoted.
- **Out-direction only.** Harness B queries traverse subject→object (ADR-0016 ships out-direction;
  backlinks await the reverse index). Backlink-shaped KBQA is out of scope, stated not worked around.
- **Matched recall only.** Latency/pages are reported ONLY at matched recall, via
  `bench.wiki_h2h.publication_gate` reused verbatim (graph-set parity, timer-boundary, HNSW build
  health, matched recall, examined>0).

---

## What was built (host-verified this session)

| Deliverable | File | Verified here |
|---|---|---|
| ADR — dataset/slice/id-map/honesty | `docs/decisions/0018-wikidata-benchmark.md` | `make lint` |
| Tri-modal ingest (dump → shards) | `tools/wikidata_ingest.py` | 14 host tests + CLI dry-run (prefix + BFS) |
| Harness A — edit-firehose consistency | `bench/wikidata_consistency.py` | 8 host tests + host-sim dry-run |
| Harness B — filter-first KBQA h2h | `bench/wikidata_h2h.py` | 11 host tests + `oracle` CLI on a synthetic slice |
| This report | `docs/wikidata_spike_v0.1.0.md` | — |

`make test` = **314 passed**, `make lint` green. The engine/live legs (`tridb-emit`, `baseline`,
Harness A `--live`) are GX10/Spark-gated exactly as `wiki_h2h`/`wiki_consistency` are.

---

## Harness A — cross-modal consistency (edit firehose)

**Host simulation (deterministic, runs anywhere).** Replaying a synthetic edit window (300 edits,
50 entities), a reader that samples the in-flight entity after every store commit:

| Architecture | torn / observations | rate |
|---|---|---|
| TriDB (one WAL, atomic edit) | **0 / 300** | **0.0%** |
| Multi-store (independent commits) | 600 / 900 | 66.7% (structural upper bound) |

> Command: `python -m bench.wikidata_consistency --edits 300 --m 50`
> The multi-store 66.7% is the STRUCTURAL upper bound (reader after every commit), not a timing
> measurement. The **real, lower, timing-dependent rate** comes from the live replay:
> `python -m bench.wikidata_consistency --replay <edit-window.jsonl> --live` (GX10/Spark), reusing
> `wiki_consistency`'s real engine + Milvus/Neo4j/Postgres. **Headline to fill:** torn cross-modal
> reads, TriDB vs multi-store, replaying a pinned Wikidata edit window at M entities.

The tear is inherent to having no cross-system transaction (each store is internally consistent);
mitigable app-side (2PC/saga/outbox) at real cost. TriDB gives cross-modal ACID for free.

---

## Harness B — filter-first KBQA head-to-head

**Oracle functional check (synthetic 51-entity slice, host).** The filter-first oracle ("entities
X links to via P, of type T, ranked by similarity to X") emits a well-formed recall ground truth:

> Command: `WD_SLICE=<slice> python -m bench.wikidata_h2h oracle --queries 3 --hops 1 --k 5`
> → `3 queries, N=51, induced_edges=45, median candidate set=10` (the type filter correctly
> selects the 10 star-type members of each galaxy anchor out of its 15-member P-reach).

**Gate behaviour (host, tested).** `publication_gate` (reused verbatim) refuses a headline until
parity holds: it returns a `graph-set MISMATCH` blocker when engine≠oracle/Neo4j edge counts, a
`recall NOT matched` blocker at unequal recall, and `[]` only when topology matches, boundary parity
is acknowledged, HNSW builds are healthy (≥3/3), and recall is matched. `render_report` emits
"COMPARISON INVALID" (no ratio) while any blocker stands.

> **Headline to fill (GX10):** grade `tridb-emit` (filter-first `tjs_open`) + `baseline`
> (Milvus+Neo4j+pg) against the oracle on the loaded slice; report latency + pages-touched at
> matched recall. Expected shape (from DEV-1290's 1M filter-first point, recall 1.0 / 4.7 ms): a
> selective typed+type constraint yields a small candidate set the fused operator ranks in one
> round-trip vs the multi-store's three.

---

## Sizing curve (to be measured)

No scale was loaded this session, so every scale cell is GX10/dump-gated — **not** fabricated.

| Slice | Where | Load time | Footprint | Harness A torn Δ | Harness B recall / latency @ matched |
|---|---|---|---|---|---|
| 100k (BFS closure) | this box (ingest) / GX10 (live) | gated | gated | host-sim: 0 vs 66.7% (upper bound) | gated |
| 1M (BFS closure) | GX10 | gated | gated | gated | gated (DEV-1290 precedent: recall 1.0 / 4.7 ms) |
| 10M+ | GX10 (128 GB) | gated | gated | gated | gated |

To fill a row: `python -m tools.wikidata_ingest --dump <latest-all.json.gz> --seeds <Q…> --target
<N> --out data/wikidata_slice/`, embed (fastembed/BGE, normalize-at-write), load into the engine +
the isolated multi-store, then run Harness A `--live` and Harness B `oracle`/`tridb-emit`/`baseline`
/`report`.

---

## Verdict — INCONCLUSIVE (GO-pending-one-measurement-pass)

Not a NO-GO: nothing structural is blocked. The differentiator is the **filter-first + one-WAL**
combination, and both are (a) proven at wiki 200k/1M already and (b) exercised end-to-end here by the
Wikidata harnesses at functional scale. Not yet a GO: a public GTM claim needs the measured curve on
a stranger-reproducible Wikidata slice, and that measurement is dump- + GX10-gated.

**The single missing thing:** one measurement pass on the GX10 —

1. Ingest a pinned Wikidata truthy slice (BFS closure, ≥1M) with `tools/wikidata_ingest.py`.
2. Harness A `--live`: the real torn-read delta on a pinned edit window.
3. Harness B `report`: the matched-recall latency ratio through the reused `publication_gate`.

If all three clear their gates → **GO** (commit to a 110M public benchmark + GTM claim). If the gate
blocks (graph-set mismatch, HNSW non-reproducibility bleeding in via the shared vector leg, or
unmatched recall) → the report names the blocker; do not green-wash.

---

## Reproducibility pins (fill on the measured pass)

- Dump: `latest-all.json.gz` — **pin the dump date** in the run manifest.
- Slice: BFS closure seed set + target — recorded in `manifest.json["slice"]`.
- Harness A: **pin the edit-window bounds** (a fixed recorded replay window).
- Embedder: fastembed/BGE, L2-normalize-at-write (ADR-0017 B4-interim).
- Gate env: `WH_ENGINE_EDGES`/`WH_NEO4J_EDGES`, `WH_HNSW_HEALTHY_BUILDS`/`_TOTAL_BUILDS`,
  `WH_BOUNDARY_PARITY=1` only once the timer boundary is genuinely equalized.

# gBrain graph-leg head-to-head: TriDB native AM vs relational `links` (v0.1.0)

**Date:** 2026-07-04 · **Engine:** `tridb/msvbase:gx10-v1-pgv` (fork + pgvector), live on the DGX Spark
· **Harness:** `bench/gbrain_graph_bench.sh` + `bench/graph_surface_isolation` (inline).

## What this measures

gBrain (AgentBOX memory) models its knowledge graph as a **relational `links` table walked by
recursive CTEs**; its own BrainBench calls the graph "the load-bearing wall (+31 P@5)." TriDB's thesis
is a **native adjacency-list access method** instead. This isolates exactly that: the SAME topology in
ONE database, only the STORE differs. The relational side is **tuned** (gBrain's real
`idx_links_from`/`idx_links_to` partial indexes) — a fair baseline, not a strawman. Vector (pgvector)
and BM25 legs are identical on both sides and omitted. Corpus: 20k pages, ~120k edges, 20 hubs ×
fanout 2000 (power-law-ish knowledge graph); warm cache; median of 7–9 (EXPLAIN ANALYZE exec time).

## Results (honest — the naive integration LOSES; the store does not)

| Query (hub #1, degree ~2000) | Relational `links` (tuned) | Native via SQL shim `neighbors()` | Native **raw C** `gph_neighbors` |
|---|---:|---:|---:|
| **1-hop expansion** | 0.25 ms | 1.1–2.5 ms | **0.23 ms** |
| **2-hop expansion** (nested) | **8.3 ms** | 67 ms | (per-call model; see below) |
| 1-hop from a regular node (deg 4) | 0.06 ms | 0.65 ms | — |
| adjacency pages read for the 2000-edge hub | ~2000 index entries | — | **2 pages** |

## The three honest findings

1. **The native store is competitive AND more storage-efficient.** The raw C SRF `gph_neighbors` does a
   2000-edge hub expansion in **0.23 ms reading 2 adjacency pages** — matching the tuned relational
   index scan (0.25 ms) while touching ~1000× fewer pages. The read-once-per-page thesis holds.
2. **The SQL compatibility shim, not the engine, is the overhead.** `graph_store.neighbors()` →
   `gph_neighbors_ext` (a `LANGUAGE sql` wrapper + the id-map translation) adds **~5–10×** (0.23 → 1.1–2.5 ms).
   That is the exact per-neighbor id-map reverse-lookup cost **PERF-02/03** address. An adapter that calls
   the **raw native surface** (vid-based, identity-mode) gets the fast path; the compat `neighbors()`
   surface does not.
3. **Multi-hop via per-node expansion loses to a single optimized join.** The relational 2-hop is ONE
   planner-optimized join (8.3 ms); the native per-node expansion invokes the SRF once per 1-hop
   neighbor (2000×). Even at 0.23 ms/call the per-call model cannot beat one join. **The native AM's
   multi-hop win requires a single fused C traversal operator** (early-terminating, like `tjs_open`) —
   which TriDB HAS but gBrain, fusing app-side, does not call.

## What this means for gBrain-on-TriDB (reframed, honestly)

- **Do NOT pitch gBrain-on-TriDB as a graph-traversal speedup at this access pattern/scale.** Through
  the per-node SQL surface it is *slower*; through the raw surface it *ties* on single-hop.
- **The defensible win is architectural, not raw latency:** one WAL / transactional consistency across
  the vector + graph + relational legs (the graph cannot drift from the other stores — a bolt-on graph
  DB or a relational-links mirror can), one system to operate, and ~1000× fewer page reads per hop
  (which should widen at **cold cache / much larger graphs** — untested here; this was warm 20k).
- **To make it a latency win too**, the adapter must (a) call the **raw native surface** (bypass
  `gph_neighbors_ext`; land PERF-02/03), and (b) route multi-hop `traverseGraph`/`traversePaths` to a
  **single fused C operator** (a `tjs_open`-style early-terminating traversal), not per-node expansion.

## Honesty box

- Warm cache, single box, 20k/120k, one run of medians — not a cold-cache or at-scale (186k-page) run,
  where the 2-page-read locality should help the native side more. Re-run cold + at 100k–1M before any
  external claim.
- `gph_traverse_typed(...)` did not return a timing in the isolation harness (arg/type mismatch in the
  ad-hoc call) — the raw `gph_neighbors` number is the clean native-surface figure.
- The relational baseline is genuinely tuned (partial indexes, ANALYZE, no parallelism to keep it
  comparable). This is the number to beat, and honestly reported as beating the *naive* native path.

## Repro

```bash
scripts/add_pgvector.sh tridb/msvbase:gx10-v1 tridb/msvbase:gx10-v1-pgv     # build the shim image
# then on the GX10:
docker run --rm -u postgres -v $PWD/src/graph_store:/tmp/ext_v1:ro \
  -v $PWD/bench/gbrain_graph_bench.sh:/tmp/bench.sh:ro \
  -e N=20000 -e HUBS=20 -e HUB_FANOUT=2000 --entrypoint bash \
  tridb/msvbase:gx10-v1-pgv /tmp/bench.sh
```

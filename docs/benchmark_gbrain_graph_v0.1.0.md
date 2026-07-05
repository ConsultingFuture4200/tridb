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

## Addendum v0.2.0 — built (a) raw surface + (b) fused BFS operator, re-ran (parity-verified)

Per the v0.1.0 recommendations, built both and re-ran on the same substrate:
- **(a)** the bench now calls the **raw** `gph_neighbors` (vid resolved once via `gph_upsert_vertex`,
  NOT the unverified identity-mode assumption — that bug made the first BFS traverse the wrong vertex;
  caught by a ground-truth parity check).
- **(b)** a new fused C operator `gph_traverse_bfs(seed_vid, max_depth, type_id)` — full BFS in C
  (frontier + visited over the native adjacency, ONE call), the native counterpart to gBrain's
  recursive-CTE `traverseGraph`. **Parity verified**: it reaches the identical set as the relational
  CTE (and a `UNION` ground truth) at every depth.

| | Relational (tuned CTE / index) | Native (raw `gph_neighbors` / fused `gph_traverse_bfs`) |
|---|---:|---:|
| **20k** 1-hop hub (deg 2000) | 0.25 ms | 1.3 ms |
| **20k** 2-hop (reached 13k) | 24.6 ms | **22.2 ms** (native 10% faster) |
| **20k** 3-hop (reached 20k) | 94 ms | 98 ms (≈ tie) |
| **100k** 1-hop hub (deg 3000) | 0.34 ms | 0.90 ms |
| **100k** 2-hop (reached 21k) | 30.6 ms | 88.9 ms (native 2.9× slower) |
| **100k** 3-hop (reached 80k) | 146 ms | 577 ms (native 4× slower) |

**Verdict (honest, and it does not favor the thesis at warm cache):**
- **(b) helped, but only at small scale.** The fused BFS took multi-hop from **8× slower** (per-node
  SRF) to a **tie** at 20k — then **relatively WORSE at 100k** (3–4× slower). The relational engine's
  **set-based join** (one optimized hash-join per BFS level over the whole frontier) scales better than
  **per-node** adjacency walks; the fused BFS still pays per-node `gs_open`/HTAB/page-fetch overhead
  × (nodes reached), which grows with the frontier.
- **(a) did not flip single-hop.** The raw surface is faster than the SQL shim but still ~3–5× slower
  than the relational index scan — the native SRF's per-row return overhead vs the executor's index scan.
- **The native store's efficiency is real but doesn't cash out as traversal latency here.** 3 page reads
  for a 3000-edge hub is a genuine storage win, but warm-cache traversal is CPU/overhead-bound, not
  I/O-bound, so it doesn't show. The one untested regime where it *should* matter is **cold cache /
  graph exceeds RAM** (I/O-bound single-hop) — not run.

**Bottom line for gBrain-on-TriDB:** do NOT pitch it as a graph-traversal speedup — at warm moderate
scale it is at best a tie and at 100k it loses. TriDB's honest value for gBrain remains **architectural**
(one WAL / transactional consistency across the three legs; one system to run), not traversal latency.
A latency win would require either a genuinely I/O-bound regime, or a set-based native traversal
primitive (batch the frontier expansion in C the way the planner batches the join) — a redesign, not a
tweak. `gph_traverse_bfs` is committed and parity-correct; it is not a latency win at scale as-is.

## Addendum v0.3.0 — the I/O-bound / cold regime (the storage-locality thesis's best case)

`bench/gbrain_graph_cold.sh`: single-hop hub expansion (deg 3000), **cold** (16 MB `shared_buffers`,
post-restart), measuring **pages touched** (the cache-independent storage metric) over 30 fresh hubs.

| | pages touched (cold) | latency (cold) |
|---|---:|---:|
| relational (`idx_links_from` + scattered heap) | **84.7** | 0.40 ms |
| native AM (co-located adjacency) | **3.0** | 1.53 ms |
| ratio | **28× fewer reads for native** | relational still 3.8× faster |

**The storage-locality thesis is TRUE — and still doesn't produce a latency win here.** The native
store touches **28× fewer pages** (3 vs 85) for the same hub — the read-once-per-page adjacency claim
holds cold. But on a 128 GB Spark **nothing is actually disk-I/O-bound**: those 85 relational pages are
all OS-page-cache hits (cheap), so the native SRF's per-row overhead still dominates latency (1.5 vs
0.4 ms).

**When the 28× would matter:** only when the workload is genuinely I/O-bound — the graph exceeds RAM,
or storage is slow/remote/networked. For a personal gBrain (186k pages ≈ tens of MB) on a 128 GB box,
that never happens; it fits in RAM thousands of times over. So the native store's efficiency is real but
**latent and irrelevant at gBrain's scale on this hardware.**

### Final conclusion (all regimes)
- **Warm, moderate scale (gBrain's actual regime):** relational wins single-hop; fused BFS ties then
  loses multi-hop at 100k. Native does NOT win.
- **Cold / I/O metric:** native touches 28× fewer pages, but that doesn't convert to latency because
  gBrain fits in RAM.
- **Verdict:** TriDB's value for gBrain is **architectural** (one WAL / transactional consistency across
  the three legs; one system), full stop — **not** graph-traversal latency, in any regime tested. A
  latency win needs a workload that outgrows RAM, which a personal brain does not.
- **Bug found along the way:** PERF-02 identity fast-path is silently wrong (`vid = ext_id-1`, not
  `==`) — DEV-1352.

## Repro

```bash
scripts/add_pgvector.sh tridb/msvbase:gx10-v1 tridb/msvbase:gx10-v1-pgv     # build the shim image
# then on the GX10:
docker run --rm -u postgres -v $PWD/src/graph_store:/tmp/ext_v1:ro \
  -v $PWD/bench/gbrain_graph_bench.sh:/tmp/bench.sh:ro \
  -e N=20000 -e HUBS=20 -e HUB_FANOUT=2000 --entrypoint bash \
  tridb/msvbase:gx10-v1-pgv /tmp/bench.sh
```

# Fusion head-to-head — TriDB `tjs_open` vs a tuned multi-store (v0.1.0)

> **Status: EXECUTED. First speed win attributable to TriDB's actual differentiator** — the
> in-process fused operator (no cross-system round-trips), not HNSW-vs-HNSW. At N=200k, matched
> recall, TriDB's `tjs_open` beats an app-side Milvus→Neo4j→pgvector pipeline at **every** hop
> depth (1.29×–11.5×), **even on loopback** (the multi-store's best case). But the advantage
> **shrinks** as hops deepen — it comes from eliminating fixed cross-system overhead, so it is
> largest on cheap queries, NOT "grows with hops." Compute-regime (dim-384, RAM-resident); this
> is not the I/O-bound thesis (which is structurally unsupported — see the co-location audit).

## TL;DR

Matched recall@10 (~0.997 TriDB vs 1.000 baseline), p50 latency, N=200,000, loopback baseline:

| hop | TriDB `tjs_open` p50 | Multi-store p50 | **TriDB speedup** | graph reach / bridges | baseline bytes shipped |
|---|---|---|---|---|---|
| 1 | **3.7 ms** | 41.9 ms | **11.5×** | ~515 | 9.9 KB |
| 2 | **94.1 ms** | 322.7 ms | **3.4×** | ~21,000 | 337 KB |
| 3 | **1,729 ms** | 2,227 ms | **1.29×** | ~123,000 | 1.97 MB |

- **TriDB wins at every hop.** This is the first benchmark win in this project attributable to the
  *fused operator* — TriDB avoiding the vector→graph→filter round-trips the multi-store must pay —
  rather than raw ANN (the earlier 2.1× vector-leg win was just HNSW-vs-HNSW).
- **The advantage shrinks with hop depth (11.5× → 3.4× → 1.3×), the OPPOSITE of "grows with hops."**
  Mechanistically clear: TriDB's edge is eliminating *fixed* per-query cross-system overhead
  (3 round-trips + serialization). At hop=1 the query is cheap, so that fixed overhead is nearly the
  whole cost → 11.5×. At hop=3 the intrinsic work (processing ~123k reached/bridge nodes) dominates
  *both* systems, so the fixed saving is a smaller fraction → 1.3×. TriDB still wins by avoiding the
  1.97 MB of cross-store shipping, but the gap narrows.

## Mechanism (what TriDB avoids)

The multi-store pipeline is: Milvus ANN (seed) → **ship seed ids** → Neo4j `*1..h` traversal →
**ship reached ids** → pgvector filter/rank → app-side merge. Instrumented per query:

| hop | round-trips | bytes shipped across store boundaries | reached nodes |
|---|---|---|---|
| 1 | 3 | 9.9 KB | 512 |
| 2 | 3 | 337 KB | 20,982 |
| 3 | 3 | 1.97 MB | 122,995 |

TriDB's `tjs_open` does the same fusion in ONE in-process call over libpq: ANN seeds from the
`vectordb` HNSW leg → native `graph_store` BFS from all seeds → vector-ranked merge with bridge
injection + early termination — **0 bytes shipped between systems, 1 round-trip**. That eliminated
per-query overhead is the entire source of the win.

## Method

- **Matched scale:** N=200,000 articles, 8,208,179 induced edges, loaded identically into the engine
  (`tridb/msvbase:gx10-v1-hnswcap`, port 5447) and the isolated baseline (Milvus :19531 dim-384
  HNSW/COSINE, Neo4j :7688 200k nodes / 8.2M rels, pgvector :5434 200k rows). Counts reconcile.
- **Matched recall:** for each hop, both sides swept (TriDB m_seeds/term_cond grid; baseline
  seeds×ef grid) and compared ONLY at equal recall@10 vs a per-query exact fused oracle
  (Milvus-equivalent ANN ∪ exact h-hop graph reach). recall_matched=true each hop.
- **Timer parity:** both client-timed over TCP (psycopg to the engine; pymilvus/neo4j/psycopg to the
  stores). Warm: the TriDB HNSW index was warmed once (cold `LoadIndex` rebuild = **~96 s** at 200k,
  disclosed — the multi-store has no equivalent cold load).
- Harness: `bench/wiki_fusion.py`; raw results `bench/results/wf200k_lean.json`.

## Honest caveats

- **Lean run** (30 queries, 1 run/config) — a first-cut signal, not a tight-CI publication number.
  The trend is large and monotone across hops and both grid points, but a fuller sweep (more queries,
  ≥3 runs) should confirm the magnitudes before any external claim.
- **Loopback favors the baseline.** All three stores run on localhost = minimal glue cost, the
  multi-store's *best* case. A real-network (split-machine) deployment adds real latency to each of
  the 3 round-trips → TriDB's advantage would only *grow*, most at shallow hops. So a loopback TriDB
  win is conservative. (Real-network is the natural follow-up.)
- **Compute regime, dim-384, RAM-resident.** Not the spec's I/O-bound thesis (which the co-location
  audit found structurally unsupported). This measures the *fusion* mechanism, not page locality.
- **hop-3 is a large-intermediate stress case** (reach ~123k ≈ 61% of the corpus). Realistic 2–3-hop
  wiki QA rarely reaches that far; the hop-1/hop-2 numbers are the more representative operating points
  (and where TriDB's advantage is largest anyway).
- **Cold-start asymmetry:** TriDB pays a ~96 s one-time `LoadIndex` per cold backend (DEV-1235); the
  multi-store does not. Warm steady-state is compared above; the cold cost is disclosed, not hidden.

## Verdict

The fusion thesis gets its **first real support**: TriDB's in-process fused retrieval beats a tuned
multi-store at matched recall across all hop depths, even on the baseline's best case, and the
mechanism (eliminated cross-system round-trips / MB of shipped intermediates) is directly measured.
This is a genuine, differentiator-attributable win — distinct from the earlier HNSW-vs-HNSW result.

The nuance to carry honestly: the win is **largest on cheap/shallow queries and narrows as the
intrinsic work grows** — TriDB removes fixed overhead, it does not make deep graph traversal cheap.
So the pitch is "fused single-engine retrieval is materially faster than orchestrating three stores,
especially at interactive query sizes" — plus the standing ADR-0017 value (one-WAL consistency +
operational simplicity), which is independent of and complementary to this latency result.

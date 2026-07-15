# Gate B spike — the fusion win off the fork (stock PG17 + pgvector + un-forked graph AM)

**TL;DR — GATE B: PASS, decisively.** The same pinned 1,002,331-entity Wikidata slice, the same
50 oracle queries, the same multi-store baseline, the same `publication_gate` — but the TriDB
side running on **stock PostgreSQL 17.10 + pgvector + the un-forked graph AM on 8KB pages**
(no MSVBASE fork anywhere in the engine path):

| Platform | TriDB (fused filter-first) | multi-store | speedup |
|---|---|---|---|
| PG 13.4 fork, 32KB pages (D1, 2026-07-14) | 0.27 ms @ recall 0.992 | 3.16 ms @ 0.986 | **11.90×** |
| **Stock PG 17.10 + pgvector, 8KB pages (2026-07-15)** | **0.14 ms @ recall 0.992** | 3.34 ms @ 0.986 | **23.68×** |

The un-fork question ("does the fusion win survive off the fork?") answers not merely yes —
**stock PG17 runs the identical fused statement ~2× faster than the 13.4 fork**, even paying
the 8KB-page adjacency penalty (ADR-0015 E2). Recall and BFS work are identical on both
platforms (0.992, examined median 42), so this is the same computation on a newer executor.

## What ran

- Engine: `pgvector/pgvector:pg17` + PGXS toolchain (`scripts/pg17/Dockerfile`, container
  `tridb-wikidata-pg17`), graph AM built from `src/graph_store` (BLCKSZ>=8192 capability +
  PG14/15 version guards — commits 4956086, 8af30b2), pgvector HNSW m=16/ef_construction=64,
  3/3 healthy builds (1 parallel + 2 serial; NB parallel builds need `--shm-size` ≥
  maintenance_work_mem in docker).
- Loader/harness: `--dialect stock` / `WD_ENGINE_DIALECT=stock` (vector(384) column, pgvector
  literals) — commit 27c45fe; graph side byte-identical to the fork load (same extension
  source, dictionary registration deterministic: identical type ids).
- Gate conditions: edge parity 7,422,959 == 7,422,959; recall |Δ|=0.006 < 0.02; examined
  median 42 (>0); boundary equalized (both sides client-side over TCP); HNSW 3/3.
- Artifacts: `bench/results/wd_1m_pg17_{report.md,graded.json,baseline.json,engine_load_manifest.json}`.
  Slice/dump pins identical to `docs/wikidata_spike_v0.2.0.md`.

## Honesty notes

- The fused filter-first operating point does not exercise pgvector's ANN scan (the vector leg
  is an exact rank over a ~18-candidate set) — so this pass does NOT test the ADR-0015 E3 gaps
  (per-candidate distance exposure, budget-shaped termination). Those apply to the seedless /
  vector-first operator re-home (roadmap 2.5), which remains open work; pgvector's index is
  built, healthy, and carried through the gate's health discipline regardless.
- One load caveat found live: the initial parallel pgvector build died on docker's default
  64MB /dev/shm ("could not resize shared memory segment"); documented in the loader and the
  runbook. The relational+graph load had committed (autocommit per statement), so the index was
  rebuilt without a reload; the manifest's edge_type_map was re-harvested from the authoritative
  `graph_store.edge_type` dictionary.
- 8KB-page adjacency penalty: invisible at this operating point (BFS reach ~50 vertices,
  RAM-resident). It matters for large-reach traversals — the roadmap 2.3 CSR work retains its
  motivation for >1M graphs and deep traversals, not for this query class.

## Consequences (roadmap)

- **Phase 2.2's premise is proven**: pgvector + the un-forked graph AM preserve (double) the D1
  fusion win. The fork is now demonstrably a launch vehicle, not a dependency (ADR-0015's FAQ
  framing holds with measured numbers behind it).
- Remaining D2 phases stand: 2.3 CSR footprint (motivated by scale, not this benchmark),
  2.4 packaging/CI (the pg17 image + runner are the seed), 2.5 operator re-home for the
  vector-first legs (where the E3 gaps live — that is the *second* half of Gate B, scoped to
  the seedless mode that D1 already excludes from claims).
- Plan 043 (fork seedless HNSW non-determinism) is now moot on the target platform, exactly as
  the roadmap's resolved decisions predicted.

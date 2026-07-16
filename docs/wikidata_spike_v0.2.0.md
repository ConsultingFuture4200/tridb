# Wikidata-on-TriDB — measured pass, report v0.2.0

**TL;DR — GO.** v0.1.0's "GO-pending-one-measurement-pass" verdict is resolved: the pass ran on
the GX10/Spark (2026-07-14) against a pinned 1,002,331-entity / 7,422,959-edge Wikidata slice and
**both halves of the value story cleared their gates**:

- **Fusion speed (Harness B, through `publication_gate`):** fused filter-first KBQA in TriDB =
  **0.27 ms** median vs **3.16 ms** for the Milvus+Neo4j+pg assembly at matched recall
  (0.992 vs 0.986, |Δ|=0.006 < 0.02) — **11.90×** at N=1,002,331. Reproduces the wiki-200k
  fusion result (11.5×/16.7×) on a natively tri-modal public corpus at 5× the scale.
- **One-WAL consistency (Harness A, live edit-firehose replay):** replaying a pinned real
  2,000-edit window, 3 runs: multi-store **12/900 torn reads (1.33%)** vs TriDB
  **15/51,332 (0.029%)** — ~46× lower per-read, and TriDB's residual is *provably confined to
  the graph leg* (vector+relational share one MVCC snapshot per read statement), i.e. the known
  DEV-1166 snapshot-isolation gap already scheduled for D3 hardening. A true-0 claim awaits
  DEV-1166; the measured delta is reported as-is, not rounded to zero.

Roadmap Gate A (docs/tridb_productization_roadmap_v0.1.0.md): **PASS** — the fusion win
reproduces at ≥1M under matched recall; the consistency-only kill-criterion is NOT triggered.

---

## Headline table (all numbers gated; raw artifacts in bench/results/wd_1m_*)

| Claim | TriDB | multi-store | Gate conditions held |
|---|---|---|---|
| KBQA filter-first, k=10, hops=2, 50 queries | 0.27 ms @ recall 0.992 | 3.16 ms @ recall 0.986 (best combo h2f4096) | graph parity 7,422,959 == 7,422,959; HNSW builds 3/3 healthy; examined median 42 (>0, ≪4000 cap); recall |Δ|=0.006 < 0.02; timer boundary equalized (both client-side over TCP) |
| Torn cross-modal reads, real edit window | 15/51,332 (0.029%, graph-leg only) | 12/900 (1.33%) | writer replays the FULL window; reader spans writer lifetime; watched entity = window's hottest (23 edits), pinned |

## Surface honesty (what the TriDB side actually ran)

ONE fused SQL statement per query — native typed multi-hop BFS in C
(`graph_store.gph_traverse_bfs(seed, max_depth, type_id)`) → relational `P31 @>` filter →
exact vector distance rank — one round-trip, one system, one snapshot. Semantically identical
to the oracle, so recall is matched — measured and tie-break-pinned (residual <1.0 is float noise, gated).

- **`tjs_open` is NOT part of this claim.** Its typed-traversal integration is the plan 038
  residual (typed traversal landed as native AM SRFs, not operator arguments); the v0.1.0
  emit that pretended otherwise errored on first live contact and was rewritten.
- `graph_store.assume_dense_open = on` (advisor 048 O(1) vertex locate) is SET in the emitted
  session and disclosed: the load satisfies its dense-in-order precondition and every lookup is
  hard-verified by the AM (ERROR on violation, never silent). Without it the linear locate walks
  ~15k vertex pages per BFS (17 ms); the number without the GUC is a measurement of the known
  v1 locate gap, not of fusion.
- The baseline is granted the same exactness: its rank leg is the exact pg `<=>` rerank
  (wiki_h2h fairness convention; no gratuitous Milvus round-trip is charged to it).

## Regime honesty (unchanged from v0.1.0)

Compute-bound at 1M (RAM-resident dim-384 floats); the I/O-locality thesis stays dead. Value =
(1) fusion speed, (2) one-WAL cross-modal consistency. Out-direction only (backlinks await the
ADR-0016 reverse index). Seedless/vector-first stays out of scope (plan 043, retired via
pgvector in D2 per the roadmap's resolved decisions; spec Addendum A1).

## Sizing curve (v0.1.0 table, measured cells filled)

| Slice | Where | Load | Harness A torn Δ | Harness B @ matched recall |
|---|---|---|---|---|
| 1,002,331 (geo BFS closure, 4 hops) | GX10/Spark | engine 1M vertices + 7.42M typed edges + HNSW (3/3 healthy builds); Milvus/Neo4j/pg at parity | live: 0.029% vs 1.33% (46×) | 0.27 ms vs 3.16 ms (**11.90×**) |
| 10M+ | GX10 (128 GB) | still gated — no claim | gated | gated |

## What the discipline caught on the way (why the gate earns its keep)

1. **Advisor plan 034 reverted** (DEV-1345): the cached vid→ext translator never invalidates on
   DML; caught by tjs_filter_first PASS 9 on the first GX10 suite run, bisected, patch deleted.
2. **Oracle cycle bug**: `typed_reach` violated its own "excludes src" contract on symmetric
   properties (P47) — 33/50 queries missed exactly [anchor]; gate refused the headline until
   oracle + baseline + engine agreed.
3. **BFS arg order** (seed, max_depth, type_id) — first emit passed (seed, type_id, hops);
   recall 0.0, examined 0; gate refused.
4. **O(V) vertex locate** dominating the fused statement (15,236 buffer hits/query) — fixed by
   the documented 048 opt-in, disclosed above.

## Reproducibility pins

- Dump: `latest-all.json.gz`, dump date **2026-07-07**, 154,601,777,362 bytes,
  sha256 `8effd2ddcd7de39d9c43b07dcb8269e49429086a8ab6e6231b3bb181b7d40099`.
- Slice: BFS closure, seeds Q2,Q15,Q48,Q46,Q49,Q18,Q51,Q538,Q30,Q142,Q183,Q145,Q148,Q17,Q159,
  Q668,Q155, target 1,030,000, 4 hops scanned → 1,002,331 usable entities / 7,422,959 in-slice
  edges (`bench/results/wd_1m_slice_manifest.json`); ingest via `tools/wikidata_compact.py`
  sidecar + `tools/wikidata_ingest.py --compact`; embeddings BAAI/bge-small-en-v1.5 dim 384,
  L2-normalized at write.
- Edit window: `bench/data/wikidata_edit_window_20260713.jsonl` (2,000 edits, 1,487 entities,
  recorded 2026-07-13T10:31–10:40Z, pin sidecar committed).
- Engine: image `tridb/msvbase:gx10-d1` (full patch chain minus reverted 034; engine suite
  177/177 green on GX10, clean on x86); loaders `tools/wikidata_engine_load.py` /
  `tools/wikidata_baseline_load.py` (dense-vid contract hard-asserted at load).
- Gate env: `WH_ENGINE_EDGES=WH_NEO4J_EDGES=7422959`, `WH_HNSW_HEALTHY_BUILDS=3/3`,
  `WH_BOUNDARY_PARITY=1` (both sides client-clocked over TCP: TriDB via a psql client container
  on the docker bridge, baseline via psycopg/neo4j drivers on the host).
- Artifacts: `bench/results/wd_1m_{report.md,graded.json,oracle.json,baseline.json,
  slice_manifest.json,engine_load_manifest.json,baseline_load_manifest.json}` and
  `bench/results/wikidata_consistency_live_20260713{,_r2,_r3}.json`.

## Verdict

**GO.** The demonstrated-architecture claim of roadmap D1 is met: a stranger-reproducible ≥1M
tri-modal result on a public corpus, both value legs measured through the honesty gate. Next
per the roadmap: publish + tag v0.1.0 (D1.3), then Gate B early in D2 (does the fusion win
survive over pgvector's HNSW).

# TriDB Spec v0.1.0

> **Version:** 0.1.0 · **Created:** 2026-06-21 · **Status:** Draft — markers #1 (GX10/GB10 live
> build signed off) and #2 (real-corpus repro shipped) resolved, 1 remaining.
> **Lineage:** Clone of AkasicDB (SIGMOD Companion '26, DOI 10.1145/3788853.3801609),
> built on Chimera (PVLDB 18(2):279–292) + VBASE (OSDI '23).
> **Source of truth:** Linear doc `tridb-spec-v010-59ba388c777f`. This is a mirror.

## §1 Project identity

A single-process database engine that natively executes vector similarity search, graph
traversal, and relational filtering inside one query plan, for Omni RAG retrieval on
local hardware (GX10, 128 GB).

**Is not:** an orchestration layer over separate DBs; distributed/clustered; hosted; a
general graph analytics engine.

## §2 Core thesis & scope

The win is collapsing multi-turn, multi-system retrieval into a single plan that enforces
top-k during execution, avoiding materialize-transfer-prune.

Scope decisions (locked):
- Native graph store from the start (no Apache AGE scaffold).
- Target hardware: GX10.
- Three stores only. BM25 seam architected but closed.
- v1 does NOT build a full cost-based optimizer — only the single highest-value planning
  decision: cross-modal leg ordering by selectivity (FR-6). Cost models, cardinality
  estimation, adaptive re-planning deferred to v2+.

## §3 Keystone move

Fork MSVBASE (inherit relational + vector + Volcano executor + one transaction manager),
then build the native adjacency-list graph store inside the same Postgres process so it
shares the existing transaction manager.

## §4 The three stores

| Store | Primitive | Backing | Index |
| -- | -- | -- | -- |
| Vector | Similarity (ANN) | MSVBASE segments | HNSW |
| Graph | Traversal | Adjacency-list access method | B-tree |
| Relational | Filter | PostgreSQL tables | B-tree |

- §4.2 Join ordering: selectivity-based leg ordering (the 20%).
- §4.3 B-tree index over graph elements for attribute-based access.
- §4.4 Every operator is a Volcano iterator (Open/Next/Close).

## §5 Canonical query (single template for v1)

```sql
SELECT chunk
FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
  COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
WHERE timestamp IN :selected_time_range
ORDER BY src_embedding <-> :question_embedding
LIMIT 5;
```

## §6 Functional requirements (referenced by issues)

- FR-2 / FR-7 — native graph topology + single shared transaction manager (Phase 1).
- FR-3 — HNSW exposed as relaxed-monotonicity Open/Next/Close iterator.
- FR-4 / FR-5 — TJS operator composes all three legs with one global top-k.
- FR-6 — cross-modal join-order heuristic (the 20%).

## §7 Success metrics

- SM-1: ≥5× intermediate-result reduction vs. baseline (selective queries)
- SM-2: lower latency on ≥80% of queries
- SM-3: <25% of corpus examined for k=5
- SM-4: ≥99% answer-set parity with baseline
- SM-5: 100% transaction atomicity

Baseline = out-of-DB integration of Neo4j + Milvus + Postgres (AkasicDB Scenario 2,
Figure 3a).

## §8 Execution invariant (TR-1)

Every operator honors Open/Next/Close + early termination. Non-negotiable.

## §12 Open markers

- **#1 RESOLVED by DEV-1160/1161:** MSVBASE builds on ARM64 with 3 documented deltas
  (aarch64 CMake, SPTAG excluded, hnswlib portable → HNSW works); the GX10/GB10 live build +
  engine suite are now signed off (`docs/STATUS.md`), so the earlier "live build TBC" is closed.
- **#2 RESOLVED:** benchmark corpus — real public corpora now ship with a one-command repro
  (`docs/benchmark_public_repro_v0.1.0.md`), settling the synthetic-vs-real question. Owned by DEV-1172.
- **#3:** edge properties beyond single `:related_to`. Owned by DEV-1163.

## Build-vs-borrow

| Piece | Source |
| -- | -- |
| Relational + vector + relaxed-monotonicity operator | MSVBASE, lifted |
| Volcano executor | MSVBASE (extends Postgres) |
| Native graph store | **Build** (Chimera design) |
| Shared txn manager | Inherited (never leave Postgres) |
| SQL/PGQ surface | Postgres parser + pgvector + AGE grammar ref |
| Tri-modal join ordering | **Build** (Chimera cost model, the 20%) |

## Addendum A1 (2026-07-13) — D1 demonstrated claim is FILTER-FIRST only

Scope ruling for Destination 1 of `docs/tridb_productization_roadmap_v0.1.0.md` (roadmap phase 1.1):

- **Every D1 headline runs the filter-first `tjs_open` physical path** (selective typed-edge +
  entity-type constraint first, vector ranking second) — the regime green at 1M (DEV-1290) and the
  regime ADR-0018's Wikidata queries naturally select.
- **The seedless / vector-first leg is OUT OF SCOPE for D1.** Its blocker — non-deterministic
  seedless HNSW iteration in the fork (plan 043) — is explicitly NOT being fixed: per the roadmap's
  resolved decisions, the fork iterator is retired in D2 phase 2.2 by adopting pgvector's
  deterministic HNSW. Any seedless number before then is unpublishable by policy.
- **Enforcement is mechanical, not editorial:** `bench.wiki_h2h.publication_gate` (reused verbatim
  by the Wikidata harness) refuses a headline without matched recall, graph-set parity, healthy
  HNSW builds, and `examined > 0`; the harnesses only emit filter-first calls. A seedless headline
  cannot pass through the gate because no harness produces one.
- SM-1..SM-5 (§7) are unchanged; they are simply *evaluated at the filter-first operating point*
  for D1, and SM-4 parity is reported as a recall curve, not a bare percentage (DEV-1169 ruling).

## Addendum A2 (2026-07-16) — §5 canonical query executes on stock PG 16/17 via tjs_open (plan 075)

The v1 front door `graph_store.graph_query(text)` (DEV-1167) now lowers the ONE §5 template on
BOTH engines:

- **Fork (MSVBASE)**: unchanged — a single `tjs(...)` call (7- or 8-arg, DEV-1169/1290).
- **Stock PG 16/17**: when the fork `tjs()` is absent and the `tjs_pg` extension (ADR-0019) is
  installed, the same template lowers to a single `public.tjs_open(regclass, k, term_cond=0,
  m_seeds=0, hops=1, id_col='id', filter, query vector, src, edge_type)` call. The canonical
  edge label `related_to` is resolved through the `graph_store.edge_type` catalog (seeded id 1);
  an absent catalog row RAISES — the lowering never widens to "any edge". `tjs_open`'s ranked
  entity ids are joined back to `entities` for the projected `chunk` column in the operator's
  own emit order (`WITH ORDINALITY`), never heap order. With the v1 pinned `src.id`, `tjs_open`
  always runs its filter-first body; `graph_store.last_join_order()` reports `filter_first`.

Grammar ruling: the accepted template is unchanged except that the `:question_embedding`
literal now admits BOTH the fork brace dialect (`'{...}'`) and the pgvector bracket dialect
(`'[...]'`), matched delimiters only; each lowering converts to its engine's dialect. No other
template expansion — off-template text still fails closed (golden rule 4). Where neither
operator is installed, `graph_query` RAISES an explicit no-compatible-lowering error.

Proof: `test/canonical_stock_e2e_test.sql` (STOCK_TESTS + CI job `stock-pg`, PG 16 and 17),
including direct-vs-canonical ordered parity on the same fixture.

## Addendum A3 (2026-07-16) — TR-1 graph-work bound for the stock operator (plan 077)

§8's Open/Next/Close + early-termination invariant is made mechanical for the stock
`tjs_pg` operator: the graph leg of one `tjs_open` call performs at most
`tjs.graph_work_budget` edge-steps (default 65536) and holds graph-leg state bounded by
the same budget, independent of |V|/|E|. Reach acquisition is a pull-based multi-hop
iterator over the native AM; no complete reachable set is ever materialized. A
budget-capped call returns a deterministic-prefix result and MUST disclose it:
`tjs_open_graph_censored() = true`, with `tjs_open_graph_examined()` reporting
edge-steps; an uncensored result is byte-identical to the pre-077 contract (071 parity
harness green). Seedless retains plan 087 fork-parity semantics: seed_window =
max(m_seeds*8, m_seeds+32) with nearest-in-window seed selection, floor(k/2)-min-1
bridge cap, uniform drop accounting. Benchmarks/harnesses MUST report the censor flag
next to any headline (a capped run is a different operating point, not a win).

See `docs/decisions/0020-stock-tjs-incremental-graph-leg.md` for the full contract.

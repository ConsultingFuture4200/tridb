# TriDB Spec v0.1.0

> **Version:** 0.1.0 · **Created:** 2026-06-21 · **Status:** Draft — marker #1 resolved by
> DEV-1160 desk spike, 2 remaining.
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

- **#1 RESOLVED (desk) by DEV-1160:** MSVBASE builds on ARM64 with 3 documented deltas
  (aarch64 CMake, SPTAG excluded, hnswlib portable → HNSW works). GX10 live build TBC.
- **#2:** benchmark corpus — synthetic LDBC-style vs. real Omni RAG. Owned by DEV-1172.
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

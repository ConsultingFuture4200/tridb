# 0001 — Architecture Overview

**Status:** Accepted

**Date:** 2026-06-23

**Supersedes:** none

---

## Context

TriDB is a tri-modal DBMS that serves *one* query shape natively: a graph
traversal whose endpoints carry vector embeddings and relational attributes,
filtered and ranked in a single physical plan (the canonical query in
`PROJECT.md`). The reference baseline for this workload is the out-of-DB stack —
Neo4j for traversal, Milvus for similarity, Postgres for filters — merged at the
application layer (AkasicDB Fig 3a). That baseline has two structural costs we
must beat:

1. **Cross-system transaction cost.** Three engines means three storage
   managers, three WALs, and no shared transaction boundary. Atomicity across a
   graph edge, its vector, and its row is impossible without a distributed
   commit protocol (2PC) or eventual-consistency glue. SM-5 (100% txn
   atomicity) is unachievable on that architecture by construction.

2. **Materialize-then-merge cost.** Each engine returns a full intermediate
   result; the app layer joins/re-ranks them. The traversal engine does not know
   the vector ranking, the vector engine does not know the graph topology, and
   neither knows the relational filter. Every modality runs to completion before
   the next can prune. This is the O(|R_graph| + |R_vector| + |R_filter|)
   materialization wall that SM-1 (>=5x intermediate-result reduction) and SM-3
   (<25% corpus examined) exist to break.

The two costs are linked: you cannot fuse the operators into one early-
terminating plan unless they live in one executor over one storage manager.
So the architectural problem reduces to a single question — *how do we get a
native graph store, an HNSW vector index, and relational tables under one
transaction manager and one Volcano executor?*

Two prior systems answer parts of this. **VBASE/MSVBASE** (OSDI '23) already
embeds an HNSW vector index inside a PostgreSQL fork and exposes it to the
Volcano executor with relaxed-monotonicity early termination — i.e. vector +
relational + one txn manager, solved. **Chimera** (PVLDB 18(2)) demonstrates a
graph access method co-resident with relational storage sharing a single
transaction manager — the proof that a native adjacency-list store can live
inside an RDBMS without a second WAL. Neither alone gives us all three modalities
fused; together they define the build.

---

## Decision

### Keystone: fork MSVBASE, then build a native graph store inside the same Postgres process.

We fork **MSVBASE** (a PostgreSQL 13.4 fork: HNSW vector index + Volcano
executor + one transaction manager, built `--with-blocksize=32`) and add a
**native adjacency-list graph store as a Postgres access method** in the *same*
backend process. The graph store registers with the existing storage manager and
WAL; it is not a separate engine and not a set of relational join tables.

**Why this sidesteps cross-system transactions:** because the graph store is an
access method inside the Postgres backend, an edge insert, a vector insert, and a
row insert are all logged to the *same* WAL and committed under the *same*
transaction manager. There is no second log to coordinate, so there is no 2PC,
no distributed commit, and no consistency-glue layer. Atomicity (SM-5) is
inherited from Postgres's existing MVCC + WAL rather than engineered on top of
three systems. This is the entire reason the fork-then-embed path exists: every
alternative that keeps the graph engine separate reintroduces the cross-system
commit problem we are trying to delete.

### Three-store closure set: vector / graph / relational, nothing else.

TriDB exposes exactly three physical stores, each mapped to exactly one
primitive operation:

| Store | Access method | Index | Primitive |
|-------|---------------|-------|-----------|
| Vector | HNSW (from MSVBASE) | graph-structured ANN | similarity (`<->`) |
| Graph | native adjacency-list (BUILD) | B-tree over edge lists | traversal (`MATCH`) |
| Relational | Postgres heap | B-tree | filter (`WHERE`/`IN`) |

The closure rationale: the canonical query decomposes into precisely these three
operations — rank by similarity, traverse edges, restrict by attribute — and
nothing else. Adding a fourth store (e.g. full-text/BM25) widens the operator
surface, the planner's join-order search space, and the txn/recovery test matrix
without serving the v1 query. The BM25 seam is **architected but CLOSED for
v1**: the access-method registration and planner hooks leave room for a fourth
store, but no BM25 index, operator, or cost model ships. Three stores is the
minimal closure that covers the query; we hold the line there.

### Load-bearing invariant: TR-1 early termination.

Every operator in TriDB is a Volcano iterator (`Open`/`Next`/`Close`) that can
**terminate early** — produce the top-k and stop without draining its input. No
blocking operator (no full sort-before-emit, no hash-build-before-probe, no
materialize-all) is permitted on the canonical path. This is the load-bearing
constraint, not a performance nicety:

- A *single* blocking operator anywhere on the path forces full materialization
  of its subtree, which collapses SM-1 (intermediate-result reduction) and SM-3
  (<25% corpus examined) back to the baseline's materialize-then-merge cost. The
  efficiency thesis is forfeit the moment one operator blocks.
- Early termination is what lets the vector `ORDER BY ... LIMIT 5` push a bound
  *into* the traversal and the filter, so the plan examines a fraction of the
  corpus instead of all of it. This pruning is only sound because MSVBASE's HNSW
  iterator supports VBASE-style relaxed-monotonicity early stop; we extend the
  same discipline to the new graph iterator.

TR-1 is therefore a *correctness-of-design* gate: any new operator that cannot be
expressed as an early-terminating iterator is rejected at review, not optimized
later.

### Build-vs-borrow split.

| Concern | Disposition | Source |
|---------|-------------|--------|
| Relational storage + B-tree | **Borrow** | MSVBASE / Postgres 13.4 |
| Vector store (HNSW index + `<->` operator) | **Borrow** | MSVBASE |
| Volcano executor + early-termination framework | **Borrow** | MSVBASE / Postgres |
| Transaction manager + WAL + MVCC | **Borrow** | Postgres (single, shared) |
| 32KB page on-disk layout assumptions | **Borrow/Inherit** | MSVBASE `--with-blocksize=32` |
| Native adjacency-list graph store (access method) | **BUILD** | new, this project |
| `GRAPH_TABLE` / `MATCH` operator as early-terminating iterator | **BUILD** | new |
| Join ordering / plan fusion across the three stores | **BUILD** | new |
| BM25 store | **NOT BUILT** (seam only) | deferred |

The discipline: we borrow everything that already exists and is correct
(relational, vector, executor, txn manager) and build only the two things that do
not exist anywhere in fused form — the **native graph access method** and the
**join ordering that fuses traversal + similarity + filter into one early-
terminating plan**. The graph store follows Chimera's co-resident-access-method
pattern; the fusion planning is the genuinely novel work and the locus of SM-1/
SM-2/SM-3.

### GX10 hardware gating.

The native C work (graph access method, on-disk adjacency layouts against 32KB
pages, executor integration) is **GX10-gated**: it targets and is validated on
GX10 hardware (ARM64 + CUDA, 128GB). Design work — ADRs, operator contracts,
plan-fusion specs, schema and layout design — is **not** gated and proceeds on
any workstation. The gate exists because on-disk layout decisions (page packing,
edge-list block boundaries) and build flags (`--with-blocksize=32`) must be
verified on the ARM64 target before they are considered shipped; a layout that
passes on x86 is not evidence on GX10.

---

## Consequences

**Positive**

- **SM-5 atomicity is free.** One WAL, one txn manager, one MVCC. Cross-modal
  atomicity is inherited from Postgres, not built. This is the keystone payoff.
- **TR-1 makes SM-1/SM-3 reachable.** End-to-end early termination lets the
  vector `LIMIT` bound prune the traversal and filter, so intermediate results
  shrink and <25% of the corpus is examined.
- **Minimal new surface.** We write a graph access method and a fusion planner,
  not a database. Everything else is borrowed and already production-tested.
- **One operator vocabulary.** All three stores speak the same Volcano
  `Open/Next/Close` contract, so plan fusion is an exercise in iterator
  composition rather than cross-engine glue.

**Negative / accepted costs**

- **Coupled to a Postgres 13.4 fork.** We inherit MSVBASE's fork point and its
  divergence from upstream Postgres. Security/feature backports from modern
  Postgres are non-trivial. Accepted: the txn-manager-sharing benefit dominates.
- **Native C against 32KB pages.** Adjacency-list layouts must account for
  32KB blocks; this is fiddly, GX10-gated, and slower to iterate than a
  relational prototype would be. Accepted: relational edge tables would violate
  the "graph is a native access method" invariant and reintroduce join-table
  cost.
- **TR-1 constrains the operator set.** Some textbook plans (hash joins with a
  blocking build, full sorts) are off the table on the canonical path. Accepted:
  blocking operators forfeit the thesis.
- **Single-process scaling ceiling.** Staying in one Postgres backend means we
  scale within Postgres's process/connection model, not by sharding across
  engines. Accepted for v1; the canonical query is latency-bound, not
  throughput-sharded.

**Neutral**

- BM25 remains a designed-but-empty seam. Opening it is a future ADR, not a
  code change deferred mid-flight.
- Baseline parity (SM-4 >=99% answer-set parity) is measured against the
  Neo4j+Milvus+Postgres merge, which remains the reference oracle.

---

## Alternatives considered

### A. Out-of-DB merge (the baseline): Neo4j + Milvus + Postgres at the app layer.

Rejected. This *is* the baseline we must beat. It forfeits SM-5 (no shared txn
boundary; atomicity requires 2PC or eventual consistency) and forfeits SM-1/SM-3
(each engine materializes a full result before the app merges them — the
materialize-then-merge wall). It is the thing the whole project exists to
out-perform.

### B. Fork MSVBASE, add graph as relational join tables.

Rejected. Keeps everything in one txn manager (good) but models edges as
relational rows joined at query time. Traversal becomes a self-join whose cost is
governed by the relational planner, not an adjacency-list walk; multi-hop
traversal degenerates into repeated joins with no native early-terminating
neighbor iterator. Violates the "graph topology is a native access method, never
relational join tables" invariant and surrenders SM-1/SM-3. The whole point of
Chimera's contribution is that you do *not* have to do this.

### C. Embed a separate graph engine (e.g. a graph library) alongside Postgres.

Rejected. Reintroduces the cross-system transaction problem: a second storage
manager and a second log means 2PC or consistency glue, killing SM-5 — the exact
cost the keystone exists to delete. Also reintroduces a second operator
vocabulary, so plan fusion across stores becomes cross-engine glue rather than
iterator composition.

### D. Build a tri-modal engine from scratch (no fork).

Rejected. We would re-implement a Volcano executor, an MVCC transaction manager,
a WAL, a B-tree, and an HNSW index — all of which MSVBASE already provides,
correct and tested. The novel work (graph access method + fusion planner) is a
fraction of that; rebuilding the borrowed 90% adds years of risk for no edge.
Build-vs-borrow says borrow what exists and is correct.

### E. Start with the graph store, bolt vectors on later.

Rejected on sequencing grounds. MSVBASE already delivers vector + relational +
executor + txn manager as a working substrate; starting there means the *first*
integration milestone is a single new access method against a live executor and
txn manager, not two unproven subsystems at once. Building graph-first would
force us to also build or borrow the vector and executor halves before the first
fused query could run, inflating the critical path.

---

## References

- **AkasicDB** (SIGMOD '26) — tri-modal target system and baseline (Fig 3a:
  out-of-DB Neo4j+Milvus+Postgres merge).
- **Chimera** (PVLDB 18(2)) — native graph access method co-resident with
  relational storage under one transaction manager. Basis for the BUILD half of
  the keystone.
- **VBASE / MSVBASE** (OSDI '23) — HNSW vector index inside a PostgreSQL fork
  with relaxed-monotonicity early termination in the Volcano executor. The fork
  point and the BORROW half of the keystone.
- `PROJECT.md` — canonical query, non-negotiable invariants (TR-1, single
  process, native graph access method, 32KB pages, three-store closure), success
  metrics SM-1..SM-5.

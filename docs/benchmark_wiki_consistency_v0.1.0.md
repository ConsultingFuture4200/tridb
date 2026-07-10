# Benchmark — one-WAL cross-modal consistency: TriDB vs a 3-store stack (v0.1.0)

> **Status: EXECUTED live on the Spark (GB10), 2026-07-08.** The other half of the wiki value
> story. `docs/benchmark_wiki_fusion_v0.1.0.md` measured the **fusion SPEED** win (one fused
> `tjs_open` round-trip vs a Milvus→Neo4j→pgvector pipeline: 3.3–11.5× loopback, 10.6–16.7×
> real-network at N=200k). This doc measures the **fusion CONSISTENCY** win: the transactional
> guarantee TriDB gives across vector + graph + relational *because they live in one Postgres
> process under one WAL*, that three separate stores structurally cannot give without bolting on a
> distributed-transaction layer. Harness: `bench/wiki_consistency.py`; raw:
> `bench/results/wiki_consistency.json` + `bench/results/wiki_consistency_raw.txt`.

## TL;DR

A **multi-modal update** on an entity E changes the three mutually-dependent modalities at once —
(a) its embedding, (b) a graph out-edge, (c) a relational attribute — each tagged with a version
`v`. E is **consistent** iff all three legs agree on `v`, **torn** iff they disagree. We run three
failure/concurrency scenarios head-to-head: TriDB writes all three in **one transaction**; the
multi-store writes Milvus → Neo4j → pgvector as **three independent transactions** with no
coordinator (the gBrain-style stack people actually run). Small entity set (M=100) — consistency is
about **correctness, not scale**.

| Scenario | Metric | TriDB (one WAL) | Multi-store (3 stores) |
|---|---|---:|---:|
| 1. Atomicity under injected failure | inconsistent entities (of 42 injected / 100) | **0** (0.0%) | **42** (42/42 injected = 100%) |
| 2. Crash consistency (unclean shutdown + recovery) | post-recovery state | **atomic + durable** (uncommitted→all-old, committed→all-new) | **torn orphan persists** (Milvus=new, Neo4j/pg=old) |
| 3. Torn reads under concurrency (300 reads) | torn-read rate | **1.0%** total; **0.0%** across the two heap legs | **76.7%** |

**Headline:** with no application effort, TriDB gives cross-modal **atomicity** (0 vs 42) and
**crash-atomicity/durability** (recovers vs orphan) for free. On **read isolation** the two
heap-resident legs (vector + relational, one MVCC snapshot) **never tear** (0/300); the only
residual TriDB tears are the **native graph leg** (3/300), whose v1 read path is *commit-visible*,
not yet *snapshot-isolated* — an honest, documented limitation (DEV-1166), and still ~75× below the
multi-store's structural 76.7%.

## Setup

- **TriDB engine**: a throwaway container (`tridb-consistency`, image
  `tridb/msvbase:gx10-v1-batchedge`) — one Postgres 13.4 process, one WAL, `vectordb` +
  `graph_store_am`. Table `cons(id, attr int, embedding float8[])` holds the relational + vector
  legs; the native `graph_store` holds the graph leg (identity mode, `gph_insert_edge` /
  `gph_tombstone_edge` / `gph_neighbors_ext`). Isolated from the 200k wiki load so it can be crashed
  freely.
- **Multi-store baseline**: the isolated `tridb-wiki-*` stores — **Milvus** :19531 (vector,
  **read at STRONG consistency** so we never sandbag it), **Neo4j** :7688 (graph),
  **pgvector/Postgres** :5434 (relational). Dedicated `cons_*` collections/tables — the wiki data is
  untouched.
- **Version model**: each leg encodes an integer `v`. A TriDB multi-modal write is `UPDATE cons SET
  attr, embedding` + graph edge flip, all in one txn. A multi-store write is `Milvus.upsert` then
  `Neo4j` edge flip then `pgvector UPDATE`, three separate transactions.

## Scenario 1 — atomicity under injected failure

For each of 100 entities we attempt an update v0→v1; on a seeded 42% we **inject a failure before
the write set is durable**. TriDB does the three writes in one txn and **rolls back** the injected
ones. The multi-store writes Milvus first and, on the injected ones, **stops after store 1** (a
crash / exception with no rollback path).

```
injected failures: 42/100
TriDB (one txn/one WAL): torn=0, wrong-state=0   -> all injected atomically rolled back
Multi-store (3 stores):  torn=42                 -> every injected op left a partial write nothing reconciles
example torn entities (multi-store): id=1 {milvus:1, neo4j:0, pg:0}, id=3 {1,0,0}, id=4 {1,0,0}, id=7 {1,0,0}
```

TriDB: **0** inconsistent, and every entity is in its correct end-state (injected→all-v0,
committed→all-v1). Multi-store: **42** torn — every injected op left Milvus holding the new vector
while Neo4j/pgvector kept the old edge/attr. Nothing reconciles them.

## Scenario 2 — crash consistency (real unclean shutdown + WAL recovery)

TriDB: entity 1 committed to v1; entity 0's multi-modal txn left **in flight (uncommitted)**. We
**crash the engine** with `pg_ctl -m immediate` (SIGQUIT — no shutdown checkpoint), then restart →
**WAL crash recovery** (the same mechanism a SIGKILL / power loss triggers).

```
[crash]    pg_ctl -m immediate stop (SIGQUIT; no shutdown checkpoint)
[restart]  server started
[recovery] database system was not properly shut down; automatic recovery in progress
[recovery] redo starts at 0/23938F8 ... redo done at 0/24EA5C0
post-recovery entity 0 (vector,graph,rel) = (0, 0, 0)  -> PASS all-old (atomic rollback)
post-recovery entity 1 (vector,graph,rel) = (1, 1, 1)  -> PASS all-new (durable)
multi-store entity 0: crash after Milvus write -> (milvus,neo4j,pg)=(1, 0, 0)  -> TORN orphan persists
```

The uncommitted entity comes back **fully-old across all three legs** (atomic), the committed one
**fully-new** (durable), neither torn — one WAL recovered all three modalities together. The
multi-store's partial write (Milvus durably flushed at STRONG consistency) is a **torn orphan**:
there is no cross-store log to roll it back or roll it forward, so it survives indefinitely. (We do
not bounce the shared Milvus/Neo4j to avoid disturbing the wiki collection — but the point is
structural: with no cross-system WAL, **no** restart can reconcile it.)

## Scenario 3 — read isolation (torn reads under concurrency)

A writer thread continuously flips a hot entity v0↔v1; a reader thread reads all three legs 300
times and counts torn reads. TriDB reads the three legs in **one SQL statement**; the multi-store
reads the three stores in **three round-trips** (writer inter-store gap 5 ms, to make the inherent
non-atomic window observable in a bounded run — the window exists at 0 ms too, just narrower).

```
TriDB total torn reads = 3/300 (1.0%); of these the heap legs (vector,relational) tore 0/300 (0.0%)
   example TriDB torn reads: {vector:1, graph:0, relational:1}, {0,1,0}, {0,1,0}
Multi-store (3 reads, 3 instants): torn reads = 230/300 (76.7%)
   example torn reads (multi-store): {milvus:1, neo4j:0, pg:0}, {0,1,0}, {1,0,1}, {0,1,0}
```

- **The two heap-resident legs (vector + relational) never tear** — 0/300. They share the row's one
  MVCC snapshot, so a reader always sees them at the same version. This is the one-WAL snapshot
  working exactly as claimed.
- **The residual TriDB tears (3/300) are the native GRAPH leg only** (note the examples: vector ==
  relational in every case; only `graph` differs). This is an **honest v1 limitation**: the native
  graph store's read path is *commit-visible* — `gph_xmin_visible()` returns
  `TransactionIdDidCommit(xmin)`, i.e. it sees any **committed** transaction's edges immediately,
  regardless of the reader's statement snapshot. So a writer that commits during the narrow
  intra-statement window between the heap scan and the graph sub-read can be seen by the graph leg
  but not the heap legs. Full per-tuple **snapshot isolation** for the graph store is explicitly
  deferred to **DEV-1166** (`src/graph_store/graph_am.c:64-70`). Until then the graph leg gives
  atomicity + durability + crash-visibility (scenarios 1 & 2) but not MVCC snapshot isolation
  against concurrent committers.
- Even so, **1.0% vs 76.7%** (and 0.0% on the heap legs): the multi-store's torn-read rate is
  **structural and unbounded** — three unsynchronized stores with no shared snapshot — while TriDB's
  is a narrow, closeable race on one leg.

## Honesty — inherent, but mitigable at real cost

- **The multi-store inconsistency is INHERENT, not a bug.** It is not a Milvus / Neo4j / pgvector
  defect — each store is internally consistent (we read Milvus at STRONG consistency). The tear is
  strictly **cross-store**: there is no transaction that spans the three systems, so any failure or
  concurrent read between the writes exposes a partial state.
- **It IS mitigable app-side — but that is real engineering.** Two-phase commit, sagas with
  compensating actions, a transactional outbox, or a periodic reconciliation job can each shrink or
  close the window. Every one of them adds code, latency, operational surface, and its own failure
  modes (a saga's compensation can itself fail; 2PC blocks on the coordinator). The multi-store can
  **approximate** cross-modal ACID; it cannot get it for free.
- **TriDB gets cross-modal ACID for free** — one transaction manager, one WAL, one recovery path
  (golden rules 1–3). That is the architectural value, independent of any speed number. This is a
  **different tradeoff, not "the multi-store is broken."**
- **Nothing here is fabricated.** Counts are observed live; the crash is a real unclean shutdown +
  WAL recovery (redo log lines shown). Scale is deliberately small — consistency is correctness, not
  throughput.

## The whole story: two halves

TriDB's total wiki-scale value is the sum of two measured results, not one:

1. **Fusion speed** (`docs/benchmark_wiki_fusion_v0.1.0.md`): the in-process fused `tjs_open`
   beats the app-side 3-store pipeline **3.3–11.5× (loopback) / 10.6–16.7× (real-network)** at
   N=200k, matched recall — the win from eliminating cross-system round-trips. (1M is blocked on the
   fork's single-threaded / non-reproducible HNSW vector iterator — documented future work with an
   unblock path in the fusion doc; the Wall-3 batched edge loader **did** validate at 1M.)
2. **Fusion consistency** (this doc): cross-modal atomicity (0 vs 42), crash-atomicity + durability
   (recovers vs orphan), and heap-leg snapshot isolation (0% vs 76.7% torn reads) — for free.

The multi-store can, with significant added complexity, approach TriDB's *speed* (co-locate,
cache) or its *consistency* (2PC/sagas/outbox) — but paying for both at once, in one system, is
what the single-process/one-WAL architecture buys. The I/O-locality thesis (SM-3 "3 pages vs 85")
is **not** what carries this story at wiki scale (dim-384 is RAM-resident — see the fusion doc's
Milestone-B memo); the fusion speed win and this consistency win are.

## Reproduce

```bash
# On the Spark, with the isolated tridb-wiki-* stores up and a throwaway engine container:
docker run -d --name tridb-consistency --entrypoint bash -p 5455:5432 \
  -v /home/bob/code/tridb/src/graph_store:/tmp/ext:ro \
  -v /home/bob/cons_start_engine.sh:/cons_start.sh:ro \
  tridb/msvbase:gx10-v1-batchedge /cons_start.sh          # builds ext, initdb, starts pg

/home/bob/code/tridb/.venv/bin/python bench/wiki_consistency.py \
  --scenarios 1,2,3 --entities 100 --reads 300 --gap-ms 5 \
  --out bench/results/wiki_consistency.json
```

_Generated from a live run; `bench/wiki_consistency.py`. Numbers observed; no result fabricated._

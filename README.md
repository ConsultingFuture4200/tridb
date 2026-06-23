# TriDB

A single-process database engine that natively executes **vector similarity search,
graph traversal, and relational filtering inside one query plan** — serving "Omni RAG"
retrieval on local hardware (GX10 / DGX Spark, 128 GB).

> Clone of **AkasicDB** (SIGMOD Companion '26, DOI 10.1145/3788853.3801609), built on the
> open design primitives **Chimera** (PVLDB 18(2):279–292) and **VBASE** (OSDI '23,
> `microsoft/MSVBASE`).

## Core thesis

The win is not better individual retrievers — it is collapsing multi-turn, multi-system
retrieval into a single plan that **enforces top-k during execution**, avoiding the
materialize-transfer-prune cycle that bloats intermediate results.

## Keystone move

Fork **MSVBASE** (inherits relational + vector + Volcano executor + one transaction
manager), then build the native adjacency-list **graph store inside the same Postgres
process** so it shares the existing transaction manager — sidestepping cross-system
transactions by construction.

## The three stores (closure set)

| Store      | Primitive        | Backing                      | Index  |
| ---------- | ---------------- | ---------------------------- | ------ |
| Vector     | Similarity (ANN) | MSVBASE segments             | HNSW   |
| Graph      | Traversal        | Adjacency-list access method | B-tree |
| Relational | Filter           | PostgreSQL tables            | B-tree |

Three = three irreducible primitives (similarity / traversal / filter). BM25 seam
architected but closed for v1.

## Execution invariant (TR-1, non-negotiable)

Every operator honors Open/Next/Close + early termination. A single blocking operator
forfeits the entire efficiency thesis.

## Canonical query (single template for v1)

```sql
SELECT chunk
FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
  COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
WHERE timestamp IN :selected_time_range
ORDER BY src_embedding <-> :question_embedding
LIMIT 5;
```

## Status

Kickoff: 2026-06-23. Tracked in Linear project **TriDB**
(`linear.app/staqs/project/tridb-39ec4d2bbc0b`), issues DEV-1160 … DEV-1173.

### Hardware gating

The target hardware is the **GX10 (ARM64 + CUDA, 128 GB)**. The MSVBASE fork build
(DEV-1160) and the native C access-method work (DEV-1164/1165/1166, DEV-1168/1169/1170)
**must run on the GX10** and cannot be compiled on a non-target workstation. This repo
contains the hardware-independent layer that is buildable anywhere — design specs, the
reproducible build script, the seed-corpus tooling, and the benchmark baseline harness —
plus interface-level skeletons for the gated C work.

See `docs/STATUS.md` for the per-issue gated/unblocked breakdown.

## Layout

```
spec/        Versioned spec mirror (source of truth: Linear doc)
docs/        Design specs + ADRs (docs/decisions/)
scripts/     gx10build.sh — reproducible MSVBASE fork build (runs on GX10)
tools/       seed_corpus.py — synthetic Omni-RAG corpus generator
baseline/    Neo4j + Milvus + Postgres multi-system baseline (DEV-1171)
bench/       TriDB benchmark harness + report (DEV-1172/1173)
src/         graph_store/ + planner/ — native store + join-order (gated on GX10 build)
```

## Build & test

See `CLAUDE.md` for the canonical commands. Quick start for the
hardware-independent layer:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
make test          # runs the buildable-anywhere test suite
```

# Baseline — multi-system Omni-RAG (DEV-1171)

The **measurement baseline** TriDB is compared against: AkasicDB Scenario 2,
out-of-DB integration merged at the application layer. Three independent
systems, three transaction managers, results merged in Python.

| Concern | System | Role in the canonical query |
|---|---|---|
| similarity | Milvus (standalone) | ANN top-k on `src_embedding <-> :question_embedding` |
| traversal | Neo4j 5.x | 1-hop `(src:entity)-[:related_to]->(dst:entity)` |
| filter | Postgres 16 | `WHERE timestamp IN :selected_time_range` |
| merge | Python (`harness.py`) | join the three, `ORDER BY` distance, `LIMIT k` |

This stack deliberately keeps the three systems separate and merges app-side.
That separation — and the intermediate-result blowup it forces — is the point
of the comparison. The harness instruments both **end-to-end latency** and the
**size of every intermediate result set**, which is what SM-1 (>=5x
intermediate-result reduction) is measured against.

## 1. Bring up the stack

```bash
cd baseline
docker compose up -d
```

Services and ports:

| Service | Port(s) | Notes |
|---|---|---|
| neo4j | 7474 (HTTP), 7687 (Bolt) | auth `neo4j/testpassword`, APOC enabled |
| milvus | 19530 (gRPC), 9091 (health) | standalone + etcd + minio deps |
| minio | 9000 (S3), 9001 (console) | Milvus object store |
| postgres | 5432 | db `tridb_baseline`, `postgres/postgres` |

Data persists under `baseline/volumes/` (gitignored). Wait for healthchecks:

```bash
docker compose ps          # all should report (healthy)
```

## 2. Generate a seed corpus

The harness loads from a seed dir produced by `tools/seed_corpus.py`:

```bash
# from repo root
python tools/seed_corpus.py --entities 1000 --dim 768 --out data/seed/
```

Produces `entities.csv`, `edges.csv`, `queries.jsonl` (and a `load.sql` the
harness does not use).

## 3. Load the corpus into all three systems

```bash
python baseline/harness.py load --seed-dir data/seed/
```

This populates Neo4j (nodes + `:related_to` edges), Milvus (embedding
collection), and Postgres (`entity` table). One-time setup; not part of the
measured run.

> Skeleton note: the Milvus collection/index/insert path is marked
> `TODO(live)` and needs the running Milvus instance to finalize. Neo4j and
> Postgres load paths are complete.

## 4. Run the benchmark

```bash
python baseline/harness.py run --seed-dir data/seed/ --k 5 --out baseline/baseline_metrics.json
```

Per-query, the harness records latency (total + per-system + merge) and every
intermediate-result size: graph pairs, distinct src/dst, vector candidates,
relational survivors, merged candidates, and final results.

## 5. Where metrics land

The `--out` JSON (default `baseline_metrics.json`):

```json
{
  "baseline": "akasicdb-scenario-2-out-of-db",
  "k": 5,
  "num_queries": 10,
  "queries": [
    {
      "qid": 0,
      "latency_total_ms": 0.0,
      "graph_pairs": 0,
      "vector_candidates": 0,
      "relational_filtered": 0,
      "merged_candidates": 0,
      "final_results": 0,
      "result_chunks": []
    }
  ]
}
```

The TriDB side emits the same per-query intermediate sizes; SM-1 is the ratio
of baseline `merged_candidates` (and friends) to TriDB's fused intermediate
sets.

## Connection params

All clients read env vars with localhost defaults — override to point at a
non-local stack:

| Var | Default |
|---|---|
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `neo4j` / `testpassword` |
| `MILVUS_HOST` / `MILVUS_PORT` / `MILVUS_COLLECTION` | `localhost` / `19530` / `entity_embeddings` |
| `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | `localhost` / `5432` / `postgres` / `postgres` / `tridb_baseline` |

## Teardown

```bash
docker compose down            # keep volumes
docker compose down -v         # also drop named/anon volumes (not ./volumes)
rm -rf volumes/                # wipe persisted data
```

Python deps (`neo4j`, `pymilvus`, `psycopg[binary]`) are declared in the repo
root `requirements.txt`.

# Installing TriDB's graph AM on stock PostgreSQL 16/17

The native adjacency-list graph store (`graph_store_am`) installs on **stock PostgreSQL 16 or
17** as a plain PGXS extension — no forked Postgres, no custom block size (8KB pages work;
32KB remains the high-degree performance target, see ADR-0015 E2). Pair it with
[pgvector](https://github.com/pgvector/pgvector) for the vector leg and you have the tri-modal
substrate the Gate B benchmark measured (`docs/gate_b_spike_v0.1.0.md`: fused filter-first KBQA
23.68× vs a Milvus+Neo4j+Postgres assembly at matched recall, N=1,002,331).

## Option 1 — Docker (fastest)

```bash
docker build -f scripts/pg17/Dockerfile.release -t tridb/postgres-trimodal:pg17 .
docker run -d --name trimodal -e POSTGRES_PASSWORD=secret tridb/postgres-trimodal:pg17
docker exec -it trimodal psql -U postgres \
  -c 'CREATE EXTENSION vector;' \
  -c 'CREATE EXTENSION graph_store_am;'
```

`--build-arg PG_MAJOR=16` selects PostgreSQL 16. If you plan pgvector **parallel** HNSW index
builds at scale, start the container with `--shm-size` ≥ your `maintenance_work_mem` (docker's
64MB `/dev/shm` default fails a parallel 1M-vector build).

## Option 2 — from source (PGXS)

Prerequisites: PostgreSQL 16/17 server headers (`postgresql-server-dev-17` on Debian/Ubuntu),
a C toolchain, and pgvector if you want the vector leg.

```bash
cd src/graph_store
make PG_CONFIG=$(which pg_config)
sudo make PG_CONFIG=$(which pg_config) install
psql -c 'CREATE EXTENSION graph_store_am;'
```

## Verify

```bash
scripts/pg17_graph_test.sh                       # builds + runs the core AM suite in docker
psql -c "SELECT graph_store.gph_upsert_vertex(1);"
```

CI runs the full 11-suite matrix on stock PG 16 and 17 (x86_64) on every push
(`.github/workflows/ci.yml`, job `stock-pg`). ARM64 is validated out-of-band on a DGX Spark
(GB10): the same suites pass on aarch64 stock PG17 — GitHub-hosted ARM runners are not
available on this repository's plan, so ARM is not (yet) in the per-push matrix.

## What this does and does not include

- **Included:** the native graph access method (typed/directional adjacency, `gph_*` SQL
  surface, WAL-logged via GenericXLog, MVCC-visible), on stock PG.
- **Included (D2 phase 2.5, ADR-0019):** `src/tjs_pg` — the fused operator `tjs_open(...)`
  re-homed on stock PG: filter-first behind the operator surface, and vector-first/seedless
  driving pgvector's iterative HNSW scan directly (requires
  `SET hnsw.iterative_scan = relaxed_order`, pgvector ≥ 0.8) with TR-1 early termination and
  honest budget-cap reporting (`tjs_open_budget_capped()`). Exact fork phase/bridge parity
  (ADR-0012/0017 seed-bridge injection) is follow-up; the harness counters expose enough to
  grade recall honestly either way. The **filter-first** Gate A/B headline also works with no
  operator at all: a single SQL statement over `graph_store.gph_traverse_bfs(...)` (see
  `bench/wikidata_h2h.py:emit_tridb_sql`).
- **PGXN:** `src/graph_store/META.json` is prepared; publication happens at release time.

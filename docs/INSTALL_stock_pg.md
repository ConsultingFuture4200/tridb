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

## Harness environment variables

The Wikidata head-to-head harness (`bench/wikidata_h2h.py`, and its sibling `bench/wiki_h2h.py`)
is configured entirely through environment variables. The one that matters most is
**`WD_ENGINE_DIALECT`**: it selects which engine the harness drives. It defaults to `fork` (the
MSVBASE fork) — set `WD_ENGINE_DIALECT=stock` to benchmark the un-forked pgvector engine this
document installs. Forgetting it silently measures the fork.

| Variable | Meaning | Default |
|---|---|---|
| `WD_ENGINE_DIALECT` | Engine to drive: `fork` (MSVBASE) or `stock` (pgvector) | `fork` |
| `WD_SLICE` | Corpus slice directory | `data/wikidata_slice` |
| `WD_EMB` | Dense id-aligned embedding `.npy` | `data/wikidata_slice/emb/dense_id_aligned.npy` |
| `WD_DIM` | Embedding dimensionality | `384` |
| `WD_ENGINE` | TriDB engine container name | `tridb-wikidata` |
| `WD_ENGINE_DB` | TriDB engine database | `postgres` |
| `WD_ENGINE_TABLE` | TriDB engine entity table | `entities` |
| `WD_Q` | Number of queries | `50` |
| `WD_TJS_MAX_EXAMINED` | TR-1 work cap (must match the C default) | `4000` |
| `WD_MILVUS_HOST` / `WD_MILVUS_PORT` / `WD_MILVUS_COLLECTION` | Baseline Milvus target | `localhost` / `19531` / `wikidata_entities` |
| `WD_NEO4J_URI` / `WD_NEO4J_USER` / `WD_NEO4J_PASSWORD` / `WD_NEO4J_LABEL` | Baseline Neo4j target | `bolt://localhost:7688` / `neo4j` / `wikipassword` / `Entity` |
| `WD_PGHOST` / `WD_PGPORT` / `WD_PGDB` / `WD_PGUSER` / `WD_PGPASSWORD` / `WD_PGTABLE` | Baseline Postgres target | `localhost` / `5434` / `tridb_wikidata` / `postgres` / `postgres` / `wd_entity` |

### Publication-gate keys

The honesty gate refuses a headline ratio until the ground-truth graph size and HNSW build
health are declared. These keys accept **either** the `WH_` or the `WD_` prefix (plan 065) — the
harness reads `WH_` first, then falls back to `WD_`:

| Variable | Meaning | Default |
|---|---|---|
| `WH_ENGINE_EDGES` / `WD_ENGINE_EDGES` | Edges the engine graph actually holds | *(undeclared → gate blocks)* |
| `WH_NEO4J_EDGES` / `WD_NEO4J_EDGES` | Edges Neo4j holds | oracle's induced edge count |
| `WH_HNSW_HEALTHY_BUILDS` / `WD_HNSW_HEALTHY_BUILDS` | HNSW builds that came out healthy | *(undeclared → gate blocks)* |
| `WH_HNSW_TOTAL_BUILDS` / `WD_HNSW_TOTAL_BUILDS` | HNSW builds attempted | *(undeclared → gate blocks)* |
| `WH_BOUNDARY_PARITY` | Set `1` to acknowledge timer-boundary parity was equalized | *(unset → gate blocks)* |

## What this does and does not include

- **Included:** the native graph access method (typed/directional adjacency, `gph_*` SQL
  surface, WAL-logged via GenericXLog, MVCC-visible), on stock PG.
- **Included (D2 phase 2.5, ADR-0019):** `src/tjs_pg` — the fused operator `tjs_open(...)`
  re-homed on stock PG: filter-first behind the operator surface, and vector-first/seedless
  driving pgvector's iterative HNSW scan directly (requires
  `SET hnsw.iterative_scan = relaxed_order`, pgvector ≥ 0.8) with TR-1 early termination and
  honest budget-cap reporting (`tjs_open_budget_capped()`). Fork phase/bridge parity **landed**
  (commit `81b8023`, ADR-0012 recipe B): `tjs_open` now performs the guaranteed reachability-bridge
  injection past the vector frontier, with the `tjs_open_bridges_injected()` counter exposing how
  many bridges were forced. The remaining follow-up is the **seedless SM-4 recall-curve parity** vs
  the fork (the pgvector budget-capped recall ceiling of ADR-0015 E3, re-measured per pgvector minor)
  — not the bridge mechanism itself. The **filter-first** Gate A/B headline also works with no
  operator at all: a single SQL statement over `graph_store.gph_traverse_bfs(...)` (see
  `bench/wikidata_h2h.py:emit_tridb_sql`).
- **Parity gate:** `make tjs-parity-test` (`scripts/tjs_parity_test.sh`) diffs the fork's fused
  filter-first statement against the stock `tjs_open` filter-first mode on a shared corpus —
  needs BOTH engine images, so it is a manual / CI-dispatch gate, not a per-PR check.
- **PGXN:** `src/graph_store/META.json` is prepared; publication happens at release time.

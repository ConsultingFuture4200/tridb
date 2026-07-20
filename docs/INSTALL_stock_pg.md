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
  -c 'CREATE EXTENSION graph_store_am;' \
  -c 'CREATE EXTENSION tjs_pg;'
```

The order matters: `tjs_pg` (the fused tri-modal operator) requires both `vector` and
`graph_store_am`.

`--build-arg PG_MAJOR=16` selects PostgreSQL 16. If you plan pgvector **parallel** HNSW index
builds at scale, start the container with `--shm-size` ≥ your `maintenance_work_mem` (docker's
64MB `/dev/shm` default fails a parallel 1M-vector build).

## Option 2 — from source (PGXS)

Prerequisites: PostgreSQL 16/17 server headers (`postgresql-server-dev-17` on Debian/Ubuntu),
a C toolchain, and pgvector if you want the vector leg.

```bash
for ext in src/graph_store src/tjs_pg; do
  ( cd "$ext" && make PG_CONFIG=$(which pg_config) && sudo make PG_CONFIG=$(which pg_config) install )
done
psql -c 'CREATE EXTENSION vector;' \
     -c 'CREATE EXTENSION graph_store_am;' \
     -c 'CREATE EXTENSION tjs_pg;'
```

## Verify

```bash
scripts/pg17_graph_test.sh                       # builds + runs the core AM suite in docker
scripts/pg17_release_smoke.sh                    # starts the release image, runs the tri-modal smoke
psql -c "SELECT graph_store.gph_upsert_vertex(1);"
```

Query through the canonical front door, not the private operator: `graph_store.graph_query($$...$$)`
lowers the one canonical query (SQL/PGQ `GRAPH_TABLE(...)` + pgvector `<->`) to the fused
`tjs_open` operator on stock PG (plan 075/ADR-0019). Minimal end-to-end example (fixture +
query): `test/release_stock_smoke.sql`; full suite: `test/canonical_stock_e2e_test.sql`.

```sql
SELECT * FROM graph_store.graph_query($$
    SELECT chunk
    FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
      COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
    WHERE src.id = 1 AND timestamp IN (100)
    ORDER BY src_embedding <-> '[10,0,0,0]'
    LIMIT 1
$$);
```

CI runs the full 11-suite matrix on stock PG 16 and 17 (x86_64) on every push
(`.github/workflows/ci.yml`, job `stock-pg`). ARM64 is validated out-of-band on a DGX Spark
(GB10): the same suites pass on aarch64 stock PG17 — GitHub-hosted ARM runners are not
available on this repository's plan, so ARM is not (yet) in the per-push matrix.

## Upgrading (versioned extension scripts, plan 100)

Both extensions ship versioned upgrade scripts from 0.2.0 on. An existing install upgrades in
place — install the new build (`make install`, or pull the new release image), then:

```sql
ALTER EXTENSION graph_store_am UPDATE TO '0.2.0';
ALTER EXTENSION tjs_pg UPDATE TO '0.2.0';
```

Install the new shared library **before** the `UPDATE`: the upgrade DDL binds symbols that only
exist in the new `.so`. Data written under 0.1.0 (topology, id map, edge-type dictionary,
`identity_mode`) is untouched by the upgrade — `make stock-upgrade-test`
(`scripts/extension_upgrade_test.sh`, also `PG_MAJOR=16`) is the gate: it installs a genuine
0.1.0 (vendored fixtures in `test/fixtures/upgrade/`), loads the plan-099 tri-modal corpus,
upgrades, and asserts a byte-identical probe plus the 0.2.0-only surface on the pre-existing
data. Convention (see `CONTRIBUTING.md`): released surface changes ship as `--X--Y.sql` upgrade
scripts; in-place base-script edits are only allowed pre-release within a version.

## Concurrency: enforced single writer (plan 100)

**Structural writes serialize per graph; a concurrent writer blocks; readers are
MVCC-consistent.** Every structural-write entry point (`gph_insert_vertex`,
`gph_insert_edge(s)`, `gph_tombstone_*`, `gph_freeze` — and everything built on them:
`gph_upsert_vertex`, `add_edge`, `remove_edge`, the batch loaders) takes a
**transaction-scoped EXCLUSIVE advisory lock** keyed on the `gstore` relation OID before
touching any page. The v1 single-writer contract that used to live in a `graph_am.c` comment
is now enforced:

- A second concurrent writer **blocks** (normal Postgres lock-wait, not an error) until the
  holder's transaction commits or aborts, then proceeds on the committed state. Blocking was
  chosen over erroring so batch loaders and interleaved sessions serialize instead of failing;
  there is deliberately no GUC for it.
- The lock is held to **transaction end** (automatic release on commit, rollback, and error),
  so a multi-statement writing transaction is serialized as a unit.
- **Readers never take the lock**: traversal and counts proceed while a writer holds it, with
  the usual MVCC visibility filtering (an uncommitted write stays invisible).
- The lock is visible in `pg_locks` (`locktype = 'advisory'`, `classid` = `gstore`'s OID,
  `objid = 0`) — equivalent to `pg_advisory_xact_lock(gstore_oid::int, 0)`. Do not use that
  advisory key pair for anything else in the same database.

Gate: `make stock-writer-lock-test` (`scripts/graph_writer_lock_test.sh`) — writer-blocks-writer
(scalar and batch), reader-proceeds-under-held-lock, and exact final counts for interleaved
autocommit writers. Multi-writer concurrency (per-tuple snapshot isolation) remains deferred
(DEV-1166); the v1 posture is *enforced*-single-writer.

## Seedless graph scoring (ADR-0021)

`tjs_open`'s vector-first/seedless path (`src IS NULL`) defaults to **PPR-graded scoring**
(`tjs.graph_scoring = 'ppr'`): a bounded forward-push Personalized PageRank pass fuses vector
similarity with graph reinforcement to rank graph-sourced candidates, instead of the binary
reachability guarantee. Measured to dominate reachability-membership scoring on two
independent-gold recall gates (HotpotQA and a 200k-article/14.68M-edge enwiki hyperlink
corpus — see `docs/decisions/0021-ppr-default-graph-scoring.md` for the full evidence).
Filter-first (`src IS NOT NULL`) is unaffected by this setting — it stays exact membership
semantics.

**Membership escape hatch:** `SET tjs.graph_scoring = 'membership'` restores the ADR-0020
reachability-membership scoring byte-identically. This is the mode the 071 filter-first parity
harness relies on, and the one any fork↔stock seedless differential comparison should use
(ADR-0021 D4 — the stock default now intentionally diverges from the fork's membership-scored
seedless semantics).

**Budget guidance:** `tjs.graph_work_budget` (default `65536`, unchanged) is a **latency knob,
not a recall knob**, at hyperlink-density graph scale — recall stays nearly flat across a 32×
budget range while latency scales with the budget:

| budget | scoring | measured recall@20 (enwiki 200k) | measured latency | censored? |
|---|---|---|---|---|
| 8192 | ppr | 0.120 | ~135 ms | yes |
| 65536 (default) | ppr | ~0.12 (flat) | higher (≈2.3× the 8192 cost) | yes |
| 65536 (default) | membership | 0.081 | ~640 ms | yes |

`PPR @ 8192` dominates `membership @ 65536` on both recall and latency. The default budget is
left at `65536` deliberately (D2): lowering it would change membership-mode behavior too and
make wiki-scale results censored-by-default. Tune `tjs.graph_work_budget` down for
hyperlink-dense corpora if latency matters more than the (flat) recall curve. Every benchmark
headline must report the censor flag (`tjs_open_graph_censored()`) alongside it — spec
Addendum A3, unchanged by this ADR.

New GUCs `tjs.ppr_alpha` (default `0.15`) and `tjs.ppr_rmax` (default `1e-3`) expose the
forward-push teleport probability and residue-drain threshold. They are **unswept research
knobs**: the recall gates above were measured only at these defaults.

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

`tools/wikidata_engine_load.py` publishes `gate_env.WD_ENGINE_EDGES` in its load manifest
**only from engine-observed transcript markers** (plan 079). The manifest's `load_status`
(`emitted` | `complete` | `failed`) and `engine.graph_verified` / `engine.hnsw_healthy`
fields keep the phases distinct: a load that fails after the graph-count assertion still
records the observed edge/vertex counts (the graph really holds them), but its
`load_status` stays `failed` and HNSW health stays unhealthy; an `--emit-sql` run or a
failure before the assertion publishes no engine count at all — the expected host slice
counts stay under `counts` and never stand in for engine observations.

## Backup and restore

**Physical backup covers everything; logical backup needs the plan-099 procedure below.**

The graph store rides the host cluster's single WAL (golden rule 2), so any *physical* backup —
`pg_basebackup`, WAL archiving / PITR, filesystem snapshots of a stopped cluster — captures the
graph pages byte-for-byte along with the relational and vector data. Nothing extra to do.

*Logical* backup is different. `gstore`'s pages are custom-formatted (not heap tuples), so
`pg_dump` can never carry the topology natively — and before plan 099 a dump/restore cycle
**silently lost the entire graph leg** (topology, `gph_vid_map`, `edge_type` extras,
`identity_mode`) in both `-Fc` and plain modes, with rc 0 and no warning, while relational and
vector data round-tripped fine. The restored database *looked* healthy. Since plan 099:

- `graph_store.edge_type` (minus the seeded `related_to` row, which `CREATE EXTENSION`
  re-creates) and `graph_store.gph_vid_map` are `pg_extension_config_dump`-marked, so their
  rows ride `pg_dump` natively;
- the native topology is exported by two SRFs over the existing read paths:
  `gph_dump_vertices()` (all allocated vids, in vid order — allocation-preserving, see caveats)
  and `gph_dump_edges()` (every MVCC-visible, non-tombstoned edge as `(src, dst, type_id)` in
  `(src, type_id, adjacency)` order).

### Procedure

Dump (as the extension owner):

```sql
-- alongside:  pg_dump -Fc -f backup.fc <db>
COPY (SELECT * FROM graph_store.gph_dump_vertices()) TO '/path/vertices.copy';
COPY (SELECT * FROM graph_store.gph_dump_edges())    TO '/path/edges.copy';
-- record whether the identity fast-path was on:
SELECT identity_mode FROM graph_store.gph_am_meta;
```

Restore into a fresh database (`pg_restore -d <newdb> backup.fc` recreates the extensions and
the config-table rows), then replay the topology:

```sql
-- 1. re-materialize the FULL allocated vid range, in order, BEFORE any edge
--    (n = line count of vertices.copy)
SELECT count(graph_store.gph_insert_vertex()) FROM generate_series(1, :n);
-- 2. replay edges grouped by (src, type_id), array order = dump order
--    (the typed batched gph_insert_edges, plan 091)
CREATE TEMP TABLE gph_edge_staging (src bigint, dst bigint, type_id int, ord bigserial);
COPY gph_edge_staging (src, dst, type_id) FROM '/path/edges.copy';
SELECT count(graph_store.gph_insert_edges(src, dsts, type_id)) FROM (
    SELECT src, type_id, array_agg(dst ORDER BY ord) AS dsts
    FROM gph_edge_staging GROUP BY src, type_id ORDER BY src, type_id
) g;
-- 3. only if the source had it on (the DEV-1352 guard re-verifies the restored map):
SELECT graph_store.gph_set_identity_mode(true);
```

### What is NOT preserved (a logical dump is a logical snapshot)

- **Tombstone history and frozen-xid state.** Tombstoned edges are simply absent from the
  dump; tombstoned or aborted-insert vertices restore as live-but-isolated placeholder vids
  (their edges were already invisible, so *visible topology and traversals are identical*, but
  `gph_vertex_count()` may read higher post-restore and `gph_freeze` state starts fresh).
- **The raw `gph_edge_count()` insert counter.** It restarts at the replayed edge count;
  `gph_visible_edge_count()` is the preserved quantity.
- **Any-type emission order on type-interleaved sources.** Replay groups a source's edges by
  type, so `gph_traverse_typed(v, t, ...)` output is byte-identical *per type*, while the
  type-0 (any) emission *order* may differ (same edge set). `gph_am_meta` does not ride the
  dump (its seeded row would PK-conflict); step 3 above re-derives it.

### The gate

`make stock-dump-restore-test` (`scripts/graph_dump_restore_test.sh`, also `PG_MAJOR=16`) runs
the full round trip in one container — corpus load, dump, restore into a fresh database,
byte-equality diff of a tri-modal probe (per-type traversals, counts, id map, dictionary,
vector top-k, `tjs_open` ids), plus a corrupted-dump negative control that must fail the diff.
It needs two databases in one container, so it is deliberately **not** in the per-PR `stock-pg`
CI job's `STOCK_TESTS` list (keeps per-PR runtime flat); run it locally before release cuts or
wire it as a CI dispatch job alongside `tjs-parity-test`.

## What this does and does not include

- **Included:** the native graph access method (typed/directional adjacency, `gph_*` SQL
  surface, WAL-logged via GenericXLog, MVCC-visible), on stock PG.
- **Included (D2 phase 2.5, ADR-0019):** `src/tjs_pg` — the fused operator `tjs_open(...)`
  re-homed on stock PG: filter-first behind the operator surface, and vector-first/seedless
  driving pgvector's iterative HNSW scan directly (requires
  `SET hnsw.iterative_scan = relaxed_order`, pgvector ≥ 0.8) with TR-1 early termination and
  honest, *censored* termination reporting (ADR-0019 addendum 2026-07-16):
  `tjs_open_termination_reason()` returns `filter_first` | `term_cond` |
  `stream_end_unknown` — pgvector does not disclose whether `hnsw.max_scan_tuples` or
  natural index exhaustion ended its stream, so an ended stream is reported as *unknown*,
  never as a definite budget cap. The compat boolean `tjs_open_budget_capped()` is `false`
  for known non-budget endings and SQL `NULL` for unknown ones (never `true` today);
  measurement scripts must treat `NULL` as possibly-capped, not count it as either side.
  `tjs_open_candidates_examined()` on the filter-first path reports the full qualifying-row
  count before the top-k `LIMIT` (not `min(work, k)`). Fork phase/bridge parity **landed**
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

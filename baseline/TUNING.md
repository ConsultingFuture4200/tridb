# Baseline Tuning — committed configs (GTM "beat it" invitation)

**TL;DR.** The multi-store baseline (Milvus + Neo4j + Postgres, merged app-side —
see `baseline/README.md`) is tuned, and every tuning parameter is committed in
this repo so the comparison is not a strawman. The numbers live in code
(`baseline/sm2.py`), not in a private spreadsheet. **If you can tune it faster,
the configs are right here — beat it, and send the diff.** This is the explicit
counter to the "strawman baseline" attack in `docs/gtm_opensource_v0.1.0.md`.

## Why this exists

A public benchmark is only credible if the thing you beat was configured the way
a competent operator would actually run it. The GTM plan names a *tuned* real
multi-store baseline (configs committed, "invite others to beat it") as a launch
blocker. This file is the single, reviewable place those choices are recorded and
justified, so a hostile reader can audit — and improve — them.

## Committed tuned parameters

All of these are constants in `baseline/sm2.py` (the live SM-2 driver). Changing a
value here means changing it there; there is no hidden config.

| Parameter | Value | Where | Why this value |
|---|---|---|---|
| Milvus index | `IVF_FLAT`, `nlist=128` (default; `BASELINE_NLIST` env) | `sm2.py: MILVUS_INDEX` | IVF_FLAT is exact-within-probed-lists (no quantization recall loss), so the baseline's vector recall is not handicapped vs TriDB's HNSW. `nlist=128` is the standard rule-of-thumb (~4·√N for the 20k–100k slice) for IVF cluster count. At other scales `nlist` MUST follow the same rule via `BASELINE_NLIST` (1M → `nlist=4096`) — leaving it at 128 at 1M would force each probe to scan ~0.8% of the whole corpus per list and strawman the baseline. |
| Milvus search | `nprobe=64` (default; `BASELINE_NPROBE` env) | `sm2.py: MILVUS_SEARCH_PARAM` | `nprobe=64` of `nlist=128` probes half the lists — a deliberately HIGH-recall operating point so the baseline is not losing the comparison on missed neighbours. Lowering it would make the baseline faster but lower-recall; we chose recall (a faster wrong answer is worth nothing — GTM metric). At 1M/`nlist=4096` the same high-recall stance is `BASELINE_NPROBE=128` (3% of lists — above typical tuned deployments). The env used for any published run is stamped into the run's report. |
| ANN over-fetch | `k * 32` (default; `BASELINE_ANN_FANOUT` env) | `sm2.py: BASELINE_ANN_FANOUT` | The baseline CANNOT push the graph/time predicates into the ANN scan, so it over-fetches `k*32` candidates and prunes app-side. This is the intrinsic multi-store penalty (the SM-1 intermediate blowup), not a config we crippled — it is set generously (32×) so the baseline rarely under-fetches and misses a qualifying dst **at the 2k–100k slice**. At 1M with a selective graph predicate the qualifying density collapses (e.g. ~0.12% joint selectivity → E[qualifying in top-160] ≈ 0.2) and `k*32` structurally under-returns (<k answers, measured 2026-07-02). A correct-answer 1M operating point needs the fetch scaled to the joint selectivity (`k*2000` fills k with E≈12 qualifying at 0.12%); the fetch used for any published run is stamped into the run's report. This scaling cost IS the multi-store penalty the comparison exists to measure. |
| Neo4j | uniqueness constraint on `:entity(id)` | `sm2.py: load_neo4j` | A `CREATE CONSTRAINT ... REQUIRE e.id IS UNIQUE` gives Neo4j a backing index, so the 1-hop `(src)-[:related_to]->(dst)` expansion is an indexed lookup, not a scan. |
| Postgres | B-tree index on `entity(timestamp)` | `sm2.py: load_postgres` | The relational leg filters on the timestamp window; `entity_ts_idx` keeps that an index range scan. |
| Measurement | warm conns, 1 warm-up discarded, **median** of N runs, load+index EXCLUDED | `sm2.py: run_query` | Identical methodology to the TriDB side (warm `psql`, median of N `\timing` runs, load/index out of the timed path) so SM-2 is like-for-like. |

### Public-dataset note (real embeddings)

The public-dataset benchmark (`make bench-public`,
`docs/benchmark_public_v0.1.0.md`) runs the SAME canonical query over REAL public
embeddings (default `gist-960-euclidean`, dim 960, L2). When the SM-2 head-to-head
is run on that corpus, IVF_FLAT `nlist` should scale with the chosen row count
(`PUBLIC_LIMIT`): the `~4·√N` rule gives `nlist≈400` at the 100k headline slice
(vs 128 for the ~20k smoke). Keep `metric_type="L2"` — the public default set is
Euclidean, matching the canonical `<->` ordering and the engine
`distmethod=l2_distance`. An *angular* set would require switching both the index
and the oracle to cosine; we pin an L2 set precisely to avoid that mismatch.

## Beat it

These are honest, defensible defaults — not optimal for every scale. If you have a
better config (a tuned HNSW on the Milvus side, a different `nprobe`/`nlist`, a
graph-first plan that shrinks the over-fetch, a single-store contender), change
the constants in `baseline/sm2.py`, re-run `make sm2` (needs `make baseline-up` +
the engine image), and open a PR with the diff and your numbers. The whole point
of committing the config is that the comparison stays reproducible and contestable.

## Not claimed here

This file documents the tuned baseline *configuration*. It does NOT report any
latency or recall number — those come from a LIVE run (`make sm2` for the SM-2
head-to-head, `make bench-public` for the public-dataset recall), which is
GX10-/stack-gated. No benchmark result is asserted in this document.

## Client/server version alignment (plan 030)

The baseline stack pins **Milvus `v2.4.5`** (`baseline/docker-compose.yml`), **Neo4j 5.20**, and
**Postgres 16**. The Python clients are pinned in `requirements.lock` (`pymilvus`, `neo4j`,
`psycopg`). `pymilvus` minor tracks the Milvus server line — `requirements.txt` floors it
`>=2.4,<2.7` so a future 2.7 major cannot silently drive the 2.4 server. If a published SM-2 run
uses a different Milvus image, bump the client and this note together. The versions used for any
published run are stamped into that run's report JSON (`baseline_index_config` / methodology block).

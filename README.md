<!-- Banner / logo. TODO: add ./assets/banner-{light,dark}.svg and uncomment.
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/banner-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="./assets/banner-light.svg">
    <img alt="TriDB" src="BANNER_LIGHT_URL" width="640">
  </picture>
</p>
-->

<h1 align="center">TriDB</h1>

<p align="center">
  <strong>Collapse your RAG stack into one Postgres: vector search, graph traversal, and relational filtering in a single query plan — three extensions on stock PostgreSQL 16/17.</strong>
</p>

<p align="center">
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="License"></a>
  <a href="https://github.com/ConsultingFuture4200/tridb/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ConsultingFuture4200/tridb/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="#benchmarks"><img src="https://img.shields.io/badge/SM--2-1M%20filter--first%20%C2%B7%20recall%201.0-brightgreen?style=flat-square" alt="SM-2"></a>
  <a href="spec/tridb_spec_v0.1.0.md"><img src="https://img.shields.io/badge/spec-v0.1.0-informational?style=flat-square" alt="Spec"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/PostgreSQL_16%2F17_(stock)_%2B_13.4_fork-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/C-A8B9CC?style=for-the-badge&logo=c&logoColor=black" alt="C">
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/CUDA_·_ARM64-76B900?style=for-the-badge&logo=nvidia&logoColor=white" alt="CUDA / ARM64">
</p>

---

**Current release: v0.2.0** — [release notes](docs/releases/v0.2.0.md) · [install guide](docs/INSTALL_stock_pg.md) · extensions install on **stock PostgreSQL 16/17 + pgvector**, no fork.

## Try It

One command to a running tri-modal Postgres (stock PG + pgvector + the TriDB extensions, prebaked):

```bash
docker build -f scripts/pg17/Dockerfile.release -t tridb/postgres-trimodal:pg17 .
docker run -d --name trimodal -e POSTGRES_PASSWORD=secret tridb/postgres-trimodal:pg17
docker exec -it trimodal psql -U postgres \
  -c 'CREATE EXTENSION vector;' \
  -c 'CREATE EXTENSION graph_store_am;' \
  -c 'CREATE EXTENSION tjs_pg;'
```

Or use it as **agent memory over MCP** (store / connect / recall through the fused operator — [docs](docs/mcp_agent_memory_v0.1.0.md)):

```bash
pip install -r requirements-mcp.txt
make mcp-demo        # container up -> store/connect/recall over real stdio JSON-RPC -> teardown
```

### Proven, at what scale

| Claim | Scale it was measured at | Evidence |
|---|---|---|
| Fused filter-first query **23.68×** faster than Milvus+Neo4j+Postgres at matched recall (0.992/0.986), on stock PG 17 | 1,002,331-entity Wikidata slice | [`docs/gate_b_spike_v0.1.0.md`](docs/gate_b_spike_v0.1.0.md) |
| Fused seedless retrieval **3.3–16.7×** faster than the app-side multi-store pipeline at matched recall@10 | 200k Wikipedia articles, 14.68M hyperlink edges | [`docs/benchmark_wiki_fusion_v0.1.0.md`](docs/benchmark_wiki_fusion_v0.1.0.md) |
| One-WAL consistency: **0** torn cross-store writes under injected failure (baseline 42/42); torn reads 1.0% vs 76.7% | live crash/failure harness, same corpus | [`docs/benchmark_wiki_consistency_v0.1.0.md`](docs/benchmark_wiki_consistency_v0.1.0.md) |
| PPR-graded seedless default beats reachability scoring: **+47% rel** link-pred recall@20; **+4.7 pt** HotpotQA recall@5 | 200k/14.68M-edge enwiki + HotpotQA | [ADR-0021](docs/decisions/0021-ppr-default-graph-scoring.md) |
| Graph-bridge injection lifts multi-hop joint evidence recall@5 by **+15.6 pt** over vector-only (reproducible host-side, pinned data) | HotpotQA dev slice | [`docs/benchmark_public_repro_v0.1.0.md`](docs/benchmark_public_repro_v0.1.0.md) |
| **Honest tie we published anyway:** plain SQL in the *same* Postgres (pgvector + a tuned `links` CTE) comes within 16 µs of the fused operator at the anchored query class — the big win is one-system-vs-three; at the pure filtered-ANN class plain pgvector still leads on median (1.3–1.6× after two fix rounds; tails now bounded below pgvector's — [#32](https://github.com/ConsultingFuture4200/tridb/issues/32)) | same 1M Wikidata slice, same DB, same session | [`docs/benchmark_allpg_baseline_v0.1.0.md`](docs/benchmark_allpg_baseline_v0.1.0.md) |

**Not yet measured:** the 128 GB headline benchmark (GX10-gated) and the 1M seedless head-to-head (blocked; documented in the fusion doc). Honest-limits list: [release notes](docs/releases/v0.2.0.md#honest-limits--read-before-benchmarking).

<details>
<summary>Table of Contents</summary>

- [Try It](#try-it)
- [About](#about)
- [Features](#features)
- [Architecture](#architecture)
- [Benchmarks](#benchmarks)
- [The Canonical Query](#the-canonical-query)
- [Quick Start](#quick-start)
- [Repository Layout](#repository-layout)
- [Status](#status)
- [License](#license)

</details>

## About

"Omni-RAG" retrieval needs three things at once: **similarity** (which chunks are relevant?), **traversal** (what's connected to them?), and **filtering** (which are in scope?). The usual answer stitches three systems together (a vector DB, a graph DB, and a relational DB) and merges results in application code. That **materialize-transfer-prune** cycle ships large intermediate sets across process boundaries on every turn.

TriDB collapses all three into **one query plan, in one PostgreSQL process, under one transaction manager**. The win isn't better individual retrievers. It's enforcing the global top-k *during* execution so intermediate results never blow up. It is a clean-room implementation of **AkasicDB** (SIGMOD Companion '26), built by forking **MSVBASE** (VBASE, OSDI '23) and adding a native graph store, extending **Chimera**'s (PVLDB 18(2)) dual-store design to a triple store.

**Why this exists vs. AkasicDB:** AkasicDB is the design TriDB descends from; TriDB is an **open, Postgres-native, locally-runnable** realization of it: it runs on a single DGX Spark, the whole stack is reproducible from this repo, and it leans on the pgvector/Postgres ecosystem rather than a closed system. The peer-reviewed lineage (VBASE / AkasicDB / Chimera) is the credibility anchor; the open + local + reproducible angle is the contribution.

**Installable on stock Postgres (D2 un-fork):** the native graph store (`graph_store_am`) and the fused `tjs_open` operator (`src/tjs_pg`) now install as **plain extensions on stock PostgreSQL 16/17 + pgvector** — no forked Postgres required (see [`docs/INSTALL_stock_pg.md`](docs/INSTALL_stock_pg.md), ADR-0019). The MSVBASE fork (PG 13.4) remains the **reference vehicle**: it holds the relaxed-monotonicity executor mechanism the seedless SM-4 recall curve is measured against, but it is the launch vehicle, not the destination. The stock-PG filter-first path already **measures a larger fusion win than the fork** (Gate B below).

> [!NOTE]
> **What v1 actually delivers (read before benchmarking):** TriDB wins decisively on **source-anchored
> tri-modal queries** ("given entity X, find vector-similar entities reachable from X, filtered") and on
> **one-WAL transactional consistency across all three stores**, a guarantee a bolt-on
> Milvus+Neo4j+Postgres stack cannot make. The open-domain retriever is now a real engine operator
> (first-cut): the single-source `tjs()` operator ranks vectors only within one source's reachable set,
> while the seedless multi-seed **`tjs_open` operator (ADR-0012) ships as a first-cut**: seedless ANN
> seeding + multi-source graph expansion + bridge injection past the vector frontier (TR-1-preserving),
> at **recall@10 0.980 on real HotpotQA** (vs 0.967 vector-only). It uses reachability-bridge injection +
> VBASE early termination; the PPR-graded + rank-join-fusion refinement (host-validated at 0.987,
> `bench/tjs_open_ref.py`) is the next iteration. The cross-modal join-order heuristic is **live**: the
> filter-first physical body shipped (DEV-1290) and the FR-6 lowering binds the decision to execution
> (DEV-1285), so a selective predicate at scale runs filter-first: at 1M this drops the canonical
> query from ~171 ms (vector-first) to single-digit ms at recall 1.0 (see benchmarks). Lead with the
> source-anchored + consistency wins; the open-retrieval operator is real but first-cut.

## Features

- **Tri-modal in one plan** — vector + graph + relational compose in a single Volcano pipeline via the **TJS** (Traversal-Join-Similarity) operator, with a single global top-k.
- **Native graph store** — topology is a first-class adjacency-list **PostgreSQL access method** (32 KB pages, GenericXLog, crash/abort-durable), *not* relational join tables.
- **One transaction manager, one WAL** — the graph store lives inside the Postgres process, so a single transaction commits/rolls back atomically across all three stores (FR-7). No second WAL, no cross-system transactions.
- **Early termination everywhere (TR-1)** — every operator honors Open/Next/Close and stops as soon as the top-k is settled. No blocking operator is allowed to materialize a full intermediate result.
- **Standard query surface** — the one canonical query is plain SQL/PGQ `GRAPH_TABLE(...)` + pgvector `<->`, lowered to the `tjs()` operator. No new query language.
- **Cross-modal join ordering** — a selectivity heuristic chooses filter-first vs. vector-first to keep the intermediate working set small.

## Architecture

```mermaid
flowchart TB
    Q["Canonical SQL/PGQ query<br/>GRAPH_TABLE ... ORDER BY emb &lt;-&gt; q LIMIT k"] --> TJS

    subgraph PG["Single PostgreSQL process · one transaction manager · one WAL"]
        TJS["TJS operator<br/>(Traversal-Join-Similarity)<br/>single global top-k · early termination"]
        TJS --> V["Vector leg<br/>HNSW ANN<br/>relaxed monotonicity"]
        TJS --> G["Graph leg<br/>native adjacency-list<br/>access method"]
        TJS --> R["Relational leg<br/>B-tree filter"]
    end

    TJS --> K["top-k chunks"]
```

Contrast with the baseline TriDB is measured against, **out-of-DB integration** (AkasicDB Scenario 2): Milvus + Neo4j + Postgres as three separate systems, three transaction managers, results merged in Python. That separation is what forces the intermediate-result blowup and the cross-system round-trips.

## Benchmarks

Head-to-head against the multi-system baseline (Milvus + Neo4j + Postgres, app-side merge) on an **identical corpus and query set** (2000 entities, 12 queries, k=5). Both sides measured like-for-like (warm client wall-clock, median of runs). Run it yourself with `make sm2` and `make bench-live`.

| Metric | Meaning | Target | Result |
|--------|---------|--------|--------|
| **SM-1** | Intermediate-result reduction vs. baseline | ≥ 5× | **1.07× FAIL** (standin; corrected `max(k, reached)` — see [`docs/benchmark_results_v0.1.0.md`](docs/benchmark_results_v0.1.0.md); not restored by GX10) |
| **SM-2** | Lower end-to-end latency than baseline | ≥ 80% of queries | **100% (12/12), median 15.1× (2k/dim-32, x86 standin; re-measure at corrected operating point = DEV-1284, pending)** |
| **SM-3** | Corpus examined (k=5, worst case) | < 25% | **6.4%** |
| **SM-4** | Answer-set parity vs. exact oracle | ≥ 99% | **curve, not a point** (see note ↓) |
| **SM-5** | Transaction atomicity across all stores | 100% | **100%** |

> [!IMPORTANT]
> **SM-4 is a recall/effort curve. Read it honestly.** At the 2k/dim-32 standin scale the qualifying
> rows sit in the top-50, so SM-4 reads 100%; that is *not* the at-scale number. At **100k/dim-768 on
> the GX10 (NEON)** SM-4 trades recall for effort via `term_cond`: **58.5%** exact-parity at the shipped
> default (`term_cond=50`, 3.6% examined) → **97.2%** (`term_cond=5000`) → **100%** (`term_cond=10000`,
> 20.1% examined, still under the 25% TR-1 ceiling). Pin a `term_cond` per reported metric; do **not**
> mix the default-`term_cond` latency number with the high-`term_cond` recall number. (SM-2's "100%"
> means 100% of queries had *lower latency*; the recall metric is SM-4.)

> [!NOTE]
> These are measured on an **x86_64 standin** at standin scale (~1–2 ms/query vs. the baseline's ~16–20 ms). The **128 GB headline benchmark** runs only on the GX10 target (ARM64 + CUDA) and is not yet run. Full methodology and caveats: [`docs/benchmark_sm2_v0.1.0.md`](docs/benchmark_sm2_v0.1.0.md) and [`docs/benchmark_results_v0.1.0.md`](docs/benchmark_results_v0.1.0.md).

### Reproduce the benchmark (one command, public data)

One command runs TriDB's retrieval against **recognized public datasets** and grades **recall@k against an exact oracle**: pinned data (SHA256), pinned seeds. The recall headline reproduces on a commodity x86 box (no engine, no GPU): on the **HotpotQA** dev slice, injecting real graph bridges lifts multi-hop **joint** evidence recall@5 by **+15.6 pt** over vector-only. Live `tjs()` latency stays GX10-gated and is never fabricated.

```bash
make fetch-hotpot HOTPOT_Q=150 && make graphrag    # HotpotQA dev slice + BGE-768 graph (network-gated)
make fetch-dataset PUBLIC_DATASET=sift-128-euclidean   # pinned SIFT1M public-ANN set
make bench-repro                                    # grade recall@k vs exact oracle -> JSON + table
```

Full writeup, the tuned "beat it" baseline, and the honest real-vs-gated split: [`docs/benchmark_public_repro_v0.1.0.md`](docs/benchmark_public_repro_v0.1.0.md).

## The Canonical Query

TriDB targets one locked query template for v1, assembled from existing SQL/PGQ + pgvector standards, no new syntax:

```sql
SELECT chunk
FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
  COLUMNS ( src.embedding AS src_embedding,
            dst.chunk     AS chunk,
            dst.timestamp AS timestamp ) )
WHERE timestamp IN :selected_time_range
ORDER BY src_embedding <-> :question_embedding
LIMIT 5;
```

The template is carried verbatim (as text) through the front door `graph_store.graph_query($$...$$)`, which lowers it to a single fused-operator call that drives all three legs with one global top-k: the fork's `tjs()` where installed, or `tjs_open()` (extension `tjs_pg`) on stock PostgreSQL 16/17. Off-template text is rejected — one canonical query for v1.

```sql
SELECT * FROM graph_store.graph_query($$
    SELECT chunk
    FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
      COLUMNS ( src.embedding AS src_embedding, dst.chunk AS chunk, dst.timestamp AS timestamp ) )
    WHERE src.id = 1 AND timestamp IN (100)
    ORDER BY src_embedding <-> '[19,0,0,0,0,0,0,0]'
    LIMIT 5
$$);
```

## Quick Start

### Install on stock PostgreSQL 16/17 (recommended)

The graph store and the fused operator install as plain extensions on **stock PostgreSQL 16 or 17 + pgvector** — no fork. Full guide: [`docs/INSTALL_stock_pg.md`](docs/INSTALL_stock_pg.md).

```bash
docker build -f scripts/pg17/Dockerfile.release -t tridb/postgres-trimodal:pg17 .
docker run -d --name trimodal -e POSTGRES_PASSWORD=secret tridb/postgres-trimodal:pg17
docker exec -it trimodal psql -U postgres \
  -c 'CREATE EXTENSION vector;' \
  -c 'CREATE EXTENSION graph_store_am;' \
  -c 'CREATE EXTENSION tjs_pg;'
```

### Build the dev/engine layers (advanced)

> [!IMPORTANT]
> The **128 GB headline benchmark** and the **ARM64 fork build sign-off** target the **GX10 (ARM64 + CUDA, 128 GB)**. The MSVBASE fork also builds and runs on an x86_64 standin via Docker for development; the graph AM additionally builds on stock PG 16/17 off-GX10 (`scripts/pg17_graph_test.sh`).

The repository has two layers. The hardware-independent layer (design, tooling, harnesses, Python tests) runs anywhere:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock   # pinned, reproducible; requirements.txt holds floors only
# pip install -r requirements-vdbb.txt   # optional: only for the VectorDBBench adapter (bench/vdbb_tridb.py)
cp .env.example .env               # documents every env var the tooling reads
make test          # Python + lint layer — fast, no Docker
make lint
```

The engine layer needs the forked-MSVBASE image (`tridb/msvbase:dev`):

```bash
scripts/x86build.sh --docker   # build the fork image (x86_64 standin)
make test-all                  # test + lint + smoke + graph engine suites
make bench-live                # live SM-1/SM-3/SM-4/SM-5 on the real engine

make baseline-up               # stand up Milvus + Neo4j + Postgres baseline
make sm2                       # fair SM-2 latency head-to-head (needs PGPORT=5433 where baseline PG maps to 5433)
make baseline-down
```

On the GX10 target:

```bash
scripts/gx10build.sh           # ARM64 + CUDA build of the MSVBASE fork
```

## Repository Layout

```text
spec/        Versioned spec mirror (source of truth: Linear doc TriDB)
docs/        Design specs, ADRs (docs/decisions/), benchmark results
scripts/     Build scripts (x86build.sh, gx10build.sh) + patch layer
src/         graph_store/ (native access method) + planner/ (join order)
tools/       Synthetic Omni-RAG corpus generators
baseline/    Milvus + Neo4j + Postgres multi-system baseline (DEV-1171)
bench/       TriDB benchmark harness + reports (DEV-1172/1173)
test/        Engine SQL suites (graph, tri-modal, canonical, FR-7)
tests/       Python unit tests (harness, planner, corpus)
```

## Status

Active development, tracked in Linear project **TriDB**. The **v1 tri-modal core** (native graph store, single-source TJS operator, SQL/PGQ surface, HNSW vector durability, one-WAL atomicity) is built and the **GX10 ARM64 build + engine suite are signed off** (the fork builds and the full suite passes on the DGX Spark; the first at-scale run found and fixed a TJS early-termination scale defect; see the SM-4 curve above). **Honestly scoped:** the seedless `tjs_open` multi-seed operator (ADR-0012, the open-GraphRAG retriever) now **ships as a first-cut engine operator**: recall@10 0.980 on real HotpotQA (beating vector-only 0.967) via reachability-bridge injection + VBASE early termination; the PPR-graded + rank-join-fusion refinement (host-validated at 0.987) is the next iteration. The cross-modal join-order heuristic is now **live**: the filter-first physical body shipped (DEV-1290) and the FR-6 lowering binds it to execution (DEV-1285); the **128 GB headline benchmark** and the honest SM-2 re-measurement at the corrected operating point (DEV-1284) are pending.

**D2 un-fork (landed):** the graph AM and the fused `tjs_open` operator (`src/tjs_pg`, ADR-0019) now install on **stock PostgreSQL 16/17 + pgvector**, exercised off-GX10 by the always-on `stock-pg` CI matrix (PG 16 + 17, x86_64). The fusion win reproduces off the fork: **Gate A PASS** — 11.90× on the PG 13.4 fork — and **Gate B PASS** — **23.68×** for the filter-first fused query on stock PG 17 + pgvector at matched recall (0.992/0.986), both on the same pinned 1M slice. See [`docs/tridb_productization_roadmap_v0.1.0.md`](docs/tridb_productization_roadmap_v0.1.0.md) (Addenda A1–A3) for the strategy, [`docs/STATUS.md`](docs/STATUS.md) for the per-issue breakdown, and [`advisor-plans/`](advisor-plans/) for scoped improvement plans.

## License

[MIT](LICENSE), consistent with the upstream [`microsoft/MSVBASE`](https://github.com/microsoft/MSVBASE) base, whose derived portions remain under Microsoft's MIT copyright.

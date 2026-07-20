# One Postgres instead of three: TriDB, benchmarked honestly

**Status: DRAFT v0.1.0 (2026-07-20) — launch writeup for HN / blog. Maintainer voice-pass pending.**

> **TL;DR.** RAG and agent-memory stacks that need vector search, graph traversal, and
> relational filtering usually run three databases and merge results in application code.
> We built TriDB: the three retrieval modes as **three extensions on stock PostgreSQL 16/17**
> (pgvector + a native graph access method + a fused query operator), executing in one query
> plan under one transaction manager. Against a tuned Milvus + Neo4j + Postgres stack it is
> **~24–60× faster at matched recall** and survives the failure injections that tear the
> multi-store apart. Against **plain SQL in one Postgres** — the benchmark most projects
> wouldn't publish — it wins by only ~16 µs at the anchored query class, and at one query
> class it currently *loses* to plain pgvector. We think the honest version of this story
> is more useful than the headline version, so here is all of it, with a one-command repro
> for every number.

## The problem

A retrieval query for GraphRAG or agent memory looks like this: *find things similar to
this embedding, reachable from this entity in the knowledge graph, that satisfy this
predicate.* The standard architecture answers it with three systems — a vector DB, a graph
DB, and a relational DB — glued together in Python. That has two structural costs:

1. **Round-trips and over-fetching.** Each system computes its leg blind to the others, so
   the application over-fetches from each and intersects. The intermediate results are the
   product, not the answer.
2. **No shared transaction.** Three commit points. Insert a memory (text + embedding +
   graph edge) and crash halfway: nothing reconciles. We measured what that does in
   practice (below) — it is not hypothetical.

## The lineage (why this isn't a from-scratch idea)

TriDB descends from peer-reviewed work: **VBASE** (OSDI '23) showed a vector index scan can
be a *streaming* operator with relaxed monotonicity and early termination, instead of a
retrieve-k-then-filter black box. **Chimera** (PVLDB 18(2)) fused two stores in one engine.
**AkasicDB** (SIGMOD Companion '26) extended it to vector + graph + relational in one
process. TriDB is an open, clean-room realization of that design on PostgreSQL: we forked
Microsoft's MSVBASE (the VBASE research codebase) to prove the mechanism, built a native
graph store inside the same Postgres process, and then — this is the part we didn't
predict — **un-forked it**.

## The un-fork, and the number that surprised us

The research fork (Postgres 13.4, 32 KB pages) proved the mechanism: a fused
filter-first query at 0.27 ms vs 3.16 ms for the tuned multi-store — **11.9× at matched
recall** on a pinned 1M-entity Wikidata slice.

Then we re-homed the whole engine as plain extensions on **stock PostgreSQL 17 +
pgvector** — no fork anywhere in the query path — and re-ran the identical gate:

| Platform | TriDB (fused) | multi-store | speedup |
|---|---:|---:|---:|
| PG 13.4 fork, 32 KB pages | 0.27 ms @ recall 0.992 | 3.16 ms @ 0.986 | 11.90× |
| **Stock PG 17 + pgvector, 8 KB pages** | **0.14 ms @ 0.992** | 3.34 ms @ 0.986 | **23.68×** |

The win *doubled* off the fork ([evidence](gate_b_spike_v0.1.0.md)). Same computation,
identical recall and graph work — a decade-newer executor. The fork is now a reference
vehicle; the thing you install is `CREATE EXTENSION`, three times, on the Postgres you
already run.

At wiki scale (200k articles, 14.7M real hyperlink edges) the same fused-vs-multi-store
comparison over a real network boundary: **16.7× (hop-1) / 10.6× (hop-2)** at matched
recall@10 ([evidence](benchmark_wiki_fusion_v0.1.0.md)). The win is eliminating
cross-system round-trips; it is largest on cheap queries and shrinks with query depth. We
say so because that's what the data says.

## The benchmark most projects wouldn't run

Once TriDB became "three extensions on stock Postgres," the obvious attack became: *why
not plain pgvector + a relational `links` table + a recursive CTE, in one Postgres, with
no TriDB extensions at all?*

We built the strongest version of that competitor we could — covering index, index-only
scans, planner-optimized recursive CTE, verified byte-identical result sets — and ran all
three in the **same database, same backend, same session**
([evidence](benchmark_allpg_baseline_v0.1.0.md)):

| Contender (same DB, 1M entities, 7.4M edges) | recall@10 | median |
|---|---:|---:|
| TriDB fused (native graph AM → filter → vector rank) | 0.986 | **0.049 ms** |
| Plain SQL (recursive CTE over `links` → filter → vector rank) | 0.986 | 0.065 ms |
| Multi-store (Milvus + Neo4j + Postgres, app-side merge) | 0.986 | 3.34 ms |

Two honest conclusions:

1. **The enemy is the three-system stack, not Postgres.** Most of the 24× is
   *single-system vs three systems*, and plain SQL in one Postgres captures nearly all of
   it at this query class. If this table convinces you to collapse your stack into one
   Postgres and skip TriDB's extensions — that is a win for the thesis, and you should.
2. **The native graph store's 16 µs edge is real but not the argument** at shallow,
   anchored queries. The arguments for it are elsewhere: no `links`-mirror to dual-write
   and keep consistent (the graph *is* the store, in the same WAL); page locality at hub
   scale (a 2,000-edge hub expansion reads **2 adjacency pages vs ~2,000 index entries**
   [evidence](benchmark_gbrain_graph_v0.1.0.md)); and one operator call instead of a
   bespoke CTE per query shape.

And one finding that currently cuts against us: at the **pure filtered-ANN** (seedless,
no graph leg) query class, plain pgvector's iterative scan beats our `tjs_open` operator.
When we first measured it the gap was ugly — worse medians and 3–4× worse tails. Two fix
rounds later (a disclosed scan budget, then compiling the filter to a native expression
instead of an SPI probe per candidate) the tails are bounded *below* pgvector's and the
median gap is down to 1.3–1.6×; the remaining constant is a per-candidate distance
recompute forced by pgvector's scan API, tracked upstream-shaped
([#32](https://github.com/ConsultingFuture4200/tridb/issues/32), history in #30/#31). Our
own docs tell you to use pgvector directly for that query class. We could have left this
leg out of the writeup. It's in.

## The half that isn't speed

One transaction manager across all three modalities is the part a bolt-on stack cannot
copy. We measured it rather than asserting it
([evidence](benchmark_wiki_consistency_v0.1.0.md)):

- **Injected mid-write failure:** TriDB 0 torn writes; the multi-store tore 42/42 —
  every injected failure left Milvus/Neo4j/Postgres permanently disagreeing.
- **Crash + recovery:** TriDB rolls back uncommitted work atomically across all three
  stores and keeps committed work durable (one WAL). The multi-store leaves an orphan:
  vector store has the new state, graph and relational have the old, and nothing
  reconciles.
- **Concurrent-read tearing:** multi-store readers saw torn cross-modal state in 76.7% of
  sampled reads; TriDB 1.0% (its vector + relational legs read at 0.0% under one MVCC
  snapshot; the residual is the native graph leg, which is commit-visible but not yet
  snapshot-isolated — documented limitation, on the roadmap).

This matters most for the workload we built the MCP surface for: **agent memory**, where
a memory is a row + an embedding + typed graph edges and you'd like all three to exist or
none. `docker run` the release image, `claude mcp add` the server, and store/recall
memories through the fused operator with graded, connection-weighted recall
([docs](mcp_agent_memory_v0.1.0.md)) — the recall ranking (personalized PageRank over the
bounded graph leg) won all 18 matched points on a 200k held-out link-prediction gate
before we made it the default ([ADR-0021](decisions/0021-ppr-default-graph-scoring.md)).

## Everything we know is wrong or unproven

Benchmarks lie by omission, so: our SM-1 metric (rows-examined reduction on the standin
corpus) **fails its own ≥5× target** (1.07×) under honest accounting. The 1M fused
head-to-head on the *fork* path is blocked by a vector-leg defect we documented instead of
working around. Early-termination recall is a **curve, not a number** — the default
operating point is approximate, and every benchmark reports the effort knob and censor
flag alongside recall. The graph store enforces a single writer (advisory lock; concurrent
writers serialize). `pg_dump` used to silently lose the entire graph — we found it,
published the audit, and fixed it (logical dump/restore with a round-trip gate) before
anyone's data did. The 128 GB memory-saturation headline has not been run. ARM/GX10 fork
claims are gated on that hardware and marked as such.

Every number above has a pinned dataset, a committed harness, and a one-command repro
(`make bench-public`, `make mcp-demo`, per-benchmark targets). If you beat our baseline
configs, we'll publish that too.

## Try it

```bash
docker run -d -e POSTGRES_PASSWORD=secret ghcr.io/consultingfuture4200/tridb/postgres-trimodal:pg17
# then: CREATE EXTENSION vector; CREATE EXTENSION graph_store_am; CREATE EXTENSION tjs_pg;
```

Repo: https://github.com/ConsultingFuture4200/tridb — v0.2.0
([release notes](releases/v0.2.0.md)), MIT, stock PostgreSQL 16/17.

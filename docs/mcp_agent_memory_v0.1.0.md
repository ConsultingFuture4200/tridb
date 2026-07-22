# TriDB as Agent Memory: the MCP Server (v0.1.0)

**TL;DR** — `tools/tridb_mcp.py` exposes the stock TriDB release image
(`tridb/postgres-trimodal:pg16|pg17`) as agent memory over the Model Context
Protocol: five stdio tools (store / connect / recall / neighbors / stats).
Any MCP-capable agent (Claude Code, etc.) becomes a TriDB user with zero
integration code. `make mcp-demo` is the one-command proof. Advisor plan 098;
depends on the ADR-0021 PPR default (plan 097) and the release image (plan 076).

## Why

A memory is not a bare embedding: it is text + a vector + typed links to other
memories. TriDB stores all three in ONE Postgres process — one transaction
manager, one WAL — and recalls them with the fused `tjs_open` operator, whose
seedless PPR default (ADR-0021) is exactly connection-weighted recall: memories
reinforced by multiple link paths rank ahead of vector-distance-tied strangers.

## The five tools

| Tool | Signature | What it does |
|---|---|---|
| `store_memory` | `(text, kind='note', embedding?) -> {id}` | Inserts the relational row, the vector, and the graph vertex in **one transaction**. |
| `connect` | `(src_id, dst_id, rel) -> {edge}` | Typed directed edge; `rel` names auto-register (`graph_store.register_edge_type`). |
| `recall` | `(query_text \| embedding, k=8, mode='fused'\|'vector', anchor_id?) -> {results, graph_censored, termination_reason}` | `fused` = seedless `tjs_open` under the PPR default; with `anchor_id` it lowers to the filter-first path (bounded traversal from the anchor). `vector` = plain HNSW. |
| `neighbors` | `(id, rel?, hops=1) -> [{id, text, kind}]` | Direct native-graph read: `gph_traverse_typed` (1 hop) / `gph_traverse_bfs` (multi-hop). |
| `memory_stats` | `() -> {memories, vertices, edges, edge_types, engine}` | Counts + engine identity (extension versions). |

`recall` results are ordered by the operator's ranking (vector similarity fused
with the PPR reserve in `fused` mode); the per-row `score` is the negative L2
distance, provided for reference only. Honesty travels: the fused response
carries `tjs_open_graph_censored()` (did the graph leg hit its work budget) and
the stream-termination reason (`term_cond` / `stream_end_unknown` /
`filter_first`) straight from the engine's per-backend probes.

## Run it

```bash
# one-time: build the release image + extras
make stock-release-smoke PG_MAJOR=17          # builds tridb/postgres-trimodal:pg17
pip install -r requirements-mcp.txt           # mcp==1.28.1 on top of the core floors

# the one-command proof: container up -> schema init -> store/connect/recall
# through the real stdio JSON-RPC transport -> teardown
make mcp-demo
```

Against your own instance:

```bash
export TRIDB_DSN=postgresql://postgres:<pw>@localhost:5432/postgres
python -m tools.tridb_mcp --init              # idempotent schema bootstrap
python -m tools.tridb_mcp                     # stdio server
```

Point Claude Code at it:

```bash
claude mcp add tridb-memory \
  -e TRIDB_DSN=postgresql://postgres:<pw>@localhost:5432/postgres \
  -- python -m tools.tridb_mcp
```

## The wiki demo corpus

<p align="center">
  <img src="../assets/tridb-mcp-demo.gif" alt="MCP session against the 200k-article corpus: store two memories, link them to articles, fused recall pulls the linked Mushroom article into the top-5" width="780">
</p>

The recording above runs this same server, unmodified, against a release-image
container pre-loaded with the 200,000-article enwiki slice (14,686,050 real
hyperlink edges) as the memories corpus — the corpus the plan-096 PPR gate
measured. Recipe (needs `data/wiki/enwiki` from `make wiki-extract` and its
`emb/dense_id_aligned.npy` embeddings, both bge-small-384 — the server's default
model, so query-text recall is semantically meaningful):

1. Generate the gate's load SQL and cut it at the sweep boundary:
   `python -m bench.wiki_ppr_gate --n 200000 --q 1 --gen-sql load.sql ...`,
   keep lines up to `\echo #WPG LOAD_DONE`.
2. Run it against a release-image container (`--shm-size=2g`, generous
   `maintenance_work_mem` for the HNSW build).
3. Project to the memories schema: add `kind` (default `'wikipedia_article'`)
   and `text` (article title, from the `articles-*.jsonl` shards) columns, then
   `ALTER TABLE articles RENAME TO memories` and
   `ALTER INDEX articles_hnsw RENAME TO memories_hnsw`.
4. `TRIDB_DSN=... python scripts/tridb_mcp_wiki_demo.py`

Notes: the corpus HNSW is `vector_cosine_ops` (fused `tjs_open` resolves the
distance operator from the index opclass; the server's pure-`vector` mode
hardcodes `<->`, which on normalized bge vectors ranks identically but will not
use this index — use `mode='fused'` here). At this graph scale the default
`tjs.graph_work_budget` binds, so recall responses honestly report
`graph_censored=True`.

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `TRIDB_DSN` | `postgresql://postgres:tridb@localhost:5432/postgres` | Postgres DSN (the release image's local shape). |
| `TRIDB_MCP_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model (onnx, CPU — no GPU dependency). |
| `TRIDB_MCP_DIM` | `384` | Vector dimension; must match the model AND the existing table. |
| `TRIDB_MCP_TERM_COND` | `64` | Seedless early-termination depth — THE recall knob (consecutive drops). |
| `TRIDB_MCP_SEEDS` | `4` | Top-m stream candidates used as PPR push seeds. |
| `TRIDB_MCP_HOPS` | `2` | Graph-leg depth bound. |

Callers can always pass `embedding` vectors directly (bring-your-own-encoder);
that is also the fallback when fastembed or its model download is unavailable —
the server starts, logs the degradation to stderr, and text-only calls fail
with an explicit error.

## The dense-id scheme (correctness, not tuning)

Memory ids are allocated by the server as `0, 1, 2, ...` in insertion order and
the graph vertex is upserted **in the same transaction**, so `ext_id == vid`
for every memory (the `tools/wiki_engine_load.py` identity lever). This is a
correctness precondition: `tjs_open`'s graph leg joins native vids straight
against `memories.id` (see `test/release_stock_smoke.sql`). `store_memory`
asserts the equality on every insert and aborts (rolling back the whole
memory) on drift rather than let the graph leg silently mis-address rows.
Identity mode (`gph_set_identity_mode`) is deliberately NOT flipped — the map
path is byte-equivalent for reads and safe by construction.

## The atomicity claim, exactly as far as tested

`store_memory` executes row insert + vector + `gph_upsert_vertex` inside one
`conn.transaction()` block on one connection — one Postgres transaction, one
WAL. Tested: the unit suite asserts all three statements share the transaction
block and that a dense-id drift aborts it; the live integration suite
(`tests/test_tridb_mcp.py::test_live_end_to_end`) verifies stored memories,
edges, and stats agree end-to-end against the release container. NOT tested:
crash-mid-transaction recovery of this specific surface (the underlying
engine's WAL/crash behavior is covered separately by `make stock-crash-test`).

## Honest limits

- **Single-operator, single-writer.** The graph AM's v1 contract is one writer;
  the dense-id allocation (`max(id)+1`) also assumes it. Run ONE server process
  per database. No auth/multi-tenancy — the DSN is the trust boundary.
- **The embedding model is a choice, not a truth.** BGE-small (384-d) is the
  CPU-cheap default; swap `TRIDB_MCP_MODEL`/`TRIDB_MCP_DIM` at init time. The
  dim is fixed at schema bootstrap; changing models later means re-embedding.
- **`fused` recall inherits the operator's bounds.** `tjs.graph_work_budget`
  caps the graph leg (disclosed via `graph_censored`); `term_cond` trades
  recall for latency (see the SM-4 recall curve, plan 072). Small stores are
  never censored in practice.
- **No delete/update surface in v0.1.0** (tombstones exist in the engine;
  exposing them is future work).
- **Protocol-level testing only.** The gate is the mocked-connection unit
  suite + the live container integration + the stdio JSON-RPC demo driver; no
  live LLM client is part of the verification.

## Verification (plan 098 gates)

- `pytest -k tridb_mcp` — 18 tests: mocked-psycopg unit suite + FastMCP
  registration + the live end-to-end (docker + release image; auto-skipped
  where unavailable).
- Live fused-recall assertion is the `test/tjs_ppr_test.sql` scenario rebuilt
  through the MCP surface: the multi-path-reinforced memory outranks its
  vector-distance-tied unconnected twin ([0,2,3,1], PPR-graded, negative
  control verified).
- `make mcp-demo` — container lifecycle + real stdio transport + recall
  printout, exit 0, no leftover containers.

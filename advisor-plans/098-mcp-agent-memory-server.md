# Plan 098: MCP agent-memory server on the stock release image (the first-user surface)

> **Executor instructions**: This is the product wedge — an MCP (Model Context Protocol) server
> exposing TriDB as agent memory, runnable against the release image with one command. Protocol-
> level tests are the gate; a live LLM client is NOT required (do not fabricate one). Skip the
> advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat da4d1e8..HEAD -- tools/ tests/ docs/ requirements* Makefile scripts/` (expect
> plan 097's merge in range — tjs_pg default is now PPR; your recall tool inherits that default)

## Status

- **Priority**: P1 (spike→product item 1)
- **Effort**: M
- **Risk**: LOW (new files only; no engine changes)
- **Depends on**: 097 (merged — PPR default), 076 (release image + smoke)
- **Planned at**: 2026-07-18

## Why this matters

TriDB is a finished engine with no consumer. The named wedge is agent memory (gBrain/AgentBOX):
one process that stores memories as (text, embedding, typed links) and recalls them with the
fused operator — connection-weighted recall is exactly what the PPR default now does. An MCP
server makes any MCP-capable agent (Claude Code, etc.) a user with zero integration code.

## Facts (advisor-verified)

- `psycopg==3.3.4` is already in `requirements.lock`. The official `mcp` Python package is NOT —
  add it via the extras pattern plan 058 established (`requirements-mcp.txt`, NOT the core lock).
- The release image (`tridb/postgres-trimodal:pg17`, plan 076) has all three extensions;
  `scripts/pg17_release_smoke.sh` is the lifecycle exemplar (exec-based psql, trap cleanup).
- Embeddings: the server must not depend on a GPU. Use `fastembed` (already a repo dependency —
  verify in requirements.lock; it powered the GraphRAG env-gated path) with a small default model,
  and accept caller-supplied vectors as the alternative (`embedding` param) so a client can bring
  its own.
- Recall path: seedless `tjs_open` (PPR default) for connection-weighted recall; direct HNSW
  `ORDER BY embedding <-> $1` for pure-vector recall. Typed edges via `register_edge_type` +
  `gph_insert_edge`; `gph_upsert_vertex` maps memory ids to vids (or dense ids if you enforce
  them — read `tools/wiki_engine_load.py`'s dense-id lever and pick ONE documented scheme).

## The surface (keep it this small)

MCP tools (stdio server, `tools/tridb_mcp.py`):
1. `store_memory(text, kind?, embedding?) -> {id}` — insert row + vector + vertex, one txn (the
   one-WAL story IS the demo: memory + embedding + graph node are atomic).
2. `connect(src_id, dst_id, rel) -> {edge}` — typed edge (auto-register rel names).
3. `recall(query_text|embedding, k=8, mode='fused'|'vector', anchor_id?) -> [{id, text, kind,
   score}]` — fused = seedless `tjs_open` under the PPR default (or filter-first when
   `anchor_id` given); expose `graph_censored` in the response metadata (honesty travels).
4. `neighbors(id, rel?, hops=1) -> [...]` — direct `gph_traverse_typed` read.
5. `memory_stats() -> {memories, edges, ...}` — counts + engine identity.

Config via env: `TRIDB_DSN` (default the release image's local DSN), `TRIDB_MCP_MODEL`
(fastembed model), `TRIDB_MCP_DIM`. Schema bootstrap: an idempotent `--init` that creates the
memories table + HNSW + graph init if absent.

## Deliverables

- `tools/tridb_mcp.py` (the server; stdio transport via the `mcp` package).
- `requirements-mcp.txt` (extras: `mcp`, pin what you install; core lock untouched).
- `tests/test_tridb_mcp.py` — protocol-level: spin the server functions against a MOCKED psycopg
  connection for unit logic, plus an integration marker test (skipped without docker) that runs
  the real tool functions against a live release-image container end-to-end: store 5 memories,
  connect 3, fused recall returns the connected one ranked by the graded score, stats correct.
  The integration test MUST run in this session (docker exists) — report its transcript.
- `scripts/tridb_mcp_demo.sh` — one command: start release image, init schema, run a scripted
  store/connect/recall session through the server's stdio (a JSON-RPC driver in the script or a
  tiny pytest -m demo), print the recall result, clean up. This is the README-facing "agent
  memory in one docker run" proof.
- `docs/mcp_agent_memory_v0.1.0.md` — what it is, the 5 tools, config, the one-WAL atomicity
  claim (stated exactly as far as tested), how to point Claude Code at it (`claude mcp add`
  one-liner), and honest limits (single-operator, single-writer graph contract, embedding model
  choice).
- `Makefile`: `mcp-demo` target.

## Out of scope

- Engine/extension changes of any kind (if the surface needs one, STOP and report).
- The AgentBOX/gBrain repo (cross-repo; this server is what that adapter will call).
- Auth/multi-tenant (single-operator posture, stated in docs).
- Publishing anything.

## Verification

- Unit + integration tests green (`pytest -k tridb_mcp`); integration transcript in the report.
- `bash scripts/tridb_mcp_demo.sh` → recall output printed, exit 0, no leftover containers.
- `make test && make lint` (new files lint clean; core suite untouched).
- Negative control: the fused-recall integration assert must fail if you flip its expectation
  (prove the graded ranking is actually asserted, not vacuously green).

## STOP conditions

- `mcp` package cannot run in this Python/env — report; do not hand-roll the protocol without
  flagging the tradeoff first.
- fastembed unavailable/undownloadable — fall back to caller-supplied embeddings ONLY, and say
  the demo does so.
- Anything requires touching src/.

## Git workflow

Branch `advisor/098-mcp-memory`. Commits: `feat(mcp): tridb agent-memory server`,
`test(mcp): protocol + live integration`, `docs(mcp): agent memory surface`.

REPORT FORMAT: STATUS / STEPS+verifications (incl. integration + demo transcripts) /
FILES CHANGED / NOTES / WORKTREE+commits.

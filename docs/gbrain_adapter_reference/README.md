# gBrain `TriDBEngine` adapter — handoff reference (Phase C)

These are a REFERENCE COPY (this repo is TriDB; the adapter belongs in **AgentBOX**,
`UMB-Advisors/AgentBOX`, path `gbrain-master/gbrain-master/src/core/`). The live work was on
AgentBOX branch `feat/tridb-engine` in an ephemeral clone that is now gone — apply this patch to
recreate it.

- `tridb-engine.ts` — the adapter (`class TriDBEngine extends PostgresEngine`): vector(pgvector)/BM25/
  pages/facts inherit unchanged; overrides only the graph leg (initSchema +graph_store_am,
  syncGraphFromLinks, native traverseNative, dual-write addLink/removeLink). **`bun x tsc --noEmit` = 0
  errors** against the full gBrain codebase.
- `0001-tridb-engine-adapter.patch` — the full 3-file change (tridb-engine.ts new + engine-factory.ts
  `case 'tridb'` + types.ts engine-union widening). Apply with `git am` or `git apply` in AgentBOX.

## To use in AgentBOX
1. `git checkout -b feat/tridb-engine && git am path/to/0001-tridb-engine-adapter.patch`
2. Requires the pgvector fork image: `scripts/add_pgvector.sh` -> `tridb/msvbase:gx10-v1-pgv`.
3. Set `engine: 'tridb'` in the gBrain config; `initSchema` does `CREATE EXTENSION vector` (inherited)
   + `graph_store_am` (added), NOT `vectordb`.

Status: FIRST-CUT, typechecks, NOT run against a live TriDB. Full traverseGraph/traversePaths
GraphNode-rebuild overrides are a follow-on. See `docs/benchmark_gbrain_graph_v0.1.0.md` for why the
graph leg is an architectural (consistency) win, not a latency win.

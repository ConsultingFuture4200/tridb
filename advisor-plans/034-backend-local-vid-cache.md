# Plan 034: Backend-local cached vid map for the v1 graph id-map (DEV-1345 / PERF-03)

> **Executor instructions**: Follow step by step; the ~2ms recovery claim needs a before/after number.
> On any STOP condition, stop and report. Update your row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 876a696..HEAD -- src/graph_store/ scripts/patches`
> Re-read the live id-map shim and the operator's `graphReachableT` wiring before editing.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW–MED (read-only cache; the only hazard is staleness under mid-session mutation, excluded
  by the current single-writer contract — but the invalidation hook must be designed in, not deferred)
- **Depends on**: plan 025 (the v1 id-map). Complements plan 033 (PERF-02); this handles the general
  sparse-id case 033 cannot.
- **Category**: perf
- **Planned at**: commit `876a696`, 2026-07-04
- **Linear**: DEV-1345

## Why this matters

Same root cause as PERF-02: the v1 id-map reverse translation (`gph_neighbors_ext`,
`src/graph_store/graph_store_am--0.1.0.sql:109-116`) costs ~1 µs/neighbor via btree + SPI, ≈ 2 ms/query
at fanout 2000. PERF-02's identity fast-path only helps **dense-id** loads; real datasets with sparse or
non-monotone external ids need a different fix. The honesty box names it directly
(`docs/benchmark_sm2_1m_v0.3.0.md:90-91`): a **cached** vid map. Translating D neighbors from a
backend-local hash (~50 ns each) instead of per-row btree + SPI (~1 µs each) recovers most of the tax
without assuming anything about the id distribution.

## Current state

- `src/graph_store/graph_store_am--0.1.0.sql:69-72` — `gph_vid_map(ext_id PK, vid UNIQUE)` heap table.
- `src/graph_store/graph_store_am--0.1.0.sql:109-116` — the correlated per-neighbor reverse subquery.
- The operator resolves the reachable set once at Open via SPI (`graphReachableT`,
  `scripts/patches/tridb_graph_v1_rewire.patch`) — this is the single call site to accelerate.
- Native scan primitives exist but are `static` in `src/graph_store/graph_am.c:561-672` (`gs_open`,
  `gs_getnext`, `gs_close`) — relevant if you translate in C rather than in the shim.
- Single-writer contract: `src/graph_store/graph_am.c:26-30`. Incremental/concurrent ingest is
  contract-blocked (metapage lock per edge, `graph_am.c:417-426`) — the cache is safe under it, but
  DIRECTION-04 (incremental ingest) will need the invalidation hook this plan installs.
- Backend-local hash precedent: Postgres `HTAB` (`dynahash`), created in a long-lived memory context.

## Commands you will need

Plan 024's engine table, plus:

| Purpose | Command | Expected |
|---|---|---|
| Graph-leg timing | psql `\timing` draining the reachable set at deg 2000, shim path vs cached path | before/after ms |
| Parity | `bash scripts/graph_test.sh tridb/msvbase:dev test/graph_v0v1_parity_test.sql` | ALL PASS |

## Scope

**In scope:** a backend-local `HTAB` (vid → ext_id) populated lazily on first probe from `gph_vid_map`,
in a session-lifetime memory context; a translation entrypoint the operator's reachable-set resolution
uses instead of the correlated-subquery shim (either a new C function in the `graph_store` extension that
returns already-translated ext_ids, or a `gph_neighbors_ext_cached(src)` SQL wrapper over a C translator);
an **invalidation hook** — a `CacheRegisterRelcacheCallback` (or equivalent) that clears the HTAB if
`gph_vid_map` changes (mirrors the ADR-0014 eviction pattern used for the HNSW index-map, plan 023) —
even if it is a documented no-op under today's single-writer contract; a test asserting cached ==
uncached translation; README row.

**Out of scope:** the dense-id identity case (plan 033 handles it more cheaply when applicable); resolving
the single-writer / metapage-serialization contract itself (that gates real incremental ingest, separate
work); `gph_locate_vertex` (PERF-06).

## Git workflow
Branch `advisor/034-vid-cache`; `perf(graph):` commit with before/after graph-leg ms; do NOT push.

## Steps

### Step 1: The cache + translator
Build the `HTAB` lazily on first translation; on a miss, fall back to a single btree probe and populate.
Decide C-in-extension vs SQL-wrapper-over-C and note why. Keep result ordering identical to the shim.
**Verify**: incremental compile clean; a scratch translation of a known vid set equals the shim's output
exactly.

### Step 2: Invalidation hook (design it in now)
Register a relcache/inval callback that flushes the HTAB when `gph_vid_map` is modified. Under the
single-writer + bulk-load-then-query contract this never fires mid-query, but installing it now is what
makes the cache **safe by construction** once DIRECTION-04 incremental ingest lands. Document the exact
contract the cache assumes in a code comment.
**Verify**: a scratch test that mutates `gph_vid_map` then re-translates returns fresh ids (hook fires);
STOP and report if you cannot install a reliable invalidation — a silently-stale id cache is a
correctness bug, not a perf win.

### Step 3: Wire the operator + measure
Point `graphReachableT`'s resolution at the cached translator. Run the deg-2000 graph-leg drain cached
vs uncached and record the ms; optionally the 1M SM-2 filter-first end-to-end on the GX10.
**Verify**: `test/graph_v0v1_parity_test.sql` ALL PASS byte-identical; before/after ms in the commit;
`make graph-test` + `make test && make lint` green.

## Test plan
Correctness = cached translation byte-identical to the shim (parity oracle + a direct cached-vs-uncached
assertion) AND the invalidation hook demonstrably flushes on mutation. Perf = recovered ms.

## Done criteria
- [ ] Backend-local `HTAB` translator, lazy-populated, session-lifetime context
- [ ] Invalidation hook registered + contract documented (no-op today, correct once ingest lands)
- [ ] Operator uses the cached translator; parity oracle ALL PASS byte-identical
- [ ] Before/after graph-leg ms recorded; `make graph-test` + `make test && make lint` green
- [ ] README row updated

## STOP conditions
- No reliable invalidation hook is available — do not ship a cache that can serve stale ids; report.
- Any parity-oracle answer changes.
- The cache memory context leaks across backends or bloats (a per-session full-map copy at very large V
  is a real cost — if V is huge, bound the cache or prefer PERF-02's identity path; measure and report).

## Maintenance notes
This is the general-case complement to plan 033: 033 skips the map entirely for dense-id loads; 034 makes
the map cheap for everything else. The invalidation hook is the load-bearing design element — it is what
lets the same cache stay correct when DIRECTION-04 turns on incremental ingest. Reviewer focus: the
invalidation correctness and the per-session memory bound at large V.

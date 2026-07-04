# Plan 033: Dense-id identity fast-path for the v1 graph id-map (DEV-1344 / PERF-02)

> **Executor instructions**: Follow step by step; the ~2ms recovery claim needs a before/after number.
> On any STOP condition, stop and report. Update your row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 876a696..HEAD -- src/graph_store/graph_store_am--0.1.0.sql tools/bench_sm2_corpus.py`
> The id-map shim and the SM-2 corpus emitter are the two files this plan touches — re-read them live.

## Status

- **Priority**: P2
- **Effort**: S–M
- **Risk**: LOW (detected fast-path; falls back to the map on any non-dense load)
- **Depends on**: plan 025 (the v1 native AM + id-map shim this optimizes). Complements plan 034 (PERF-03).
- **Category**: perf
- **Planned at**: commit `876a696`, 2026-07-04
- **Linear**: DEV-1344 (verifiable on the x86 standin — SQL + loader only)

## Why this matters

The ~2 ms/query v1 tax (4.7 → 6.66 ms at 1M, `docs/benchmark_sm2_1m_v0.3.0.md` honesty box) is **not**
in the native access method — the native traversal is already lean (read-once-per-page shipped, no WAL
on reads). It lives entirely in the SQL/plpgsql compat shim `gph_neighbors_ext`
(`src/graph_store/graph_store_am--0.1.0.sql:109-116`), which issues **one reverse B-tree probe per
emitted neighbor** (vid → ext_id) against the `gph_vid_map` heap side-table. At fanout 2000 that is
~2000 correlated index descents per query — an **O(out-degree(src))** cost that looks constant only
because v1 pins one fixed-degree source. It scales linearly with hub degree on any real graph.

The cheapest way out for the benchmark corpus (and any dense-id dataset): notice when the map is the
identity function and skip it entirely.

## Current state

- `src/graph_store/graph_store_am--0.1.0.sql:69-72` — `gph_vid_map(ext_id PK, vid UNIQUE)`, a plain heap
  table with two btree indexes.
- `src/graph_store/graph_store_am--0.1.0.sql:109-116` — `gph_neighbors_ext(src)`: 1× forward probe
  (ext_id→vid), native `gph_neighbors(vid)` drain, then a **correlated scalar subquery per neighbor**
  (`SELECT m.ext_id FROM gph_vid_map m WHERE m.vid = n.nvid`) = the D reverse probes.
- The operator calls this **once at Open** and caches the result (`scripts/patches/tridb_graph_v1_rewire.patch`,
  `graphReachableT` → `SELECT dst FROM graph_store.gph_neighbors_ext(<src>)` via SPI), so there is exactly
  one `gph_neighbors_ext` call per query but D internal reverse probes.
- `tools/bench_sm2_corpus.py:76-119` — the corpus already assigns **dense sequential** entity ids; only
  the vertex **load order** (edge-first-appearance, `:95-112`) differs from strict id order today.
- Parity oracle: `test/graph_v0v1_parity_test.sql` (must stay byte-identical).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Emitter unit tests | `.venv/bin/python -m pytest tests/ -q -k corpus` | pass |
| v1 AM engine suite | `bash scripts/graph_test.sh tridb/msvbase:dev test/graph_v0v1_parity_test.sql` | ALL PASS |
| Graph-leg timing | a psql `\timing` block draining `gph_neighbors_ext(src)` at deg 2000, map path vs identity path | before/after ms captured |

## Scope

**In scope:** a detected identity fast-path in `gph_neighbors_ext` (and any sibling the operator's
`graphReachableT` reaches) that, when a stored flag says "loaded dense + in id order," routes straight to
native `gph_neighbors(vid)` treating `ext_id == vid` — no forward probe, no reverse probes; a way to SET
that flag at load time (a `gph_vid_map` metadata row, or a `gph_set_identity_mode(bool)` the loader
calls after a verified dense load); the loader change in `tools/bench_sm2_corpus.py` to (a) materialize
vertices in strict id order so the identity holds and (b) call the flag setter; the sibling emitters
(`tools/bench_corpus.py`, `tools/sweep_corpus.py`, `tools/filtered_corpus.py`) only if trivially dense;
README row.

**Out of scope:** the general sparse-id case (plan 034 / PERF-03 owns the cached map); the native AM
internals; `gph_locate_vertex`'s separate linear scan (PERF-06); any change to answer semantics.

## Git workflow
Branch `advisor/033-dense-id-fastpath`; `perf(graph):` commit with the graph-leg before/after ms in the
body; do NOT push.

## Steps

### Step 1: Verify the identity precondition and add the flag
Confirm the corpus ids are dense `0..N-1` and that loading vertices in id order makes `vid == ext_id`
under the native vid assignment (read how `gph_insert_vertex` assigns vids — vids are dense/monotone per
`gph_page.h`). Add an identity-mode flag to the id-map (a metadata row or a small `gph_am` GUC/catalog
entry) plus a setter the loader calls ONLY after a verified dense-in-order load.
**Verify**: setter is a no-op on correctness (a scratch query with the flag OFF is unchanged); STOP if
`vid == ext_id` does not actually hold after an in-order load — then this optimization is not applicable
as designed and PERF-03 is the only path (report).

### Step 2: The fast-path branch
In `gph_neighbors_ext`, branch on the flag: identity mode → `SELECT nvid FROM gph_neighbors(src)` (src is
already the vid); else the current map path unchanged. Keep the function signature and result ordering
identical (the storage-emission order the parity oracle relies on must be preserved —
`graph_store_am--0.1.0.sql:107-108`).
**Verify**: `test/graph_v0v1_parity_test.sql` ALL PASS byte-identical in BOTH modes; the emitter unit
tests pass.

### Step 3: Loader + measurement
Change `tools/bench_sm2_corpus.py` to materialize vertices in id order and call the flag setter. Run a
graph-leg drain at deg 2000 with the flag ON vs OFF and record the ms delta (target: recover most of the
~2 ms). Optionally re-run the 1M SM-2 filter-first recipe on the GX10 for an end-to-end number.
**Verify**: SM-4 answers byte-identical to the current v1 (parity oracle + a `#SM2 RESULT` diff if the
1M run is done); before/after graph-leg ms in the commit.

## Test plan
Answer-invariance in both modes (parity oracle + emitter unit tests) is the correctness test; the
recovered ms is the perf evidence. A sparse-id load (flag OFF) must be provably unchanged.

## Done criteria
- [ ] Identity flag + setter added; OFF is a no-op; ON verified `vid == ext_id`
- [ ] `gph_neighbors_ext` fast-path branch; parity oracle ALL PASS byte-identical in both modes
- [ ] Loader materializes in id order + sets the flag; before/after graph-leg ms recorded
- [ ] `make test && make lint` green; README row updated
- [ ] (optional, GX10) 1M SM-2 filter-first end-to-end ms recorded, SM-4 byte-identical

## STOP conditions
- `vid == ext_id` does not hold after an in-order load — the identity premise is false; report and defer
  to PERF-03 (do not ship a fast-path that returns wrong ids).
- Any parity-oracle answer changes in either mode.
- The result ordering changes (a set-based rewrite reordered neighbors) — the oracle depends on emission
  order; keep it.

## Maintenance notes
This is deliberately a **detected fast-path, never the default** — sparse/real-world ids break the
identity and must fall back to the map. It pairs with plan 034 (PERF-03), which handles the general case
with a backend-local cache; ship both and the map cost is gone for dense and sparse loads alike. Reviewer
focus: the no-op-when-OFF proof and the emission-order preservation in Step 2.

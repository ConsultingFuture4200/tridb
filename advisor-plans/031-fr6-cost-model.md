# Plan 031: Replace FR-6's static threshold with a calibrated two-cost comparison fed by real graph-leg cardinality

> **Executor instructions**: Follow step by step; verify each step. On any STOP condition, stop and
> report. Update your row in `advisor-plans/README.md` when done (unless a reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- src/planner src/graph_store_ext test/join_order*.sql tools/`
> Plans 024 (regex) and 025 (probe swap/front-door port) land first — expected drift; re-read the
> lowering file before editing. If 025 moved `graph_query` into the v1 extension, apply this plan's
> lowering edits THERE.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (changes which physical body runs near the crossover; answers stay correct either way — both bodies are correct, they differ in cost and in exact-vs-approximate ranking)
- **Depends on**: advisor-plans/025-v1-rewire-and-truth-pass.md (lowering location + the deg(src) source)
- **Category**: perf / architecture
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

The FR-6 decision still uses the FROZEN scalar rule "filter_first iff relational selectivity ≤ 10%"
— a threshold rationalized for a probe-per-seed filter-first design that was never built. The landed
DEV-1290 body is a brute-force exact scan of `reachable(src) ∩ filter`, so its true cost is
`~a·|reachable| + b·|qualifying|·dim`, while vector-first's is `~c·examined(joint_sel, term_cond)·dim`
— the crossover moves with dimension, k, fanout, and term_cond, and the current decision doesn't
even SEE the graph leg (the lowering feeds only relational selectivity; `avg_out_degree` is a
carried-but-unused field). Failure mode: a selective relational filter + a mega-hub src picks
filter_first and drains an enormous reachable set. Literature (plan regret concentrated at the
selectivity phase boundary) says benchmark ACROSS the boundary, not at the endpoints. The frozen
functions must NOT change; this adds a new, better decision path beside them with a GUC switch.

## Current state

- FROZEN decision core: `src/planner/join_order.c` — `tridb_rel_selectivity`,
  `tridb_choose_join_order(rel_filter_matches, table_size, threshold DEFAULT NULL)` (GUC
  `tridb.join_order_selectivity_threshold`, default 0.10), `tridb_estimate_intermediate`. FROZEN =
  bit-parity with `src/planner/join_order_ref.py` pinned by `test/join_order_test.sql` +
  `tests/test_join_order.py`. DO NOT EDIT these functions.
- Stage-2 lowering (in `graph_query`, `src/graph_store_ext/graph_store--0.1.0.sql` at e345998;
  possibly relocated by 025): computes `tbl_size` from `pg_class.reltuples`, `est_matches` from an
  `EXPLAIN (FORMAT JSON)` row estimate of the ts filter, then
  `jorder := tridb_choose_join_order(est_matches, tbl_size)`; records via
  `set_config('graph_store.last_join_order', ...)`; passes `jorder` into 8-arg `tjs()`.
- deg(src) sources: v0 — `SELECT COALESCE(array_length(nbrs,1),0) FROM graph_store.adjacency WHERE
  vid = <src>` (exact, O(1)); v1 (post-025) — per-vertex degree via the mapping/compat layer, plus
  store-wide `gph_edge_count()/gph_vertex_count()` for avg-degree. Use whichever store the lowering
  version you find targets.
- Calibration data that already exists: `bench/results/sm2_1m_metrics.json` (vector-first samples,
  examined implicit via term_cond regime), `bench/results/sm2_1m_ff_raw.txt` (filter-first samples,
  drain = ~1208 rows, dim 128), `docs/benchmark_neon_sweep_v0.1.0.md` (term_cond→examined curves,
  20k×128 + 100k×768), `test/join_order_integration_test.sql` (2k-scale examined counts: vf up to
  1999/20, ff 1..3).
- Conventions: planner C is a PGXS extension (`src/planner/Makefile`), tests via
  `scripts/join_order_test.sh`-style harnesses; SQL-exposed wrappers in `join_order--0.1.0.sql`;
  design docs versioned under `docs/`.

## Commands you will need

Plan 024's table, plus:
| Purpose | Command | Expected |
|---|---|---|
| Planner ext suite | `bash scripts/join_order_test.sh tridb/msvbase:dev` | parity PASS (must stay bit-identical) |
| Lowering suites | `bash scripts/join_order_lowering_test.sh tridb/msvbase:dev && bash scripts/join_order_integration_test.sh tridb/msvbase:dev` | ALL PASS |
| Boundary sweep | `.venv/bin/python tools/join_order_boundary_sweep.py --help` (created in Step 4) | usage |

## Scope

**In scope:** NEW C function `tridb_choose_join_order_cost(...)` in a NEW file
`src/planner/join_order_cost.c` (+ SQL wrapper in `join_order--0.1.0.sql`, + `OBJS` in the planner
Makefile); a new GUC `tridb.join_order_mode` (`threshold`|`cost`, default `threshold` — zero
behavior change until flipped); lowering edits to fetch deg(src) + call the cost path when
mode=cost; `docs/join_order_cost_model_v0.1.0.md` (constants + derivation from the calibration
data); `tools/join_order_boundary_sweep.py` + a results doc; extensions to the two lowering tests;
README row.

**Out of scope:** the FROZEN functions and their parity tests; a third physical body / adaptive
mid-query switching (explicitly v2 — record in the design doc); `tjs()` C changes of any kind.

## Git workflow
Branch `advisor/031-fr6-cost-model`; `feat(planner):` commits; do NOT push.

## Steps

### Step 1: Cost model on paper first
Write `docs/join_order_cost_model_v0.1.0.md`: cost_ff = A·deg(src) + B·est_qual·dim where
est_qual = deg(src)·rel_sel; cost_vf = C·est_examined·dim where est_examined =
min(term_cond_regime_bound, k / max(joint_sel, eps)) with joint_sel = rel_sel·(deg(src)/N). Fit
A,B,C from the four calibration sources above (show the arithmetic; 1M point: ff 4.7ms @ 1208 rows
dim128; vf 171ms @ ~10k examined dim128; 2k point: integration-test counts). State validity limits
and the safe default (ties → vector_first, deg unknown → vector_first).
**Verify**: doc exists; constants reproduce the 1M decision AND the 2k integration-test decisions
correctly on paper (show both checks in the doc).

### Step 2: The C + SQL surface
`join_order_cost.c`: `tridb_choose_join_order_cost(deg bigint, rel_matches bigint, table_size
bigint, k int, dim int, term_cond int) RETURNS text` implementing Step 1 exactly (pure arithmetic,
STRICT-safe NULL handling per repo convention); constants as `#define` with the doc reference;
define the GUC `tridb.join_order_mode` in its `_PG_init` (mirror how `join_order.c` defines its
threshold GUC — read it first; if both files define `_PG_init`, merge registration carefully — the
module has ONE init).
**Verify**: `bash scripts/join_order_test.sh tridb/msvbase:dev` → parity suite UNCHANGED-green (frozen
core untouched); a scratch psql in the image exercises the new SQL function against the paper table.

### Step 3: Lowering integration behind the mode GUC
In the lowering: fetch deg(src) (one indexed lookup, source per "Current state"); when
`current_setting('tridb.join_order_mode', true) = 'cost'` call the cost function (k from LIMIT, dim
from the query vector's parsed length — the lowering already has the vector text; `array_length` on
a casted literal or count commas), else the existing threshold path. Record which mode decided into
the existing `graph_store.last_join_order` companion as `'<order> (<mode>)'`? — NO: keep the value
domain stable (tests assert exact strings); add a SECOND recorder `graph_store.last_join_order_mode`.
**Verify**: `join_order_lowering_test` + `integration_test` ALL PASS in default mode (byte-identical
behavior); new assertions: with `SET tridb.join_order_mode='cost'`, the mega-hub case (selective
filter + hub with deg comparable to table size) now picks vector_first while the small-hub selective
case still picks filter_first.

### Step 4: Boundary sweep evidence
`tools/join_order_boundary_sweep.py`: generate corpora sweeping fanout×window across the crossover
(reuse `tools/bench_corpus_shared.py`), drive both forced bodies via 8-arg tjs at each point
(engine image), record ms + examined, and emit the measured crossover vs both deciders' predictions
into `bench/results/join_order_boundary_sweep.json` + a short `docs/benchmark_join_order_boundary_v0.1.0.md`
(which decider tracks the measured optimum, regret table).
**Verify**: sweep runs at x86 scale (≤50k corpus); doc shows the regret comparison; `make test && make lint` green.

## Test plan
Frozen parity suites unchanged; lowering suites extended (mode default + cost-mode assertions incl.
the mega-hub case); sweep doc is the empirical evidence. Unit-test the pure cost function via its
SQL wrapper inside the lowering test file.

## Done criteria
- [ ] Frozen parity suites bit-identical green
- [ ] Default mode = zero behavior change (existing suites pass untouched)
- [ ] cost mode flips the mega-hub case; recorded via the new mode companion
- [ ] Cost-model doc + boundary-sweep doc + JSON committed; regret table shows cost-mode ≤ threshold-mode regret
- [ ] `make graph-test`, `make test`, `make lint` green; README row updated

## STOP conditions
- The frozen parity suite fails at ANY point — you touched the frozen core; revert and report.
- The fitted constants cannot reproduce BOTH calibration points within 3× — the model shape is
  wrong; report the residuals instead of shipping a bad fit.
- The lowering has moved (025) in a way that makes deg(src) unavailable cheaply — report options.

## Maintenance notes
Flip `tridb.join_order_mode=cost` as default ONLY after a full bench cycle runs both modes (track in
STATUS). The v2 items this deliberately defers: in-filter traversal third body (predicate bitmaps,
not per-node qual re-eval), adaptive mid-query switching, learned selectivities. Reviewer focus:
GUC registration collision between the two planner C files, and that `est_examined` clamps sanely
when joint_sel→0.

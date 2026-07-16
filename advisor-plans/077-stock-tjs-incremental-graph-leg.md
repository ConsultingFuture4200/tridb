# Plan 077: Replace stock TJS full-reach materialization with a bounded pull-based graph leg

> **Executor instructions**: This is an architecture-sensitive change. Complete the decision/test
> gate before operator code. TR-1 is absolute: do not ship a full reachable-set materializer under a
> different name. Skip the advisor index update. Stock PG16/17 are build targets; do not claim the
> fork/GX10 build passed off-target.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/graph_store/graph_am.c src/graph_store/graphstore.h src/graph_store/graph_store_am--0.1.0.sql src/tjs_pg/ test/ Makefile .github/workflows/ci.yml docs/decisions/0012-tjs-open-multiseed-retrieval.md spec/`
> Any traversal/operator change since the planned commit is a semantic drift review, not a blind
> merge.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: 071, 072, 074, 075
- **Category**: bug / perf / direction
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The stock TJS operator constructs complete BFS reach sets before it can rank or emit. That violates
TriDB's non-negotiable Open/Next/Close and early-termination rule and forfeits the product's central
efficiency claim. The replacement must pull graph work incrementally, bound memory/work independently
of graph size, expose censoring honestly, and preserve native adjacency-list topology.

## Current state

- `src/graph_store/graph_am.c:1724-1730` describes `gph_traverse_bfs` as a whole-BFS helper and says
  materializing at Open honors TR-1 “in spirit.” Lines 1746 and 1772-1829 allocate result arrays,
  visited state, and frontiers for the complete reach.
- `src/tjs_pg/tjs_pg.c:318-328` uses a materialize-mode SRF. Filter-first invokes BFS in a
  FROM-clause query at lines 369-402; seedless `reach_add_from_seed` at lines 245-269 calls the full
  BFS and copies every returned ID.
- `src/graph_store/graphstore.h:15-18` requires strict Open/Next/Close. Lines 170-187 warn that a
  FROM-clause `FunctionScan` materializes an SRF.
- `test/graph_traversal_test.sql:47-68` is the correct executor-placement proof: a target-list
  `ProjectSet` plus `LIMIT 5` yields five visits rather than draining the graph.
- ADR-0005 states cross-extension composition is through SPI, not static C linking, and requires
  target-list SRF placement for pull behavior.
- ADR-0012 rejects a materialized reach set and already specifies bounded local-push graph work,
  explicit examined counts, and a frontier-bound fusion target. Reuse that vocabulary rather than
  inventing an unrelated algorithm.

## Required invariants

1. `Open` allocates only fixed/budget-bounded state; it never walks the full graph.
2. Each `Next` advances graph/vector work incrementally and checks termination/cancellation.
3. `Close` releases relation, cursor, SPI, memory, and snapshot state on normal and early abandon.
4. No complete reachable-set array/tuplestore and no FROM-clause graph SRF.
5. Work/frontier memory is bounded by an explicit contract independent of `|V|`/`|E|`; a capped
   result is identified as censored, not exact.
6. Topology remains the native graph AM. Relational edge tables/joins and sidecar execution are
   forbidden.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused PG17 | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_tr1_test.sql` | all PASS markers |
| Full stock | `make stock-graph-test` | every PG17 suite passes |
| Host | `make test && make lint` | exit 0 |
| Static guard | `rg 'gph_traverse_bfs' src/tjs_pg` | no matches |

## Scope

**In scope**:
- `src/graph_store/graph_am.c`
- `src/graph_store/graphstore.h`
- `src/graph_store/graph_store_am--0.1.0.sql`
- `src/tjs_pg/tjs_pg.c`
- `src/tjs_pg/tjs_pg--0.1.0.sql`
- `test/tjs_pg_test.sql`
- `test/tjs_pg_tr1_test.sql` (create)
- `Makefile`
- `.github/workflows/ci.yml`
- `docs/decisions/0020-stock-tjs-incremental-graph-leg.md` (create; use next free ADR number)
- `spec/tridb_spec_v0.1.0.md` addendum or a new versioned addendum

**Out of scope**:
- Graph topology in relational tables, a sidecar, second WAL, or cross-system transaction.
- A new query language or additional canonical query forms.
- Unrelated pgvector or fork C++ optimization.
- Declaring fork/GX10 or 128-GB benchmark sign-off.

## Git workflow

Use an assigned `dustin/dev-NNNN` branch. Split the ADR/tests and implementation into reviewable
conventional commits; final code commit example: `fix(tjs): stream bounded native graph work`.

## Steps

### Step 1: Characterize results and prove the current TR-1 failure

Finish plan 071 and retain exact filter-first parity as a semantic baseline. Create
`test/tjs_pg_tr1_test.sql` with a deterministic graph spanning multiple adjacency pages and a
high-degree/multi-hop case. Add counters/reset accessors as test instrumentation if needed. Show the
current implementation drains/materializes the reachable graph before a small outer `LIMIT` and
that work scales with graph size.

**Verify**: the negative control must fail a condition equivalent to “LIMIT 1 consumed strictly less
than the reachable graph.” If it passes before the implementation, STOP because the test is not
observing graph work.

### Step 2: Ratify a bounded semantic contract before C changes

Write ADR-0020 and a spec addendum. It must separately define source-anchored filter-first and
seedless behavior, the graph-work/frontier bound, result exactness versus censoring, termination
reason, examined counters, deterministic tie-breaks, and how plan 074's metric API reports the cap.
Base seedless graph scoring/termination on ADR-0012's bounded local-push plus frontier-bound design
unless a measured host reference disproves it. Do not expose a new query-language parameter; use a
documented operator setting/internal limit compatible with the pinned surface.

**Design review gate**: a maintainer must approve the ADR before Step 3. If no algorithm can preserve
the published result contract while satisfying all six invariants, STOP and narrow/correct the
published semantics; do not implement another materializer.

### Step 3: Implement a pull-based bounded graph iterator

Factor a graph-AM cursor that keeps relation/page/frontier/visited state across calls, yields one
candidate per `Next`, and has explicit `Open`/`Close`. Frontier and visited capacity must be derived
from the approved work bound, not graph cardinality. Expose it to `tjs_pg` through a target-list
`ProjectSet` SPI cursor, following ADR-0005; fetch incrementally. Include typed/directional traversal
and cancellation checks. Ensure early cursor close invokes cleanup.

Do not implement this as a FROM-clause SRF, `SPI_execute` returning all rows, a tuplestore, or an
array of all reachable IDs.

**Verify**: graph-only SQL proves `LIMIT 1`, `LIMIT 5`, and early cursor close advance only bounded
visits; ASAN/valgrind where available reports no early-abandon leaks.

### Step 4: Drive filter-first and seedless TJS from incremental legs

Replace both `gph_traverse_bfs` call sites. Maintain only approved bounded candidate/frontier state;
advance vector and graph legs in `Next` until the ADR's safe emission/termination condition is met.
Preserve native ID/filter/ranking behavior where the result is uncensored. On graph-work cap, return
the documented approximate/censored result and set counters/reason consistently with plan 074.

**Verify**: old deterministic result tests and plan 071's filter-first parity still pass for fixtures
that complete within the bound; cap tests are deterministic and report censoring.

### Step 5: Make TR-1 a stock CI gate

Add the new suite to `STOCK_TESTS` and PG16/17 CI. Include a static host test or script assertion
that `src/tjs_pg` cannot call `gph_traverse_bfs` and cannot place graph SRFs in `FROM`.

**Verify**: full PG16 and PG17 stock suites pass; `make test && make lint` pass.

## Test plan

- Multi-page/high-fanout and multi-hop fixtures; small outer LIMIT proves partial consumption.
- Normal exhaustion, term-condition stop, graph-work cap, cancellation, and early cursor close.
- Filter-first and seedless paths; typed/directional edges; absent source and duplicate-reach cases.
- Memory/work bound remains constant when unreachable graph size increases.
- Result/parity checks on uncensored fixtures and explicit censored metadata on capped fixtures.
- PG16 and PG17 engine runs. Fork/GX10 remains explicitly unverified unless run on target.

## Done criteria

- [ ] `src/tjs_pg` has no `gph_traverse_bfs` reference and no full-reach equivalent.
- [ ] A stock TJS call with outer `LIMIT 1` demonstrably stops graph work before full reach.
- [ ] Frontier/visited/candidate memory has a documented hard bound independent of graph size.
- [ ] Capped output is observable as censored; uncensored parity remains green.
- [ ] ADR/spec, implementation, SQL comments, and counter tests state one contract.
- [ ] PG16/17 full stock suites, host tests/lint, and leak/early-close checks pass.

## STOP conditions

- The ADR design review is not approved.
- The implementation needs a full reach set, tuplestore, FROM-clause SRF, relational edge table, or
  cross-extension static link.
- Plan 071 finds an unexplained semantic mismatch before this change.
- Correct early emission cannot be proved under the proposed frontier bound.
- A cap would be hidden from callers or represented as exact output.
- Work enters fork/GX10-only code and cannot be built here; report it as unbuilt, never passed.

## Maintenance notes

Reviewers should inspect executor placement and cleanup, not only algorithm loops. Any future
optimization must preserve the work/memory bound and the negative-control test that makes a small
LIMIT stop before graph drain.

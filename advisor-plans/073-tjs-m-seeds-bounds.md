# Plan 073: Bound stock `tjs_open` seed count without removing vector-only mode

> **Executor instructions**: Execute every step and verification. Skip the advisor index update.
> Stock PG16/17 tests are required; fork/GX10 sign-off is not part of this plan.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/tjs_pg/tjs_pg.c src/tjs_pg/tjs_pg--0.1.0.sql test/tjs_pg_test.sql`
> Any change around `m_seeds` parsing or argument guards requires a fresh comparison before editing.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 072 (use the corrected harness for trustworthy engine results)
- **Category**: bug / security
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

The stock operator accepts every signed `m_seeds` value. Large values can force repeated full graph
reach construction and excessive backend work, while negative values silently alter phase behavior.
The fork already bounds this argument; stock must reject invalid work requests while preserving its
intentional `m_seeds = 0` vector-only/filter-first mode.

## Current state

- `src/tjs_pg/tjs_pg.c:279-289` reads `m_seeds` from argument 3.
- `src/tjs_pg/tjs_pg.c:311-316` validates `k`, `hops`, and `term_cond`, but not `m_seeds`.
- `src/tjs_pg/tjs_pg.c:480-485` allocates graph state for positive seed counts; lines 548-558 call
  `reach_add_from_seed` for each seed.
- `reach_add_from_seed` at lines 245-269 invokes `graph_store.gph_traverse_bfs` and copies the whole
  returned reach. That blocking implementation is addressed separately by plan 077; this plan only
  prevents unbounded caller input.
- Stock intentionally uses `m_seeds = 0`; do not copy the fork's lower bound of one.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stock PG17 | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_test.sql` | `ALL PASS`, exit 0 |
| Stock PG16 | run the same script with a PG16-built image | `ALL PASS`, exit 0 |
| Host checks | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `src/tjs_pg/tjs_pg.c`
- `test/tjs_pg_test.sql`
- `src/tjs_pg/tjs_pg--0.1.0.sql` only if its function comment documents accepted ranges

**Out of scope**:
- Fork operator behavior or patches.
- Changing seed selection, traversal, or bridge semantics.
- Removing `m_seeds = 0` support.
- Solving the blocking traversal defect (plan 077).

## Git workflow

Use `dustin/dev-NNNN` after a Linear issue is assigned. Suggested commit:
`fix(tjs): validate stock seed count`.

## Steps

### Step 1: Add failing boundary tests

Extend `test/tjs_pg_test.sql` using its existing error-assertion pattern. Assert that `m_seeds = -1`
and `m_seeds = 10001` raise an error whose message names `m_seeds` and `0..10000`. Add or retain a
successful `m_seeds = 0` query so the lower boundary is protected. A value of `10000` need only be
accepted by argument validation; do not execute an expensive 10,000-seed traversal in the test.

**Verify**: current stock PG17 fails the two rejection assertions and passes the zero-mode assertion.

### Step 2: Add the C guard

After arguments are decoded and before allocating state or opening SPI, reject
`m_seeds < 0 || m_seeds > 10000` with `ERRCODE_INVALID_PARAMETER_VALUE`. Match the existing argument
guard style and include the received value in the detail only if surrounding guards do so.

**Verify**: PG17 suite prints `ALL PASS`; repeat on PG16.

### Step 3: Document the contract where the SQL surface already documents arguments

If the extension SQL comment enumerates ranges, state `m_seeds: 0..10000`; otherwise do not add a
new documentation surface solely for this one guard.

**Verify**: `git diff --check && make test && make lint` exit 0.

## Test plan

- Rejected: `-1`, `10001`.
- Accepted behavior: `0` still returns the established filter-first/vector-only result.
- Existing ordinary positive-seed tests remain green.
- Run both stock PG16 and PG17 suites because this C extension targets both APIs.

## Done criteria

- [ ] C rejects values outside `0..10000` before any work begins.
- [ ] SQL regression protects both invalid boundaries and zero mode.
- [ ] Stock PG16 and PG17 `tjs_pg_test.sql` both print `ALL PASS`.
- [ ] Host suite and lint pass; only in-scope files changed.

## STOP conditions

- Live code already has a different documented `m_seeds` range.
- A zero seed count is no longer a supported stock mode.
- Tests require allocating 10,000 graph traversals to establish upper-bound acceptance.
- The change requires touching fork-only/GX10 C.

## Maintenance notes

The numeric bound is a safety limit, not proof of TR-1 compliance. If graph work becomes governed by
a separate bounded-work contract in plan 077, keep this API guard unless that contract explicitly
supersedes it with a stricter limit.

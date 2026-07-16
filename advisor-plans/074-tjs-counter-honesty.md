# Plan 074: Make stock TJS work and termination counters semantically honest

> **Executor instructions**: Run each verification and stop on semantic ambiguity. Skip the advisor
> index update. Do not invent a precise budget-exhaustion signal that pgvector does not expose.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/tjs_pg/tjs_pg.c src/tjs_pg/tjs_pg--0.1.0.sql test/tjs_pg_test.sql docs/decisions/0019-tjs-open-stock-pg-rehome.md docs/INSTALL_stock_pg.md`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: 072, 073
- **Category**: bug / tests / docs
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

Two published stock metrics currently claim information they do not contain. Filter-first
`examined` counts rows after `LIMIT k`, not qualifying work, and `budget_capped` labels natural index
exhaustion as a budget stop. Benchmark decisions based on either value can be wrong. The fix must
represent uncertainty explicitly rather than manufacturing a boolean answer.

## Current state

- `src/tjs_pg/tjs_pg.c:396` applies `LIMIT k`; lines 402-405 assign `SPI_processed` to
  `tjs_examined`, so a query with 101 qualifying rows and `k=5` reports 5.
- `src/tjs_pg/tjs_pg.c:575-583` sets `tjs_budget_capped = true` whenever streaming ends without
  satisfying `term_cond`. This also happens on natural exhaustion.
- `src/tjs_pg/tjs_pg--0.1.0.sql:39-48` exposes `tjs_open_examined()` and
  `tjs_open_budget_capped()`.
- ADR-0019 lines 65-67 describe the boolean as meaning `max_scan_tuples` ended the scan.
- pgvector's iterator API does not expose whether its end-of-stream was caused by the cap. The
  implementation therefore cannot truthfully distinguish those two cases today.
- `test/tjs_pg_test.sql:174-184` only checks a positive capped result; it lacks a natural-exhaustion
  negative case and a filter-first qualifying-count case.

## Target contract

- `tjs_open_examined()` reports qualifying rows examined by filter-first before top-k truncation;
  streaming semantics remain the number of candidates actually consumed.
- Add `tjs_open_termination_reason()` returning one of `filter_first`, `term_cond`, or
  `stream_end_unknown`.
- Keep `tjs_open_budget_capped()` for compatibility. Return `false` for known non-budget endings and
  SQL `NULL` for `stream_end_unknown`; never return `true` without an observable budget signal.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stock engine | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/tjs_pg_test.sql` | `ALL PASS` |
| Host | `make test && make lint` | exit 0 |
| Documentation | `git diff --check` | exit 0 |

## Scope

**In scope**:
- `src/tjs_pg/tjs_pg.c`
- `src/tjs_pg/tjs_pg--0.1.0.sql`
- `test/tjs_pg_test.sql`
- `docs/decisions/0019-tjs-open-stock-pg-rehome.md` (append a dated addendum)
- `docs/INSTALL_stock_pg.md`

**Out of scope**:
- Changing ranking, traversal, or result IDs.
- Claiming exact pgvector distance computations as `examined`.
- Altering fork metrics.
- Rewriting ADR history; append an addendum.

## Git workflow

Use the assigned `dustin/dev-NNNN` branch. Suggested commit:
`fix(tjs): report censored termination metrics`.

## Steps

### Step 1: Lock the metric contract with regressions

Add a deterministic filter-first fixture with more qualifying rows than `k`; assert returned rows
remain `k` while `tjs_open_examined()` equals the full qualifying count. Add an empty filter-first
case (`examined = 0`, reason `filter_first`, capped false), a term-condition stop (reason
`term_cond`, capped false), and an exhausted/possibly capped stream (reason `stream_end_unknown`,
capped is SQL NULL). Replace the existing assertion that assumes every stream end is capped.

**Verify**: at least the examined and unknown-ending tests fail against the current implementation.

### Step 2: Count filter-first survivors before LIMIT

Change the SPI query to carry the full qualifying count, for example with `count(*) OVER()` in a
subquery before the final `LIMIT`. Preserve the current metric-derived ordering and ID tie-break.
Handle zero rows explicitly because no window row exists to carry zero. Do not add a second full
result materialization in C.

**Verify**: result IDs are unchanged and the new qualifying-count assertions pass.

### Step 3: Represent termination uncertainty

Store an internal termination enum, expose the text accessor, and make the existing boolean accessor
return SQL NULL for unknown stream ends. Reset all per-call metrics at the same lifecycle point as
today so one backend call cannot leak state into the next. Update SQL comments.

**Verify**: all termination cases pass under PG17 and PG16.

### Step 4: Correct the documentation

Append a dated ADR-0019 addendum explaining the previous overclaim and the censored contract. Update
the stock install/measurement notes. Do not silently replace the original decision text.

**Verify**: `rg 'stream_end_unknown|termination_reason' src/tjs_pg docs test/tjs_pg_test.sql`
finds code, API, tests, and documentation.

## Test plan

Use the existing deterministic `tjs_pg_test.sql` fixture. Cover nonempty/empty filter-first,
term-condition stop, unknown stream end, metric reset across consecutive calls, and unchanged result
ordering. Run the full SQL suite on stock PG16 and PG17 plus host tests/lint.

## Done criteria

- [ ] Filter-first `examined` is not capped at `k`.
- [ ] No observable path reports `budget_capped = true` without a real upstream signal.
- [ ] Unknown endings produce reason `stream_end_unknown` and SQL NULL for the compatibility boolean.
- [ ] SQL comments, ADR addendum, and tests describe the same contract.
- [ ] PG16, PG17, host tests, lint, and `git diff --check` pass.

## STOP conditions

- A live pgvector API now exposes a definitive cap signal; stop and redesign around that signal.
- Computing qualifying count would alter result ordering or require C-side full materialization.
- Existing external consumers require the boolean to be non-null and no migration can be agreed.
- In-scope SQL versioning has moved to upgrade scripts; follow the established release pattern rather
  than editing `--0.1.0.sql` in place.

## Maintenance notes

Treat `stream_end_unknown` as right-censored measurement. A future pgvector cap/exhaustion API may
allow `budget_capped=true`; add that only with a regression proving the signal.

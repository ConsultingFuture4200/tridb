# Plan 090: Stock-PG crash-recovery gate + mechanical STOCK_TESTS↔CI lockstep

> **Executor instructions**: Two deliverables, serialized in this order: (1) a stock-PG
> crash-recovery (WAL REDO) driver incl. the missing freeze scenario, (2) collapse the duplicated
> stock suite list so CI and `make stock-graph-test` cannot drift. Plan 072 (fail-loud harnesses)
> must be merged first. Skip the advisor index update. Do not claim fork/GX10 runs off-target.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- scripts/crash_recovery_test.sh scripts/pg17_graph_test.sh scripts/pg17/ Makefile .github/workflows/ci.yml test/graph_freeze_test.sql test/crash_recovery_assert.sql`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (new harness; CI wiring)
- **Depends on**: 072
- **Category**: tests / CI
- **Planned at**: commit `a780b46`, 2026-07-16

## Why this matters

D2's ship surface is the stock extension, but the stock CI job runs only clean-lifecycle SQL
suites — the FR-7 crash/WAL-REDO property (the one-WAL golden rule's proof) is never exercised on
stock PG. Worse, `test/graph_freeze_test.sql:11` claims "crash/WAL durability of the frozen pages
is covered by scripts/crash_recovery_test.sh" — that script contains zero freeze scenarios (grep
confirms), so the claim is false on BOTH engines. Separately, the 12-suite stock list is duplicated
verbatim between `Makefile` `STOCK_TESTS` and `.github/workflows/ci.yml` with no mechanical check:
an added suite can be local-green and CI-missed.

## Current state (verified)

- `.github/workflows/ci.yml` `stock-pg` job: builds `scripts/pg17/` image per PG16/17 matrix and
  loops over 12 hardcoded `test/*.sql` suites via `scripts/pg17_graph_test.sh` — pure SQL, no crash
  driver. Then builds the release image.
- `Makefile:15-20` `STOCK_TESTS` lists the same 12 suites (currently equal to CI's list; no
  assert). `Makefile:106` loops them for `stock-graph-test` (plan 070).
- `scripts/crash_recovery_test.sh` is fork-coupled: default image `tridb/msvbase:dev`, hardcoded
  `B=/u01/app/postgres/product/13.4/bin` paths inside the container. It proves 4 REDO scenarios
  (committed row, uncommitted row, committed tombstone, uncommitted tombstone) — no freeze
  scenario.
- `test/graph_freeze_test.sql:11`: "-- (c) crash/WAL durability of the frozen pages is covered by
  scripts/crash_recovery_test.sh." — false today.
- `scripts/pg17_graph_test.sh` is the stock container-invocation exemplar (paths, initdb, PGXS
  build of `src/` inside `tridb/pg17-unfork:dev`); post-072 it is fail-loud.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Syntax | `bash -n scripts/pg17_crash_recovery_test.sh` | exit 0 |
| Stock crash PG17 | `bash scripts/pg17_crash_recovery_test.sh tridb/pg17-unfork:dev` | all scenarios PASS, exit 0 |
| Stock crash PG16 | same with the PG16 image | all scenarios PASS |
| Lockstep | `.venv/bin/pytest tests/test_stock_suite_lockstep.py -q` | pass |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `scripts/pg17_crash_recovery_test.sh` (create; stock driver)
- `scripts/crash_recovery_test.sh` + `test/crash_recovery_assert.sql` (add the freeze scenario;
  keep fork scenarios intact — the fork run itself stays engine/GX10-gated)
- `test/graph_freeze_test.sql` (only the coverage-claim comment if wording must change)
- `Makefile`, `.github/workflows/ci.yml` (lockstep + CI wiring)
- `tests/test_stock_suite_lockstep.py` (create)

**Out of scope**:
- Graph AM / operator C changes. If a crash scenario FAILS on stock, that is a FINDING — STOP and
  report with the transcript; do not patch C to green it.
- HNSW crash-recovery drivers (fork-specific; separate concern).
- Running the fork driver here beyond what the x86 fork image already supports.

## Git workflow

Use assigned `dustin/dev-NNNN`. Suggested commits: `test(stock): wal crash-recovery driver`,
`ci(stock): suite-list lockstep`.

## Steps

### Step 1: Stock crash-recovery driver

Create `scripts/pg17_crash_recovery_test.sh` reusing `scripts/crash_recovery_test.sh`'s scenario
logic but `scripts/pg17_graph_test.sh`'s container conventions (stock image paths, initdb, PGXS
build — post-072 fail-loud shape). Port all 4 existing scenarios; share
`test/crash_recovery_assert.sql` if its SQL is engine-neutral, else create a stock variant and say
why. The crash must be `pg_ctl stop -m immediate` after a pre-txn CHECKPOINT, exactly like the fork
driver, so REDO is forced.

**Verify**: PG17 run passes all scenarios; deliberately corrupt one assert in a temp copy and
confirm nonzero exit (negative control), then restore.

### Step 2: Add the missing freeze REDO scenario

Scenario 5 in BOTH drivers: commit + CHECKPOINT edges, run `gph_freeze(horizon)` committed but NOT
checkpointed, crash, restart → assert the frozen state was REDONE (re-freeze is a no-op /
`gm_frozen_horizon` advanced — reuse the observable `graph_freeze_test.sql` checks). Fork driver:
author the scenario, mark its run engine-gated if the fork image isn't available; stock driver:
must RUN here. Reconcile the `graph_freeze_test.sql:11` comment with reality (point to both
drivers).

**Verify**: stock scenario 5 passes on PG16 + PG17; the freeze test's coverage claim is now true.

### Step 3: Single source of truth for the stock suite list

Make `.github/workflows/ci.yml`'s stock-pg step invoke `make stock-graph-test` (parameterize the
image tag via a Make variable if needed) so the list exists once in `Makefile`. If the runner
context genuinely cannot use make there, instead add `tests/test_stock_suite_lockstep.py` that
parses both lists and asserts set-equality — but prefer the collapse. Add the new crash driver to
the stock CI job (both PG majors) after the SQL suites.

**Verify**: `git grep -c 'graph_typed_traversal_test' .github/workflows/ci.yml Makefile` shows the
list lives in one place (or the lockstep test exists and fails when a suite is removed from one
side — prove with a temporary perturbation, then revert); workflow YAML parses.

## Test plan

Driver negative control, freeze REDO on stock PG16/17, all 4 ported scenarios, lockstep
perturbation proof, `bash -n`, host tests/lint. Fork-driver scenario 5 is authored + static-clean;
its run is engine-gated and must be reported as such.

## Done criteria

- [ ] A stock-PG WAL-REDO crash driver exists, runs 5 scenarios green on PG16 + PG17 locally.
- [ ] Freeze durability is scenario'd, and `graph_freeze_test.sql`'s claim matches reality.
- [ ] Stock CI runs the crash driver; the suite list cannot drift between Makefile and CI.
- [ ] Negative controls proven; host tests/lint/YAML checks pass.

## STOP conditions

- A crash scenario genuinely fails on stock PG (REDO bug) — report as a finding with logs.
- `pg_ctl stop -m immediate` semantics are unavailable in the stock container layout.
- CI job time becomes prohibitive (>~10 min added) — report and propose gating (e.g. one PG major
  per PR, both on dispatch) instead of silently dropping coverage.
- Plan 072 not merged (the driver would inherit the masked-failure shape).

## Maintenance notes

Any new WAL-logged graph mutation (insert/tombstone/freeze/…) needs a paired REDO scenario in BOTH
drivers. The suite list lives in `Makefile` only; CI consumes it.

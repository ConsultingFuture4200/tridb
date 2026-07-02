# Plan 012: Wire the orphaned engine regression tests into the suite (HNSW recovery, reloptions recovery, double-scan, dead runner, sleep-poll)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `advisor-plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 408e852..HEAD -- Makefile scripts/crash_recovery_hnsw_test.sh scripts/tjs_test.sh test/hnsw_reloptions_recovery_test.sql test/_fork_bug_tjs_double_scan.sql`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW (additive test wiring; may surface real failures — that is the point)
- **Depends on**: none. Plan 010 owns wiring `test/tjs_open_smoke.sql` — do NOT wire it here.
- **Category**: tests
- **Planned at**: commit `408e852`, 2026-07-01

## Why this matters

Several shipped, quality-critical fixes have executable specs that CI never runs. The HNSW
rebuild-on-recovery oracle (`scripts/crash_recovery_hnsw_test.sh`, guarding the DEV-1235 WAL
durability fix) is not in the aggregate suite; the reloptions crash-recovery regression
(`test/hnsw_reloptions_recovery_test.sql`, guarding "a tuned index must not silently recover at
lower quality") references a driver script that **does not exist**; the double-scan segfault fix
has a passing regression only for `multicol_topk`, not for the `tjs()` shape actually on the hot
path; and a stale quarantined witness plus a dead runner script confuse the surface. A regression
in any of these merges green today.

## Current state

- `Makefile:23-30` — the aggregate suite the engine CI job runs (via `make graph-test` inside
  `make test-all`):

  ```make
  AM_TESTS := scripts/graph_am_test.sh \
              scripts/txn_atomicity_test.sh \
              scripts/crash_recovery_test.sh \
              scripts/graph_concurrency_test.sh \
              scripts/graph_edge_count_test.sh \
              scripts/join_order_test.sh \
              scripts/fork_bug_multicol_test.sh \
              scripts/hnsw_abort_stress_test.sh
  ```

  `scripts/crash_recovery_hnsw_test.sh` is absent from this list and referenced by nothing.
- `scripts/crash_recovery_hnsw_test.sh` — exists, drives `test/hnsw_recovery_test.sql` through a
  crash/restart cycle. One weakness (fix in Step 3): the post-restart wait is a fixed sleep —

  ```bash
  echo "--- restart: WAL-redo recovers committed heap tuple ---"
  runuser -u postgres -- "$B/pg_ctl" -D "$D" -o "-p 5432" -w start >/dev/null 2>&1
  ```

  and earlier a bare `sleep 1` after the immediate stop. `pg_ctl -w` does wait for startup, so the
  main risk is the `sleep 1` between stop and restart; the robust exemplar is the bounded,
  fail-loud poll in `scripts/crash_recovery_test.sh:104-130` (poll `pg_stat_activity` /
  `pg_isready`-style with `for i in $(seq 1 360); do ... sleep 0.5; done` and an explicit FAIL on
  timeout).
- `test/hnsw_reloptions_recovery_test.sql` — header says:

  ```
  -- GX10-GATED. This SQL drives a live cluster through a crash/recover cycle and is NOT part of
  -- `make graph-test`; run it via scripts/crash_recovery_reloptions_test.sh inside the
  -- tridb/msvbase:dev image ... AFTER the tridb_hnsw_reloptions.patch is applied.
  ```

  `scripts/crash_recovery_reloptions_test.sh` **does not exist** (`ls` → No such file). The SQL
  expects `-v recovery_phase=seed|assert` (two-phase: seed before crash, assert after restart).
- `test/_fork_bug_tjs_double_scan.sql` — quarantined witness (leading underscore = crashes by
  design, excluded from suites) covering the double-scan SIGSEGV for `topk()`/`multicol_topk()`/
  `tjs()`. The fix (`scripts/patches/tridb_fix_double_scan_snapshot.patch`) is applied and
  sentinel-verified in `scripts/lib/msvbase_patches.sh`, and the multicol shape has a passing
  regression (`test/fork_bug_multicol_double_scan.sql` via `scripts/fork_bug_multicol_test.sh`,
  in `AM_TESTS`). The witness's header still calls the fix "UNBUILT / draft" — stale.
- `scripts/tjs_test.sh` — orphaned runner: wraps `test/canonical_e2e_test.sql`, which
  `ENGINE_TESTS` already runs; nothing invokes `tjs_test.sh`.
- Conventions: every `AM_TESTS` script is standalone (`set -euo pipefail`, takes `[image]` as
  `$1` defaulting to `tridb/msvbase:dev`, fails loud, prints PASS lines). Model any new script on
  `scripts/crash_recovery_test.sh` (the richest exemplar) and `scripts/fork_bug_multicol_test.sh`
  (the simplest).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Python layer unaffected | `make test && make lint` | exit 0 |
| Shell syntax | `bash -n scripts/<new-or-edited>.sh` | exit 0 |
| Suite dry-run | `make -n graph-test` | prints all AM_TESTS incl. new entries |
| Engine run (only if image exists) | `make graph-test` | all PASS |
| One suite by hand (image) | `bash scripts/crash_recovery_hnsw_test.sh tridb/msvbase:dev` | PASS output |

## Scope

**In scope**:
- `Makefile` (AM_TESTS list only)
- `scripts/crash_recovery_hnsw_test.sh` (poll fix)
- `scripts/crash_recovery_reloptions_test.sh` (create)
- `scripts/fork_bug_tjs_double_scan_test.sh` (create) + `test/fork_bug_tjs_double_scan.sql` (create)
- `test/_fork_bug_tjs_double_scan.sql` (header note only, or delete after replacement — see Step 4)
- (Step 5 dropped — `scripts/tjs_test.sh` is intentionally NOT deleted; it is referenced by docs + ADR-0013)
- `advisor-plans/README.md` (status row)

**Out of scope**:
- `test/tjs_open_smoke.sql` and its wiring — plan 010 owns it.
- `scripts/crash_recovery_test.sh` — the working exemplar; do not "improve" it.
- Any `.patch` file; `.github/workflows/ci.yml` (plan 011 owns CI).
- `test/join_order_integration_stub.sql` — documented GX10-gated stub for unimplemented ADR-0011
  stages; leave as-is.

## Git workflow

- Branch: `advisor/012-engine-test-wiring` from `origin/master`
- Commit per step: `test(hnsw): wire crash_recovery_hnsw into AM_TESTS (advisor plan 012)` etc.
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Add the HNSW recovery oracle to AM_TESTS

Append `scripts/crash_recovery_hnsw_test.sh` to `AM_TESTS` in the Makefile.

**Verify**: `make -n graph-test | grep crash_recovery_hnsw` → prints the entry.

### Step 2: Author the missing reloptions-recovery driver

Create `scripts/crash_recovery_reloptions_test.sh` mirroring the structure of
`scripts/crash_recovery_hnsw_test.sh` (docker-exec bootstrap, initdb, install extensions), but
two-phase per the SQL's own contract: run
`psql -v recovery_phase=seed -f test/hnsw_reloptions_recovery_test.sql`, crash
(`pg_ctl -m immediate stop`), restart with the bounded poll from Step 3's pattern, then
`psql -v recovery_phase=assert -f ...` and fail loud on any ERROR/FAIL line. Read the SQL file
first — honor exactly the phase names and any `\if` conditions it defines. Append the new script
to `AM_TESTS`.

**Verify**: `bash -n scripts/crash_recovery_reloptions_test.sh` → exit 0;
`make -n graph-test | grep reloptions` → prints the entry; engine-gated live run if the image
exists (else report "engine-gated: unbuilt here").

### Step 3: Replace fixed sleeps with bounded fail-loud polls

In `scripts/crash_recovery_hnsw_test.sh` (and use the same pattern in Step 2's new script),
replace the bare `sleep 1` after `pg_ctl -m immediate stop` with a bounded poll that waits for
the postmaster pid to disappear, and after restart poll `SELECT 1` up to ~60s
(`for i in $(seq 1 120); do ... sleep 0.5; done`), failing loud on timeout — copy the shape of
`scripts/crash_recovery_test.sh:104-130` including its FAIL messages.

**Verify**: `bash -n` → exit 0; `grep -n "sleep 1$" scripts/crash_recovery_hnsw_test.sh` → no
matches.

### Step 4: Promote the tjs double-scan regression

Create `test/fork_bug_tjs_double_scan.sql` (no leading underscore): a *passing* regression that
runs a `tjs(...)` call and a second scan of the same table in one plpgsql block and asserts it
completes (the fixed behavior). Derive the scenario from the quarantined witness
`test/_fork_bug_tjs_double_scan.sql` (read it; reuse its corpus setup, drop the crash-witness
parts), and model the assert/driver on `test/fork_bug_multicol_double_scan.sql` +
`scripts/fork_bug_multicol_test.sh`. Create `scripts/fork_bug_tjs_double_scan_test.sh`
accordingly and append to `AM_TESTS`. Update the stale header of the quarantined witness: replace
the "UNBUILT / draft" sentence with "fix shipped (tridb_fix_double_scan_snapshot.patch, applied +
sentinel-verified); this file remains only as the crash-witness for pre-fix builds — the passing
regression is test/fork_bug_tjs_double_scan.sql". Keep the witness file.

**Verify**: `make -n graph-test | grep tjs_double_scan` → entry present; engine-gated live run if
image exists — **if the new test FAILS, that is a real finding: STOP and report it as such (the
fix may not cover the tjs shape), do not weaken the assert.**

### Step 5: (DROPPED) leave `scripts/tjs_test.sh` in place

REVISED 2026-07-02: the original plan deleted `scripts/tjs_test.sh` as a dead runner, but it is
referenced by `scripts/bench_live.sh:12` (a structural comment), `docs/sqlpgq_logical_plan_v0.1.0.md`,
`docs/fork_segfault_double_scan.md`, and `docs/decisions/0013-graph-store-v1-rewire.md` (created by
advisor plan 016). Deleting it would strand four references and entangle this plan with 016's ADR.
The deletion is low-value cleanup; **do NOT delete `scripts/tjs_test.sh`** — it stays. This step is
now a no-op. (The valuable work of this plan is Steps 1–4: wiring the orphaned engine regressions.)

**Verify**: `test -f scripts/tjs_test.sh` → exit 0 (still present, intentionally).

## Test plan

This plan IS test work. Final gate where the image exists: `make graph-test` → every suite PASS,
including three new entries (`crash_recovery_hnsw`, `crash_recovery_reloptions`,
`fork_bug_tjs_double_scan`). Where it doesn't: `bash -n` all touched scripts, `make -n` output
inspection, and an explicit "engine-gated: unbuilt here" note per suite.

## Done criteria

- [ ] `AM_TESTS` contains the three new/wired scripts (`make -n graph-test` shows them)
- [ ] `scripts/crash_recovery_reloptions_test.sh` exists, `bash -n` clean, two-phase per the SQL header
- [ ] No bare `sleep 1` remains in `scripts/crash_recovery_hnsw_test.sh`
- [ ] `test/fork_bug_tjs_double_scan.sql` + driver exist; quarantined witness header updated
- [ ] `scripts/tjs_test.sh` left in place (Step 5 dropped — still referenced by docs/ADR-0013)
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] Engine `make graph-test` PASS or explicit "engine-gated: unbuilt here" per suite
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

- `test/hnsw_reloptions_recovery_test.sql`'s phase contract differs from "seed|assert via -v
  recovery_phase" (read it first; if it needs three phases or different variables, report the
  actual contract before writing the driver).
- The promoted tjs double-scan regression fails on the built image — report as a real defect
  (fix doesn't cover tjs), do not soften the test.
- Any AM_TESTS suite that passed before your change fails after (ordering interaction — the
  crash_recovery family is order-sensitive by history).

## Maintenance notes

- The engine CI job is `workflow_dispatch`-gated; these suites gate merges only when someone
  triggers it. If per-PR engine coverage is ever wanted, that's a CI-capacity decision, not more
  wiring.
- Deferred: a predicate-aware early-termination regression (the DEV-1169 scale-defect class —
  needs a selective-predicate corpus design) and negative-path/`ereport` coverage in the v1 AM;
  both recorded in advisor-plans/README.md ranked findings.
- Reviewer: scrutinize that the reloptions driver's assert phase actually re-reads the reloptions
  (`m`/`ef_construction`) after recovery rather than only checking row visibility.

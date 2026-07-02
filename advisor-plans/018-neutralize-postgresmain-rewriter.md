# Plan 018: Neutralize the unused MSVBASE PostgresMain query rewriter (kills a memory-corruption/injection cluster + a per-query leak and query-text log leak)

> **Executor instructions**: Follow this plan step by step. Run every verification command and
> confirm the expected result before moving on. If anything in "STOP conditions" occurs, stop and
> report. When done, update the status row in `advisor-plans/README.md`.
>
> **Drift check (run first)**: this plan edits TriDB's patch chain, not vendor code. Verify the
> upstream code you are neutralizing still exists at the pinned commit:
> `cd vendor/MSVBASE && git show HEAD:patch/Postgres.patch | grep -n "approximate_sum\|lowercase(query_string)\|order\[100\]"`
> If those lines are gone, the pin changed — STOP.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (touches the protocol-layer query path via a fork patch; engine-gated verification)
- **Depends on**: none
- **Category**: security / perf
- **Planned at**: commit `408e852`, 2026-07-01
- **Upstream**: microsoft/MSVBASE `patch/Postgres.patch` (frozen; pinned commit `1a548db` == main HEAD, no open PRs — permanent fork)

## Why this matters

MSVBASE's `patch/Postgres.patch` injects a hand-written query rewriter into `PostgresMain` that
intercepts any statement containing `approximate_sum(...)` and string-rewrites it into a
`topk(...)` call. **TriDB never uses this path** — its canonical query lowers directly to
`tjs(%L, ...)` in `src/graph_store_ext/graph_store--0.1.0.sql:136`; `grep -rn approximate_sum
src/ test/ tools/ bench/` is empty. Yet TriDB inherits the rewriter's full liability:

- The rewriter's active branch (reached by any client that sends an `approximate_sum` query to a
  TriDB engine) contains a `char* order[100]` **stack overflow** with no bound
  (`Postgres.patch:354,391-401`), an **unbounded-`strcat` heap overflow** past a `palloc(strlen*2)`
  buffer (`:347,438-470`), **`'`-unescaped SQL injection** into the rewritten statement
  (`:438-470`), and a **`pfree(NULL)` crash** on any `approximate_sum` query with no WHERE clause
  (`:413,482`).
- Worse, the preamble runs on **every query**: `char* sql = lowercase(query_string)` +
  `palloc(strlen(sql)*2)` are allocated unconditionally before the `approximate_sum` check
  (`:334,347`), and `sql` is only freed inside the taken branch — a **per-query heap leak on all
  traffic** (`Postgres.patch:334,485`; UP-PATCH-09). There is also an
  `ereport(LOG, (errmsg("originial low canse string: %s\n", sql)))` that **logs the full text of
  every query** to the server log (`:~340`) — data-minimization/log-noise defect, always on.

Because TriDB doesn't use the rewriter, the correct, lowest-risk fix is to **remove the entire
rewrite hunk** from the applied `Postgres.patch` via a TriDB fork patch — eliminating the whole
cluster (UP-PATCH-01…06 and 09) in one move, with zero impact on TriDB's own query path.

## Current state

- TriDB applies the base MSVBASE patches by calling upstream's own applier:
  `scripts/lib/msvbase_patches.sh:106` → `( cd "$root" && bash scripts/patch.sh )`, which applies
  `spann.patch, hnsw.patch, Postgres.patch` (NOT `new_pg.patch`). Then TriDB stacks its own
  patches via `apply_tridb_fork_patches()` (same file, ~line 121+), each: check a sentinel →
  `git apply` the patch under `scripts/patches/` → `die` on failure; and `verify_patches()`
  (~line 42) greps end-state sentinels.
- The rewriter lives in `Postgres.patch`'s `PostgresMain` hunk. After `bash scripts/patch.sh`
  runs, the rewriter code is present in `vendor/MSVBASE/thirdparty/Postgres/src/backend/tcop/postgres.c`.
  Confirm the injected markers post-apply with:
  `grep -n "approximate_sum(\|originial low canse\|char\* order\[100\]" vendor/MSVBASE/thirdparty/Postgres/src/backend/tcop/postgres.c`
- The canonical lowering that proves TriDB doesn't need the rewriter:
  `src/graph_store_ext/graph_store--0.1.0.sql:136` builds
  `'SELECT t.chunk FROM tjs(%L, %s, 0, %s::bigint, %L, %L, %L) ...'` — a direct `tjs()` call, no
  `approximate_sum`.
- Convention for a new fork patch (mirror `l2_distance_scalar.patch` wiring in
  `msvbase_patches.sh:121-133`): patch file under `scripts/patches/`, an `apply` block with a
  sentinel guard, and a `verify_patches` grep that `die`s if the patch's effect is absent.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patch chain applies + sentinels (fast, no compile) | `bash scripts/ci_check_patches.sh` | exit 0 |
| Confirm rewriter gone post-apply | `grep -c "approximate_sum(" vendor/MSVBASE/thirdparty/Postgres/src/backend/tcop/postgres.c` | 0 |
| Python layer unaffected | `make test && make lint` | exit 0 |
| Engine build+suite (SLOW, gated) | `scripts/x86build.sh --docker && make test-all` | PASS |

## Scope

**In scope**:
- `scripts/patches/tridb_remove_pgmain_rewriter.patch` (create)
- `scripts/lib/msvbase_patches.sh` (apply block + `verify_patches` sentinel)
- `test/pgmain_rewriter_removed.sql` (create — asserts an `approximate_sum` query errors cleanly, not crashes, and that ordinary queries are unaffected) + wire into a suite
- `advisor-plans/README.md` (status row)

**Out of scope**:
- The executor hunks of `Postgres.patch` (`nodeSort.c`/`nodeIndexscan.c` `is_index_inorder`,
  `tuplesort_heapfull`, the removed wrong-order ERROR) — those are the RELAXED-MONOTONICITY
  mechanism TriDB's thesis depends on; plan 022 hardens them. Remove ONLY the PostgresMain
  string-rewriter block, not the executor changes.
- `new_pg.patch` — do NOT apply it (it's broken WIP: parses a hardcoded literal, duplicate symbols).
- Any TriDB operator patch.

## Git workflow

- Branch: `advisor/018-remove-pgmain-rewriter` from `origin/master`
- Commit: `security(fork): remove unused MSVBASE approximate_sum query rewriter (advisor plan 018)`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Locate the exact rewriter block

After a clean apply (`bash scripts/ci_check_patches.sh` clones + applies into a temp dir; or run
the apply steps manually into `vendor/MSVBASE`), open
`vendor/MSVBASE/thirdparty/Postgres/src/backend/tcop/postgres.c` and find the injected block in
`PostgresMain`'s simple-query handler: it starts at the comment `/* Parse multi topk statement */`
(right after `query_string = pq_getmsgstring(&input_message); pq_getmsgend(&input_message);`) and
ends where the rewritten `result` is dispatched (the block that ends by falling through to the
normal `exec_simple_query(query_string)` path). Capture the exact start/end lines.

**Verify**: you can quote the `char* sql = lowercase(query_string);` line and the closing brace of
the `if (approximateSumStart != NULL)` block from the applied file.

### Step 2: Author the removal patch

Create `scripts/patches/tridb_remove_pgmain_rewriter.patch` as a `git apply`-compatible unified
diff against `thirdparty/Postgres/src/backend/tcop/postgres.c` that **deletes the entire injected
block** identified in Step 1 (the `/* Parse multi topk statement */` comment through the end of the
rewrite dispatch), restoring the plain `query_string`/`exec_simple_query` flow. Leave a single
one-line comment sentinel in its place so `verify_patches` can assert removal, e.g.:
`/* TRIDB: MSVBASE approximate_sum PostgresMain rewriter removed (advisor plan 018) */`

Generate the diff by editing a copy of the applied file and `git diff --no-index`, or hand-author
the hunk. The patch applies AFTER `bash scripts/patch.sh` (it edits code that `Postgres.patch`
introduced), so it must be wired in `apply_tridb_fork_patches`, not before.

**Verify**: `cd vendor/MSVBASE && git apply --check ../../scripts/patches/tridb_remove_pgmain_rewriter.patch`
after a fresh base-patch apply → exit 0. (Do this inside `ci_check_patches.sh`'s flow; do not rely
on the gitignored working tree.)

### Step 3: Wire into the patch chain + sentinel

In `scripts/lib/msvbase_patches.sh`: add an apply block in `apply_tridb_fork_patches` (mirror the
`l2_distance_scalar.patch` block) that greps for the sentinel comment and applies if absent; and
add to `verify_patches` a grep asserting the sentinel IS present AND that `approximate_sum(` is
GONE from `postgres.c`, `die`ing otherwise. Order it after the base-patch apply.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0; the run's own verify step passes.

### Step 4: Regression test

Create `test/pgmain_rewriter_removed.sql`: (a) a plain `SELECT 1;` returns 1 (rewriter preamble
gone — no behavior change); (b) an `approximate_sum('...')` query now produces a normal parser
error (`function approximate_sum(...) does not exist` or syntax error), NOT a crash and NOT a
rewritten `topk` call. Wire it into `ENGINE_TESTS` in the Makefile (append to the list).

**Verify**: engine-gated — run under the image if present; else "engine-gated: unbuilt here" in
your report. `make -n graph-test | grep pgmain_rewriter_removed` → present.

## Test plan

- `test/pgmain_rewriter_removed.sql` is the regression (ordinary query unaffected; approximate_sum
  errors cleanly).
- `bash scripts/ci_check_patches.sh` proves the patch applies + verifies against the pinned clone.
- `make test && make lint` unchanged.

## Done criteria

- [ ] `scripts/patches/tridb_remove_pgmain_rewriter.patch` exists and `git apply --check`s clean
      against a freshly base-patched MSVBASE clone
- [ ] `msvbase_patches.sh` applies it (sentinel-guarded) and `verify_patches` asserts both the
      sentinel present and `approximate_sum(` absent
- [ ] `bash scripts/ci_check_patches.sh` exits 0
- [ ] `test/pgmain_rewriter_removed.sql` wired into ENGINE_TESTS
- [ ] `make test && make lint` exit 0; `git status` clean outside scope
- [ ] `advisor-plans/README.md` status row updated

## STOP conditions

- The rewriter block's boundaries are ambiguous (the executor hunks are interleaved with the
  string-rewriter in the same function such that you cannot delete one without the other) — STOP
  and report; plan 022 and this plan then need to be merged into one careful hunk.
- `approximate_sum` turns out to be emitted somewhere in TriDB after all
  (`grep -rn approximate_sum src/ tools/ bench/ test/` non-empty) — STOP; removal would break a
  real path.
- `ci_check_patches.sh` fails to apply the new patch twice after regenerating the hunk — report the
  raw `git apply` error.

## Maintenance notes

- This is a *removal* patch keyed to `Postgres.patch` line shapes; if `PIN_COMMIT` ever moves, the
  hunk must be regenerated (the whole chain re-validates on a re-pin anyway — `msvbase_patches.sh:19-24`).
- Reviewer: confirm ONLY the string-rewriter went, not the `nodeSort`/`nodeIndexscan` executor
  changes (those are load-bearing for relaxed monotonicity — plan 022).
- This also resolves the always-on per-query `lowercase()` leak and the query-text `ereport(LOG)`
  in one stroke; note that in the PR description.

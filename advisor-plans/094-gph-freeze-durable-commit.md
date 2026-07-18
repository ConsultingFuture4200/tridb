# Plan 094: Make a committed `gph_freeze()` durable (close the async-flush window)

> **Executor instructions**: Small engine-C change with a crash-driver proof. The negative control
> is already half-built: both crash drivers carry a documented WAL-barrier workaround whose removal
> must FAIL pre-fix and PASS post-fix. Build/verify on stock PG16/17 + the x86 fork image; no
> ARM/GX10 sign-off claims. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat 6de2e30..HEAD -- src/graph_store/graph_am.c scripts/crash_recovery_test.sh scripts/pg17_crash_recovery_test.sh test/crash_recovery_assert.sql`

## Status

- **Priority**: P1 (durability surprise on the anti-wraparound gate)
- **Effort**: S
- **Risk**: LOW–MED (engine C, but a single well-understood flush call)
- **Depends on**: 090 (merged in your base)
- **Category**: correctness / engine C
- **Planned at**: commit `6de2e30`, 2026-07-17

## Why this matters

Plan 090 proved with pg_waldump that `gph_freeze()`'s transaction assigns no xid, so its COMMIT
takes PostgreSQL's async path (`XLogSetAsyncXactLSN`): an immediate crash inside the walwriter
window silently loses the *entire committed* freeze. The freeze is idempotent and re-runnable, but
"committed and reported OK" should mean durable for the operation that guards against wraparound.
The fix is to flush WAL synchronously before `gph_freeze` returns.

## Current state (verified)

- `src/graph_store/graph_am.c` — `gph_freeze(horizon xid)`: GenericXLog page-walk
  (committed→Frozen / aborted→Invalid, relfrozenxid advance, idempotent; plan 036). It writes page
  WAL via GenericXLogFinish per page but never forces a flush; the surrounding txn is xid-less.
- `scripts/crash_recovery_test.sh:266-275` and `scripts/pg17_crash_recovery_test.sh:255-264`:
  scenario 5 currently works around this with a committed data-writing INSERT (row 9000, the
  "wal-flush barrier") whose commit forces the flush; the comments document the mechanism and that
  `SELECT txid_current()` is not sufficient.
- Both drivers + `test/crash_recovery_assert.sql`'s `freeze` phase are green on stock PG16/17 and
  the x86 fork image (merged plan 090 state).

## The fix

At the end of `gph_freeze`, after the last GenericXLogFinish (including the metapage update),
capture the maximum LSN returned by the GenericXLogFinish calls and `XLogFlush(max_lsn)` before
returning. Notes:
- `GenericXLogFinish` returns the record's end LSN — track the max across the walk; flush once at
  the end, not per page.
- Flush only when at least one record was written (a no-op re-freeze must not flush).
- This makes the freeze durable at *function return*; the enclosing COMMIT remains async, which is
  fine — the freeze's own WAL is on disk, so REDO replays it regardless of the commit record's
  flush timing. State exactly this in the function's comment. If you conclude the commit-record
  timing DOES still matter for visibility of the freeze after replay (think it through against the
  drivers' assert phases — frozen pages are not xid-gated), STOP and report the reasoning instead
  of shipping a partial fix.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stock crash PG17 | `make stock-crash-test PG_MAJOR=17` | 5 scenarios PASS |
| Stock crash PG16 | `make stock-crash-test PG_MAJOR=16` | 5 scenarios PASS |
| Fork crash (x86) | `bash scripts/crash_recovery_test.sh tridb/msvbase:dev` | 5 scenarios PASS |
| Stock suites | `bash scripts/pg17_graph_test.sh tridb/pg17-unfork:dev test/graph_freeze_test.sql` | ALL PASS |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**: `src/graph_store/graph_am.c` (`gph_freeze` only), both crash drivers (remove the
barrier workaround + its comment, replace with a short note that `gph_freeze` now flushes),
`test/crash_recovery_assert.sql` only if the freeze phase text references the barrier.

**Out of scope**: any other WAL/commit behavior; the fork patches; other xid-less paths (if you
find more, report them as findings); GX10/ARM claims.

## Git workflow

Branch `advisor/094-freeze-durable`. Suggested commit: `fix(graph): flush wal before gph_freeze returns`.

## Steps

### Step 1: Negative control using the existing workaround

In BOTH drivers, remove the barrier INSERT (temp edit, uncommitted) and run scenario 5 against the
CURRENT (unfixed) code on stock PG17: it must FAIL (freeze lost — exactly plan 090's finding). This
re-proves the window exists and that the driver detects it.

**Verify**: scenario 5 fails without the barrier, pre-fix. If it passes, STOP — the window is not
being observed (timing?); investigate before touching C.

### Step 2: Implement the flush

Per "The fix" above. Keep the change minimal — track max LSN, one `XLogFlush` at the end, comment
explaining the xid-less async-commit rationale with a pointer to the drivers' scenario 5.

**Verify**: extension builds clean on stock PG17 (the harness builds it; watch for warnings).

### Step 3: Prove it and remove the workaround for good

With the fix in place, permanently remove the barrier INSERT + rewrite the workaround comments in
both drivers (short note: `gph_freeze` flushes its own WAL as of plan 094; the drivers now test
that contract directly). Run scenario 5 (and the full 5-scenario suite) on stock PG17, PG16, and
the x86 fork image — all green WITHOUT the barrier.

**Verify**: all three engines 5/5 PASS; `test/graph_freeze_test.sql` still ALL PASS on PG17;
`make test && make lint && git diff --check` green.

## Done criteria

- [ ] Scenario 5 without the barrier: fails pre-fix (observed), passes post-fix on stock PG16+17
      and the x86 fork image.
- [ ] `gph_freeze` flushes at most once, only when it wrote WAL; no-op re-freeze does not flush.
- [ ] Barrier workaround deleted from both drivers; comments updated.
- [ ] Full crash suites + freeze suite + host tests/lint green.

## STOP conditions

- The Step-1 negative control cannot reproduce the loss (window not observable in this run mode).
- The visibility question in "The fix" resolves against a function-local flush.
- `GenericXLogFinish`'s LSN return contract differs on PG13.4 vs 16/17 in a way that breaks the
  shared C — report; do not fork the logic without saying so.

## Maintenance notes

Any future xid-less maintenance operation (compaction, relayout) needs the same flush-on-return
contract and a scenario in both crash drivers. The drivers' scenario 5 is now the regression test
for this exact class.

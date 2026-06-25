# Fork segfault: a second table scan in the same plpgsql block as topk()/multicol_topk()/tjs()

**Issue:** DEV-1236 (spike / diagnose) · **Status:** root-caused from source; fix DRAFTED, **UNBUILT-HERE**
**Date:** 2026-06-25 · **Box:** x86 standin (not GX10); no docker image built or run this session.

## TL;DR

Co-issuing a second SQL statement that drives the executor (e.g. `SELECT count(*) FROM entities`)
in the **same plpgsql block** as a call to `topk()` / `multicol_topk()` / `tjs()` crashes the
backend (`SIGSEGV`). All three operators share one lifecycle, forked from `topk.cpp`: they build a
child `IndexScan` via SPI, capture the **caller's active snapshot once** at `CreateQueryDesc`, and
then drive `ExecProcNode` **across multiple SRF per-call invocations** without ever taking
ownership of a snapshot (`PushActiveSnapshot` / `RegisterSnapshot`). The sibling scan in the same
block mutates the active-snapshot stack and the plpgsql/SPI eval state out from under the
operator's still-open child executor. The most-likely fault is a **dangling/registered-away active
snapshot** read during `ExecProcNode` on a later SRF call (a snapshot/resource-owner lifecycle
collision), with a confirmed, separate **use-after-free in `EndFaginsState`** as an aggravating
second defect. The fix is to make the operator own its snapshot for the whole SRF lifetime
(register it on the multi-call context / push-pop around each drive) and to correct the teardown
ordering. **It cannot be compiled or verified on this box — GX10-gated.**

This is a **pre-existing MSVBASE fork bug**, not introduced by TJS (DEV-1169). TJS inherits it by
forking the same `execFagins` lifecycle. Attribution was already proved with **unmodified
`multicol_topk` alone** (no `tjs`, no `graph_store`) — see `test/_fork_bug_multicol_double_scan.sql`
and ADR-0007 §Consequences.

---

## The failing shape

```sql
DO $$
DECLARE got bigint[]; corpus bigint;
BEGIN
    SELECT count(*) INTO corpus FROM entities;     -- (1) sibling scan, drives the executor
    SELECT array_agg(id) INTO got FROM (           -- (2) operator call, drives its own SPI scan
        SELECT t.id FROM multicol_topk('entities', 5, 0, 'id', '', '',
                       'embedding <-> ''{19,0,0,0,0,0,0,0}''') AS t(id bigint, d float8)
    ) q;
END $$;
```

Server log: `server process (PID …) was terminated by signal 11: Segmentation fault`, failing
process = the `DO` block. Top-level (non-plpgsql) calls and back-to-back operator calls are
unaffected — the second statement must be in the same plpgsql block. Repro witnesses (all three
operators + discriminating variants) in `test/_fork_bug_tjs_double_scan.sql` (NOT in CI; crashes
the backend by design).

---

## How the operator drives its child scan (the relevant lifecycle)

All three operators (`topk.cpp`, `multicol_topk.cpp`, `tjs_operator.cpp`) implement the identical
SRF pattern. Citing `multicol_topk.cpp` (the minimal, no-graph reproducer):

1. **First SRF call** (`SRF_IS_FIRSTCALL`), in `funcctx->multi_call_memory_ctx`:
   - `SPI_connect()` (line 599).
   - `extractIndexScanNodeM(sourceText)` → `SPI_prepare` + `SPI_plan_get_cached_plan` to get a
     `PlannedStmt` with an `IndexScan` (lines 276-305).
   - `CreateQueryDesc(plan, sourceText, GetActiveSnapshot(), InvalidSnapshot, …)` (lines 632-634)
     — **the caller's active snapshot is captured by pointer, not pushed or registered.**
   - `standard_ExecutorStart(queryDesc, 0)` (line 638).
   - `findIndexScanStateM(queryDesc->planstate)` → live `IndexScanState` cached in `FaginsState`
     (lines 641-646).
   - `InitFaginsState(table_open(...), …)` keeps the open relation, the `QueryDesc`, and the
     `IndexScanState` for the duration of the SRF (lines 499-522, 649-650).
2. **Each per-call** invocation: `ExecProcNode((PlanState*) top_execNode)` → `execFaginsM` drains the
   child `IndexScan` via `ExecProcNode((PlanState*) node)` (line 433), reading the live distance from
   `node->iss_ScanDesc->xs_orderbyvals[0]` (line 446) and the tid from `xs_heaptid_orig` (line 450).
3. **Final call** (`finish:`): `EndFaginsStateM(top_execNode)` then `SPI_finish()` (lines 705-709).

Critically: the child executor (`queryDesc` + its `IndexScanState` + `iss_ScanDesc`) stays **open
and is actively pulled** across multiple SRF re-entries. The only snapshot it has is the bare
pointer captured at step 1 from `GetActiveSnapshot()`.

Contrast with how core Postgres runs an executor plan from inside a SQL function
(`thirdparty/Postgres/src/backend/executor/functions.c`, the `postquel_*` path): before every
`postquel_getnext`, it **`PushActiveSnapshot(...)`** (lines 1154 / 1166 / 1476) and pops it after.
The operators never do this; they assume the snapshot they captured at first-call is still the
active, valid one on every later re-entry.

---

## Evidence chain → root cause

### Primary cause: snapshot / resource-owner lifecycle collision

The sibling `SELECT count(*) FROM entities` is run by plpgsql through
`exec_eval_expr` → `exec_eval_simple_expr` / `exec_run_select`
(`pl_exec.c` lines 5861-5868). That path manages the **active snapshot stack** and the plpgsql
**`eval_tuptable`** for the simple expression, and at statement boundaries plpgsql advances the
command counter and can push/refresh a transaction snapshot (`pl_exec.c` line 6354,
`PushActiveSnapshot(GetTransactionSnapshot())`). The net effect is that **the active snapshot that
was current when the operator later calls `GetActiveSnapshot()` — and the snapshot the operator's
child `QueryDesc` holds — are not guaranteed to be the same object across SRF re-entries**, and the
snapshot the operator captured can be popped/freed by the surrounding plpgsql/SPI machinery while
the operator's child `IndexScan` is still open and being pulled.

When `execFaginsM` re-enters and calls `ExecProcNode` on the child `IndexScan`, the HNSW
`amgettuple` path dereferences `scan->xs_snapshot` (the captured `estate->snapshot`) for MVCC
visibility checks on each fetched heap tid. If that snapshot has been popped/freed, this is a read
of freed memory → `SIGSEGV` inside the index/heap visibility check. This matches the issue's stated
most-likely cause: a **scan-descriptor / memory-context / resource-owner lifecycle collision when a
second scan opens in the same block**, and it explains the precise trigger conditions:

- **Why only in a plpgsql block:** a top-level `SELECT … multicol_topk(…)` runs as one portal with
  one stable active snapshot for the whole SRF; nothing reshuffles the snapshot stack between SRF
  re-entries. Inside a plpgsql block, each statement is its own SPI execution with its own
  snapshot push/pop, so a *prior* statement leaves the stack in a state the operator's bare
  captured pointer no longer matches.
- **Why a sibling scan specifically (not just any statement):** the sibling scan forces plpgsql to
  take/refresh an active snapshot for its own executor run; that is the event that displaces or
  frees the pointer the operator later relies on. (Shape B in the repro tests whether *any* scan or
  only an own-table scan triggers it — it discriminates snapshot-stack disruption from a
  relation-lock/relcache aliasing alternative.)
- **Why back-to-back operator calls are fine:** each operator call opens AND finishes its own SPI
  scope; nothing else runs between its first and final SRF calls to disturb the snapshot.

The operators pass `InvalidSnapshot` as the crosscheck snapshot and **never `RegisterSnapshot`** the
query snapshot against a resource owner, so nothing keeps the captured snapshot alive for the
operator's extended, multi-call lifetime — Postgres is free to release it at the sibling
statement's boundary.

### Secondary cause (confirmed by inspection): use-after-free in `EndFaginsState`

Independent of the snapshot issue, `topk.cpp`'s teardown reads `state` **after** freeing it:

```c
// vendor/MSVBASE/src/topk.cpp  EndFaginsState(), lines 547-552
    delete(state->proc_pq);
    delete(state->seenSet);
    delete(state->result_stack);
    free(state);          // (a) state freed here …
    free(state->qDescs);  // (b) … then state-> dereferenced AFTER free  ← use-after-free
```

`multicol_topk.cpp`'s `EndFaginsStateM` has the **same** bug (lines 539-540: `free(state);` then
`free(state->qDescs);`). Two defects in one line: (1) `state->qDescs` is read after `free(state)`
(UAF); (2) `state->qDescs` is a `std::vector*` created with `new` in `InitFaginsState`, so it must
be `delete`d, not `free`d (mismatched alloc/free; `topk`/`multicol_topk` also never free
`state->children` or — in topk — leak it). On many allocators this is latent (the freed block is
not yet reused), which is why it does not crash every call; under the heap churn introduced by the
extra sibling scan it becomes a live crash. `tjs_operator.cpp`'s `EndTJSState` (lines 519-532)
**already fixes this**: every C++ member is `delete`d first and `free(state)` is the last statement.
So `tjs()` does NOT carry the secondary defect — but it still inherits the primary (snapshot) one,
which is why `tjs()` also crashes on Shape A.

### Why the primary cause is the operative one for this issue

The UAF alone would crash on a *normal* teardown without any sibling scan; the bug is specifically
triggered by the sibling scan and `tjs()` (which has no UAF) still crashes. Therefore the
**snapshot/resource-owner lifecycle collision is the primary, shape-specific cause**, and the UAF is
a separate, real defect that should be fixed in the same pass (and likely contributes to crash
variability under the extra allocation churn).

---

## Proposed mitigation / fix

Make each operator **own a snapshot for its entire SRF lifetime** and stop depending on the
caller's active-snapshot stack staying put across re-entries:

1. **Register and pin the query snapshot.** At first-call, instead of
   `CreateQueryDesc(plan, …, GetActiveSnapshot(), InvalidSnapshot, …)`, take a fresh snapshot owned
   by the SRF and registered against a resource owner that lives as long as the multi-call context:
   ```c
   Snapshot snap = RegisterSnapshot(GetTransactionSnapshot());   // pinned for the SRF's lifetime
   QueryDesc *qd = CreateQueryDesc(plan, sourceText, snap, InvalidSnapshot, dest, NULL, queryEnv, 0);
   ```
   and `UnregisterSnapshot(snap)` in the teardown, after `ExecutorEnd`. This keeps the exact
   snapshot the child scan reads alive regardless of what the surrounding plpgsql block does to the
   active-snapshot stack.
2. **Push/pop the active snapshot around each drive.** Wrap each `ExecProcNode` drive of the child
   scan with `PushActiveSnapshot(qd->snapshot)` / `PopActiveSnapshot()` (mirroring core's
   `postquel_*` path in `functions.c`), so any code in the scan that consults the *active* snapshot
   (not just `scan->xs_snapshot`) sees the operator's snapshot, not whatever the sibling statement
   left on top.
3. **Fix the teardown UAF in `topk.cpp` / `multicol_topk.cpp`.** Reorder so `free(state)` is the
   last statement and every `new`-allocated member is `delete`d (not `free`d), matching the already-
   correct `EndTJSState`. Also free/`delete` `state->children` (currently leaked in `topk`).

Mitigation already in place (do **not** remove): the canonical e2e test
(`test/canonical_e2e_test.sql`) and CI suites **avoid co-issuing a second scan in the same
early-termination block**. That keeps v1 green without the fix; the fix removes the foot-gun so that
real workloads (and future generated plpgsql) are safe.

A DRAFT patch implementing (1)-(3) is in
`scripts/patches/tridb_fix_double_scan_snapshot.patch` — **UNBUILT**, with a verify-wiring sketch.

---

## Verify wiring (to run ON THE GX10 / on a built `tridb/msvbase:dev` image — NOT here)

1. **Confirm the crash first (baseline):** run `test/_fork_bug_tjs_double_scan.sql` Shape A1 against
   the **unpatched** image; expect the SIGSEGV. Run Shape B1 to record whether an *unrelated*-table
   sibling scan also crashes (discriminates snapshot-stack vs own-relation aliasing). Capture the
   server log line + the failing process.
2. **Apply the draft patch**, rebuild via `scripts/gx10build.sh` (GX10) or
   `scripts/x86build.sh --docker` (x86 standin image).
3. **Re-run Shapes A1/A2/B1/C1.** Expect each `DO` block to print its `… SURVIVED` NOTICE (no
   backend termination) and the operator result set to be unchanged vs the non-sibling-scan path
   (compare against `test/canonical_e2e_test.sql` / `test/trimodal_compose.sql` expected sets).
4. **Regression:** `make test-all` (smoke + graph + canonical) must stay green — the snapshot
   change must not alter top-k results or early-termination counts (`tjs_candidates_examined()` <<
   corpus, SM-3).
5. If green, promote a *minimized* survivor block into a real CI test (renamed without the leading
   underscore) so the fixed shape is guarded going forward.

**Do not claim this compiles, passes, or is fixed until steps 1-5 run on a built image.**

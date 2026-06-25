# ADR-0010: Fork operators must own their snapshot for the full SRF lifetime

**Status:** Accepted — **BUILT AND VERIFIED on the x86 standin** (2026-06-25); GX10/ARM sign-off tabled
**Issue:** DEV-1236 (spike / diagnose)
**Related:** ADR-0007 (TJS operator), ADR-0006 (relaxed-monotonicity vector iterator),
DEV-1169 (TJS), DEV-1168 (vector iterator), `docs/fork_segfault_double_scan.md` (full evidence chain),
`docs/fork_findings.md`

## Context

`topk()`, `multicol_topk()`, and `tjs()` are C set-returning functions that build a child HNSW
`IndexScan` via SPI and **drive it across multiple SRF per-call invocations** (the validated
`execFagins` Fagin-merge — the mandated v1 architecture, ADR-0007). A long-known fork bug: issuing a
**second executor-driven scan in the same plpgsql block** as one of these operators segfaults the
backend (recorded in ADR-0007 §Consequences and `docs/fork_findings.md`; attribution to the fork —
not to TJS — proved with unmodified `multicol_topk` in `test/_fork_bug_multicol_double_scan.sql`).

DEV-1236 is the diagnosis spike. Reading the vendored C lifecycle establishes the root cause.

### Root cause (from source, `docs/fork_segfault_double_scan.md`)

The operators call `CreateQueryDesc(plan, …, GetActiveSnapshot(), InvalidSnapshot, …)` **once** at
first-call and then pull the child scan on every later SRF re-entry. They never `RegisterSnapshot`
the captured snapshot and never `PushActiveSnapshot` around the drive. They borrow the **caller's**
active snapshot and assume it stays valid for the operator's extended, multi-call lifetime.

Inside a plpgsql block each statement is its own SPI execution that pushes/refreshes the active
snapshot (`pl_exec.c` `exec_run_select` / line 6354 `PushActiveSnapshot(GetTransactionSnapshot())`).
A sibling scan therefore pops/displaces the very snapshot the operator's still-open child
`IndexScan` reads (`scan->xs_snapshot`) for MVCC visibility on each fetched tid. The next
`ExecProcNode` dereferences freed snapshot memory → `SIGSEGV`. This is precisely the issue's
hypothesized "scan-descriptor / memory-context / resource-owner lifecycle collision when a second
scan opens in the same block." It explains every trigger condition (only in plpgsql; only with a
sibling scan; not back-to-back operator calls — see the doc).

A **second, independent defect** confirmed by inspection: `topk.cpp`/`multicol_topk.cpp` teardown
has a use-after-free — `free(state);` then `free(state->qDescs);` (reads `state->` after freeing it),
plus `free()` on `new`-allocated `std::vector`s. `tjs_operator.cpp`'s `EndTJSState` already avoids
this, which is why `tjs()` still crashes on the **snapshot** cause but not the UAF — confirming the
snapshot lifecycle is the primary, shape-specific cause.

## Decision

**Fork operators that drive an SPI child scan across SRF re-entries MUST own the snapshot for that
entire lifetime.** Concretely (the contract the DRAFT patch implements):

1. **Pin the snapshot.** First-call takes `RegisterSnapshot(GetTransactionSnapshot())` and passes
   *that* to `CreateQueryDesc`; teardown `UnregisterSnapshot`s it after `ExecutorEnd`. The operator
   no longer depends on the caller's active-snapshot stack staying put.
2. **Re-establish the active snapshot per drive.** Wrap each child-scan drive in
   `PushActiveSnapshot(qd->snapshot)` / `PopActiveSnapshot()` (a `PG_TRY/PG_FINALLY` for the error
   path), mirroring core's `postquel_*` executor path in `functions.c`.
3. **Fix the teardown UAF** in `topk.cpp`/`multicol_topk.cpp`: `free(state)` must be the LAST
   statement and every `new`'d member must be `delete`d (matching the already-correct `EndTJSState`).

This keeps the v1 architecture intact: it does NOT change the operator surface, the single-stream
top-k, early termination (TR-1), or the SRF-now/CustomScan-later seam (ADR-0007). It is a lifecycle
correctness fix inside the existing `execFagins` design — no golden rule is touched (still one
process / one txn / native graph / one canonical query / three stores).

### Why not just keep the test-level workaround

The CI workaround stands today (`test/canonical_e2e_test.sql` deliberately does not co-issue a
sibling scan in the early-termination block). That is acceptable for v1 green but is a foot-gun: the
DEV-1167 lowering can emit plpgsql, and real workloads will mix scans with operator calls. Owning
the snapshot removes the latent crash rather than routing around it. Per CLAUDE.md golden rule
"delete old code paths / don't keep silent fallbacks," the real fix is preferred once it can be
built and verified.

## Status / gating

**BUILT AND VERIFIED on the x86 standin (2026-06-25).** Incremental build of `tridb/msvbase:dev`
(only the three changed TUs), build exited 0. Smoke test PASS. Canonical TJS e2e ALL TESTS PASSED;
`examined=73 of 2000` (TR-1/SM-3 intact). Double-scan SURVIVED notices confirmed for shapes A1
(multicol_topk + count(*)), A2 (tjs + count(*)), C1 (tjs then count(*)). Server alive after all.

Patch wired into `scripts/lib/msvbase_patches.sh` — sentinel `DEV-1236` in all three `.cpp` files.

**GX10/ARM sign-off tabled.** Snapshot logic is architecture-independent PG 13.4 C++ (no
GX10-specific codepaths). GX10 is required only to build the full native HNSW/graph layer;
sign-off there tracks with the GX10 Phase build, not this fix specifically.

**NOTE:** `test/_fork_bug_multicol_double_scan.sql` and `test/_fork_bug_tjs_double_scan.sql`
contain `SET enable_seqscan = off` globally, which causes `SELECT count(*) FROM entities` to crash
for an independent reason (HNSW cannot handle plain count(*) without ORDER BY). These files are
not the canonical BEFORE/AFTER witness for this fix. Correct repro: sibling scans with seqscan
enabled (default). See `docs/fork_segfault_double_scan.md` §Verification results.

## Consequences

- **Removes the double-scan SIGSEGV** once verified — operator calls become composable with sibling
  scans in plpgsql (and with generated plpgsql from the DEV-1167 lowering).
- **Slightly different snapshot semantics:** the operator runs against a freshly registered
  `GetTransactionSnapshot()` rather than whatever active snapshot the caller had. For the canonical
  read-only query this is equivalent; documented so a future non-read-only caller is aware.
- **Three files change identically** (the shared `execFagins` lineage). A patch-sentinel grep
  (`DEV-1236`) should be added to `scripts/lib/msvbase_patches.sh` so a silent drift fails the build
  (existing `verify_patches` convention).
- **The fix belongs to the fork's executor-driving lifecycle** (GX10-adjacent), exactly as ADR-0007
  predicted ("a separate hardening task"). This ADR is that task's design record.

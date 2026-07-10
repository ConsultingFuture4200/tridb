# Plan 061: Abort-safe Relation cleanup for the graph traversal SRFs (fixes 049's leak)

> **Executor instructions**: Native graph-store C (GX10/engine-gated build). Author + build + run
> `make graph-test` in the `tridb/msvbase:dev` image. Honor STOP conditions. Update
> `advisor-plans/README.md` when done.
>
> **Drift check**: `git diff --stat e550e05..HEAD -- src/graph_store/graph_am.c test/`

## Status
- **Priority**: P2 (correctness/hygiene; not a data-loss bug, but a real resource leak + abort-path hazard)
- **Effort**: M–L
- **Risk**: MED–HIGH (touches transaction-abort cleanup; wrong fix escalates errors *during* abort)
- **Depends on**: plan 049 (`advisor/049-hold-relation-across-next`, tip `7394b2d`) — this plan fixes
  the leak that plan's change introduced. Start from that branch, or fold both into one PR.
- **Category**: correctness / graph AM
- **Planned at**: commit `e550e05`, 2026-07-10

## Why this matters

Plan 049 made the four graph traversal SRFs — `gph_neighbors`, `gph_neighbors_ext_cached`,
`gph_traverse`, `gph_traverse_typed` — open `scan->rel` once (AccessShareLock) and hold it across all
`Next()` calls, closing it only on the drained `SRF_RETURN_DONE` path. An **early-abandoned** scan
(a `LIMIT` that stops before the SRF is exhausted, or an error mid-drain) never reaches `DONE`, so the
Relation reference is **leaked**. The engine-verification run confirmed **3 real
`relcache reference leak: relation "gstore" not closed` WARNINGs** on the early-abandon paths
(`main.sql:63` gph_neighbors, `trav.sql:68` gph_traverse, `typed.sql:145` gph_traverse_typed). The
`make graph-test` suite is green *only* because `ON_ERROR_STOP` ignores WARNINGs — the leak is real.

Left unfixed this is: (a) a per-abandoned-scan relcache-ref leak (unbounded under a workload that
does many `LIMIT`ed graph queries), and (b) a latent **refcount-underflow / error-during-abort** hazard
once someone adds a naive cleanup (see below). This is FR-7 / resource-hygiene territory for a
long-lived backend (the gBrain use case runs exactly this pattern).

## The trap (why the obvious fix is wrong)

The naive fix — `MemoryContextRegisterResetCallback` on the SRF's `multi_call_memory_ctx` that calls
`relation_close(rel, AccessShareLock)` — is **correct on a normal-commit early-abandon** (the per-query
context is torn down during portal cleanup, before the commit-time `ResourceOwnerRelease`), but is
**UNSAFE on transaction abort**: abort runs `ResourceOwnerRelease(RESOURCE_RELEASE_LOCKS/BEFORE_LOCKS)`
and releases the relcache ref **before** the multi-call context is deleted. The reset callback then
calls `relation_close` on an already-forgotten ref →
`ResourceOwnerForgetRelationRef` raises `elog(ERROR)` **during abort cleanup** (error-in-error
escalation) and underflows the relcache refcount in assert-less production builds. So any fix MUST be
abort-path-aware, and the suite currently has **no** graph-SRF abort/interrupt leak test to catch a
regression.

## Current state

- `src/graph_store/graph_am.c`: the 4 SRFs open `scan->rel = relation_open(..., AccessShareLock)` in the
  `SRF_IS_FIRSTCALL()` block (or first `Next()`), store it on the scan/funcctx state, and
  `relation_close(scan->rel, AccessShareLock)` only in the `SRF_RETURN_DONE` arm. Read plan 049 and the
  `7394b2d` diff for the exact struct field + close sites.
- No `PG_TRY/PG_CATCH`, no `MemoryContextRegisterResetCallback`, no `RegisterXactCallback`, and no
  ResourceOwner callback anywhere in `graph_am.c` today — there is **no existing cleanup convention to
  mirror** (this is why plan 049's executor correctly STOPPED rather than guess).

## Approach — pick ONE, justify it, and TEST all three exit paths

Evaluate and choose (the executor decides on merit; option A is the recommended default):

- **Option A (recommended): don't hold the Relation across `Next()`.** Revert to opening the store per
  `Next()` (or better: keep only the `Oid`/`Relation` pointer WITHOUT a long-lived pin+lock ref — the
  enclosing query already holds the lock for the statement's lifetime, so re-deriving the open relation
  each call is cheap and cannot leak). This trades plan 049's micro-optimization (one open vs N) for
  guaranteed correctness. Measure the cost: if `gph_neighbors`/traverse are not hot enough for the extra
  `relation_open` per row to matter (they were a readability/allocation tidy, not a measured win), this
  is the right call and the simplest safe fix.
- **Option B: keep the held ref, add an abort-safe reset callback.** Register a
  `MemoryContextRegisterResetCallback` on the multi-call context that closes the ref, BUT make it a
  no-op when the ref has already been released — e.g. guard on a flag the SRF clears in its own
  `SRF_RETURN_DONE`/error path, and additionally arm a `RegisterXactCallback`/`RegisterSubXactCallback`
  that, on `XACT_EVENT_ABORT`/`SUBXACT_EVENT_ABORT_SUB`, marks the ref as "already released by the
  ResourceOwner" so the later reset callback skips `relation_close`. This is the "correct but fiddly"
  path; only take it if a measurement shows Option A's per-call open is a real regression.

Whichever is chosen, the invariant to preserve: **the store lock/ref is released exactly once on every
exit path — full drain, early-abandon (LIMIT), and transaction abort — with no `relation_close` after
the ResourceOwner has already forgotten the ref.**

## Steps

### Step 1: Write the failing abort/abandon leak tests FIRST
Add to `test/` (wire into `AM_TESTS`) three scenarios that currently emit the leak WARNING:
1. **Early-abandon (LIMIT):** `SELECT ... FROM gph_neighbors(<hub>) LIMIT 1;` (and the traverse/typed
   variants) — stops before drain.
2. **Error mid-drain:** force an error partway through the SRF (e.g. a `LIMIT` inside a CTE that also
   raises, or a `1/0` in the target list on the 2nd row) so the scan is abandoned via ERROR, not DONE.
3. **Transaction abort:** run one of the above inside a `BEGIN; ... ; ROLLBACK;` and then, in the SAME
   session, run a trivial query to prove the backend is healthy (no refcount underflow, no
   error-in-abort).
Make the test **fail loudly on the leak**: capture the postmaster/psql stderr and assert the string
`relcache reference leak` does **NOT** appear (the current suite ignores WARNINGs — this test must not).
A driver that greps the server log for `relcache reference leak` after the scenario is the reliable
signal; ON_ERROR_STOP alone won't catch a WARNING.

**Verify**: on the unpatched `7394b2d` code these tests FAIL (the WARNING appears). If they don't
reproduce the leak, STOP — the repro is wrong, fix it before touching the fix.

### Step 2: Implement the chosen cleanup
Apply Option A or B in `graph_am.c` for all four SRFs. Keep the change symmetric across the four —
they share the traversal region (plans 037/038/047/049 all touch it), so factor the open/close through
one helper if practical rather than four copies.

**Verify**: `make graph-test` exit 0 AND the Step-1 tests pass (no `relcache reference leak` on any of
the three paths). Run the pre-existing tombstone/traverse/typed suites too — the fix must not change
traversal results.

### Step 3: Confirm no perf cliff (only if Option A)
If you reverted to per-`Next()` open, sanity-check it isn't a hot-path regression: a deg-N hub
`gph_neighbors` scan does N opens instead of 1. If `graph-test`'s existing timing is unaffected, note it
and move on; if there's a measurable cliff on a large-degree hub, reconsider Option B.

## Test plan
- The three new abort/abandon leak scenarios (Step 1), wired into `AM_TESTS`, each asserting the absence
  of `relcache reference leak` in the server log.
- Re-run the existing graph delete/traverse/typed suites unchanged — same results.

## Done criteria
- [ ] New tests reproduce the leak on `7394b2d` and PASS after the fix (all three: LIMIT, error, abort)
- [ ] `make graph-test` exit 0 with zero `relcache reference leak` WARNINGs in the server log
- [ ] No `relation_close` after ResourceOwner release (no error-in-abort, no refcount underflow) —
      proven by the abort scenario + a healthy follow-up query in the same session
- [ ] Traversal results unchanged (existing suites green)
- [ ] Index row DONE

## STOP conditions
- Option B's xact-callback coordination turns out to need touching frozen core / more than the four SRFs
  → fall back to Option A (per-call open) and note the perf tradeoff; do NOT hand-roll ResourceOwner
  internals.
- The leak does not reproduce in your Step-1 test → the repro is wrong; fix it before claiming a fix
  (a green suite over a non-reproducing test proves nothing — this is exactly how 049 shipped the leak).

## Maintenance notes
- This region (`graph_am.c` traversal SRFs) is touched by plans 037 (tombstone), 038 (typed), 047
  (cached identity), 049 (this ref) — any future change that opens a relation/buffer across `Next()`
  must honor the same "released exactly once on every exit path" rule. Consider a one-line comment at the
  open site documenting the abort-path hazard so the next author doesn't re-introduce it.
- When this lands, reconcile plan 049's index row: its code is fine to keep *only* with this fix on top;
  do not merge 049 alone.

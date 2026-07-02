# Plan 022: Harden the relaxed-monotonicity executor path — zero-init xs_inorder, gate the Sort early-stop and the removed wrong-order guard on amcanrelaxedorderbyop, and document/parameterize the emission-window constants (issue #22)

> **Executor instructions**: This plan touches the CORE of TriDB's early-termination thesis. Follow
> step by step; run every verification command. Stop and report on any "STOP condition". Treat
> recall regressions as findings, not things to tune away. Update `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `cd vendor/MSVBASE && git show HEAD:patch/Postgres.patch | grep -n "is_index_inorder\|tuplesort_heapfull\|xs_inorder" ; git show HEAD:src/hnswindex.cpp | grep -n "range = 86\|distanceThreshold\|queueThreshold"`

## Status

- **Priority**: P2 (strategically central; sequenced after the cheaper P1 crash/OOB fixes)
- **Effort**: M
- **Risk**: MED (executor-core behavior + relaxed-order semantics; recall/latency sensitive)
- **Depends on**: none, but do NOT combine with plan 018 (018 removes the *string-rewriter* hunk of
  Postgres.patch; this hardens the *executor* hunk of the same patch — keep them separate)
- **Category**: bug / correctness
- **Planned at**: commit `408e852`, 2026-07-01
- **Upstream**: microsoft/MSVBASE `patch/Postgres.patch` (nodeSort/nodeIndexscan/execnodes), `src/hnswindex.cpp` (emission window). Corroborated by upstream issue #22.

## Why this matters

TriDB's entire efficiency thesis (TR-1 early termination) rides on the VBASE relaxed-monotonicity
scan emitting candidates in *approximately* increasing distance, with the operator's
`consecutive_drops`/`term_cond` deciding sufficiency. Three inherited weaknesses make that
foundation fragile:

1. **`xs_inorder` is a new, un-zeroed IndexScanDescData field.** `Postgres.patch:199` adds
   `bool xs_inorder;` to the descriptor; stock AMs never set it and PG's descriptor allocation
   doesn't zero it, so it holds garbage for any non-relaxed scan. `Postgres.patch:44,54` copies it
   into a single per-query `EState` flag `is_index_inorder`, and `Postgres.patch:92-95` uses that in
   `nodeSort.c` to **break a bounded Sort early** (`if (estate->is_index_inorder &&
   tuplesort_heapfull(...)) break;`). A bounded `Sort` (LIMIT) over an ordinary index scan can read
   garbage/stale `xs_inorder` and truncate its input → wrong top-N for queries unrelated to vector
   search. (UP-PATCH-07)
2. **The wrong-order safety ERROR is removed unconditionally.** `Postgres.patch:66-67` comments out
   `if (cmp < 0) elog(ERROR, "index returned tuples in wrong order")` in `nodeIndexscan.c` for ALL
   order-by-op scans, not just relaxed ones — so a genuinely buggy AM yields silent wrong results.
   (UP-PATCH-07)
3. **Emission-order termination rests on unparameterized magic constants.** `src/hnswindex.cpp`
   sets `scanState->range = 86` (the ORDER BY window; :300), `distanceThreshold = 3`,
   `queueThreshold = 50` (:310-311), all `//TODO(Qianxi): set parameter` — dataset/k/ef-blind. And
   the library's `searchBaseLayerSTIterative` (`patch/hnsw.patch`) maintains no result heap and no
   VBASE median-window termination; it emits in raw frontier-pop order (upstream issue #22). For k
   larger than the window or in dense regions the `xs_inorder` flip can fire prematurely. (UP-CORE-01/02)

The good news (verified, do not "fix"): the pathkey question is handled correctly —
`create_ordered_paths` forces `is_sorted=false; presorted_keys=0` for a relaxed index
(`Postgres.patch:107-135`), so the planner keeps an explicit Sort (the pgvector-#862 failure mode is
avoided). The residual risk is the fragile early-stop, not the pathkey advertisement.

## Current state

- The executor hunks are in the applied `Postgres.patch` (present post-`bash scripts/patch.sh` in
  `vendor/MSVBASE/thirdparty/Postgres/src/backend/executor/{nodeSort.c,nodeIndexscan.c}` and
  `src/include/nodes/{execnodes.h,relscan.h}`). The window constants are in the TriDB-patched
  `src/hnswindex.cpp` (86/3/50, confirmed on-disk at lines 300/310/311).
- ADR-0007 (`docs/decisions/0007-tjs-operator.md`) already records that this relaxed-order approach
  is slated for a CustomScan migration — this plan is interim hardening, not that migration.
- `tuplesort_heapfull` (`Postgres.patch:159-162`) reaches into private `Tuplesortstate` fields
  (`memtupcount >= bound`) — correct on REL_13_4, a hard internal coupling (UP-PATCH-10); this plan
  does not change it, only gates its use.
- Fix mechanism: a TriDB fork patch under `scripts/patches/` editing the applied executor + the
  window constants, wired with a sentinel; fast gate `bash scripts/ci_check_patches.sh`; correctness
  gate is the engine recall suites (`make graph-test`, and the neon sweep for the curve).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Patch chain applies | `bash scripts/ci_check_patches.sh` | exit 0 |
| Python layer | `make test && make lint` | exit 0 |
| Engine recall suites (gated) | `scripts/x86build.sh --docker && make graph-test` | PASS, recall unchanged/better |
| Recall/latency curve (gated) | `make sweep` (or the neon sweep target) | curve within TR-1 ceiling |

## Scope

**In scope**:
- `scripts/patches/tridb_relaxed_order_executor_guard.patch` (create)
- `scripts/lib/msvbase_patches.sh` (apply block + sentinel)
- `docs/decisions/0007-tjs-operator.md` (addendum documenting the guard + the window-constant
  parameterization decision)
- `test/relaxed_order_guard.sql` (create — a bounded Sort over an ORDINARY btree index returns the
  full correct top-N; a wrong-dim/edge relaxed scan doesn't silently truncate) + wire into a suite
- `advisor-plans/README.md` (status row)

**Out of scope**:
- The full CustomScan migration (ADR-0007's eventual path) — this is interim hardening only.
- The PostgresMain string-rewriter (plan 018).
- Rewriting `searchBaseLayerSTIterative` to add a VBASE median-window terminator (that is a HIGH-risk
  library change; this plan documents the gap and parameterizes the operator-side constants instead —
  see Step 3).

## Git workflow

- Branch: `advisor/022-relaxed-order-guard` from `origin/master`
- Commits per step, e.g. `fix(fork): gate relaxed-order Sort early-stop on amcanrelaxedorderbyop (advisor plan 022)`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Zero-initialize xs_inorder and gate the early-stop per-scan

In the patch: at `index_beginscan` (or wherever the IndexScanDesc is allocated in the applied tree),
set `scan->xs_inorder = false;`. Then change the `nodeSort.c` early-break so it fires only when the
driving index scan's AM actually advertises relaxed order — i.e. gate on
`amcanrelaxedorderbyop` for THAT scan, not the single global `is_index_inorder` EState bool. If the
cleanest expression is to keep the bool but only ever set it true when the scan's AM is relaxed,
do that (set `estate->is_index_inorder = scandesc->xs_inorder && scandesc->indexRelation->rd_indam->amcanrelaxedorderbyop;`).

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 2: Restore the wrong-order guard for non-relaxed scans

In `nodeIndexscan.c`, re-enable the `if (cmp < 0) elog(ERROR, "index returned tuples in wrong
order")` check, guarded to run for scans whose AM is NOT `amcanrelaxedorderbyop` (relaxed scans
legitimately violate strict order; stock AMs must not). This restores the safety net for
non-relaxed order-by-op scans while preserving relaxed monotonicity for HNSW.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0.

### Step 3: Parameterize (or at minimum document) the emission-window constants

Two acceptable outcomes — pick based on effort budget, document which in the ADR addendum:
(a) Preferred: derive `range`/window and `distanceThreshold`/`queueThreshold` in `hnswindex.cpp`
from `k`/`ef`/reloptions instead of the literals 86/3/50, exposing them as index reloptions or GUCs
with the current values as defaults (no behavior change at defaults).
(b) Minimum: leave the constants but add a code comment + an ADR-0007 addendum documenting that
emission order is only approximately monotone, that `term_cond` is the real correctness knob (per
the TriDB TJS termination memory), and that the 86-window is a fixed heuristic — so no future reader
mistakes it for a tuned bound. Reference upstream issue #22.

**Verify**: `bash scripts/ci_check_patches.sh` → exit 0; ADR addendum present.

### Step 4: Regression test

`test/relaxed_order_guard.sql`: (a) `SELECT ... ORDER BY <non-vector col> LIMIT n` over a plain
btree index returns the exact correct top-n (proving the early-stop no longer truncates a
non-relaxed bounded Sort); (b) an HNSW `ORDER BY v <-> q LIMIT k` still returns the expected
near set (relaxed path unbroken). Wire into `ENGINE_TESTS`.

**Verify**: `make -n graph-test | grep relaxed_order_guard` present; engine-gated live run if image
exists — **if HNSW recall drops, STOP and report the numbers; do not loosen the guard to recover it.**

## Test plan

- `test/relaxed_order_guard.sql` covers both directions (non-relaxed exactness restored; relaxed
  path preserved).
- The neon recall/latency sweep (gated) must show recall unchanged or better at the same
  `term_cond` operating point.
- `bash scripts/ci_check_patches.sh` + `make test && make lint` green.

## Done criteria

- [ ] `xs_inorder` zero-initialized; the Sort early-stop gated on `amcanrelaxedorderbyop`
- [ ] Wrong-order ERROR restored for non-relaxed order-by-op scans
- [ ] Window constants parameterized OR documented (ADR-0007 addendum + code comment; issue #22 cited)
- [ ] `test/relaxed_order_guard.sql` wired; engine recall PASS (unchanged/better) or "engine-gated:
      unbuilt here"
- [ ] `bash scripts/ci_check_patches.sh`, `make test && make lint` exit 0; `git status` clean
- [ ] `advisor-plans/README.md` row updated

## STOP conditions

- Gating the early-stop measurably raises HNSW query latency (the global bool may have been doing
  useful work on the relaxed path) — report the latency delta; do not revert the correctness gate to
  chase latency.
- HNSW recall drops at the fixed `term_cond` operating point after the change — report; the window
  interaction needs analysis, not a constant tweak.
- The `nodeSort`/`nodeIndexscan` hunks are entangled with the string-rewriter such that plan 018 and
  this plan conflict — coordinate; land one, rebase the other.

## Maintenance notes

- This is interim hardening; the real fix is the ADR-0007 CustomScan migration (relaxed order
  expressed as a proper path, no global executor bool). Reviewer should confirm this doesn't make
  that migration harder.
- The `tuplesort_heapfull` private-field coupling (UP-PATCH-10) is unchanged but now only reached on
  genuinely-relaxed scans — note it as the load-bearing reason a PG re-pin needs full re-validation.
- Corroborating external context: upstream issue #22 (VBase iterative-search) and the TriDB TJS
  termination memory (term_cond is the recall knob; report SM-4 as a curve).

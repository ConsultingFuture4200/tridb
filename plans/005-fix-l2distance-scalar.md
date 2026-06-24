# Plan 005: Fix MSVBASE's scalar l2_distance (returns 0 outside the index scan)

> **Executor instructions**: This is a SPIKE + PATCH plan — investigate first, then patch. Run
> every verification command. On a STOP condition, stop and report. Update this plan's row in
> `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- test/fork_distance_probe.sql docs/fork_findings.md scripts/x86build.sh`
> If changed, compare excerpts below first; mismatch = STOP.

## Status
- **Priority**: P2
- **Effort**: M (investigation may reveal it is S or L — see STOP conditions)
- **Risk**: MED (touches a vendored C operator via the patch layer)
- **Depends on**: plans/003 (the patch-verification layer this fix plugs into)
- **Category**: bug (fork) / direction
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
MSVBASE's scalar vector distance is broken: `l2_distance(float8[], float8[])` and the `<->`
operator return **0 for every input** when evaluated as a scalar (outside an HNSW index scan) —
confirmed for fractional and integer vectors, with explicit casts, by
`test/fork_distance_probe.sql`. Real distances exist only inside the index scan. This blocks:
SQL-level exact-correctness tests (no ground-truth re-rank possible), and it forces the future
relaxed-monotonicity operator (DEV-1168) to read index-internal distances rather than reusing a
scalar finalize. Fixing it unblocks correctness testing across the project and de-risks DEV-1168.

## Current state
- `docs/fork_findings.md` §2 documents the bug as a "confirmed-by-probe fork bug pending a
  root-cause patch in `l2_distance`'s C implementation."
- `test/fork_distance_probe.sql` ASSERTS the *broken* behavior today (it passes when
  `count(distinct l2_distance(...)) = 1`, i.e. constant). After the fix, the probe must be
  flipped to assert the CORRECT behavior.
- The SQL binding: `vendor/MSVBASE/sql/vectordb.sql:32` —
  `CREATE FUNCTION l2_distance(float8[], float8[]) RETURNS float8 … ;` with
  `CREATE OPERATOR <-> (… PROCEDURE = l2_distance …)`.
- The C implementation lives in `vendor/MSVBASE/src/operator.cpp` (and possibly
  `src/lib.cpp`) — `grep -n 'l2_distance' vendor/MSVBASE/src/*.cpp` to locate the function body.
- Patches to the vendored fork are applied through the build scripts' patch layer (see
  `vendor/MSVBASE/patch/*.patch` and `scripts/x86build.sh`'s `apply_msvbase_patches`). A fix
  should ship the same way: a new patch file + a line in the patch flow, NOT a hand-edit of the
  vendored tree that gets lost on re-clone.
- Build/test: `scripts/x86build.sh --docker` rebuilds the image;
  `bash scripts/graph_test.sh tridb/msvbase:dev test/fork_distance_probe.sql` runs the probe.

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Locate the impl | `grep -n 'l2_distance\|L2\|VectorDistance' vendor/MSVBASE/src/operator.cpp` | function body |
| Rebuild image (heavy) | `scripts/x86build.sh --docker` | builds `tridb/msvbase:dev` |
| Run the probe | `bash scripts/graph_test.sh tridb/msvbase:dev test/fork_distance_probe.sql` | see below |
| Diff for a patch file | `git -C vendor/MSVBASE diff -- src/operator.cpp > /tmp/l2.patch` | a unified diff |

## Scope
**In scope**: a new patch file under `vendor/MSVBASE/patch/` (e.g. `l2_distance_scalar.patch`),
its wiring into `scripts/lib/msvbase_patches.sh` (or `scripts/x86build.sh` if plan 004 hasn't
landed), `test/fork_distance_probe.sql` (flip the assertion), `docs/fork_findings.md` (update
finding #2 once fixed).
**Out of scope**: the graph store, the compositions, the planner. Do NOT hand-edit the vendored
`src/operator.cpp` as a permanent change — it must be a patch that re-applies on clone.

## Git workflow
- Branch `advisor/005-fix-l2distance`. Commit the investigation notes, then the patch, then the
  flipped probe. Conventional commit (`fix(fork): real scalar l2_distance`).

## Steps

### Step 1: Root-cause the scalar path
Read `vendor/MSVBASE/src/operator.cpp`'s `l2_distance` function. Determine why it returns 0
outside an index scan. Likely candidates (confirm which): the function reads its distance from
some index/scan-state global that is unset in a plain scalar call; an early return when no
HNSW scan context is present; or it computes into a result that is discarded. Write 3-6 lines
of findings into the branch commit message or a scratch note. Compare against the index path
(how the HNSW scan computes the real distance) to see the intended computation.
**Verify**: you can state, in one sentence, why the scalar path yields 0.

### Step 2: Write the minimal fix as a patch
Make `l2_distance(a, b)` compute the actual L2 distance between the two `float8[]` arguments
directly (deconstruct both arrays, sum of squared differences, sqrt) when called as a scalar,
independent of any index scan state. Capture the change as a patch file:
`git -C vendor/MSVBASE diff -- src/operator.cpp > vendor/MSVBASE/patch/l2_distance_scalar.patch`
(adjust path to the real file). Keep it minimal and guard against dimension mismatch (error,
don't silently truncate).
**Verify**: the patch applies cleanly on a fresh checkout:
`git -C vendor/MSVBASE stash && git -C vendor/MSVBASE apply --check patch/l2_distance_scalar.patch && echo applies`.

### Step 3: Wire the patch into the build flow
Add the new patch to the patch layer so it applies after the upstream `patch.sh`
(`scripts/lib/msvbase_patches.sh` if plan 004 landed, else `apply_msvbase_patches` in
`scripts/x86build.sh`). Add a sentinel to `verify_patches` (plan 003) proving it applied.
**Verify**: `bash -n` the script(s); re-read shows the new patch applied + verified.

### Step 4: Rebuild and flip the probe
Rebuild: `scripts/x86build.sh --docker`. Then update `test/fork_distance_probe.sql` so it now
asserts the CORRECT behavior — `count(distinct l2_distance(embedding, ARRAY[10,0,0]::float8[]))`
> 1, and the four known distances to `[10,0,0]` are `{10, 9, 5, 0}` for ids `{1,2,3,4}`. Keep
the index-path section.
**Verify**: `bash scripts/graph_test.sh tridb/msvbase:dev test/fork_distance_probe.sql` →
prints a PASS line for the now-working scalar and exits 0.

### Step 5: Update fork_findings.md
Change finding #2 from "confirmed fork bug" to "FIXED by `patch/l2_distance_scalar.patch`" with
the date and a one-line root cause. Note the downstream unblock for DEV-1168.
**Verify**: `grep -n 'FIXED' docs/fork_findings.md` → match.

## Test plan
- Flip `test/fork_distance_probe.sql` to assert correct scalar distances (Step 4) — this is the
  regression test for the fix.
- Optionally extend `test/trimodal_early_term.sql`: now that scalar distance works, the
  over-fetch+finalize re-rank (which the file's comments say is impossible) becomes testable —
  but that is a follow-up, not required here.
- Run the full engine suite (`make graph-test`, plan 001) to confirm nothing else regressed.

## Done criteria
- [ ] You can state the root cause of the scalar-returns-0 behavior in one sentence.
- [ ] A patch file under `vendor/MSVBASE/patch/` makes scalar `l2_distance` compute real
      distances; it applies cleanly on a fresh checkout.
- [ ] The patch is wired into the build flow and covered by a `verify_patches` sentinel.
- [ ] `test/fork_distance_probe.sql` asserts the corrected behavior and passes against a
      freshly built image.
- [ ] `docs/fork_findings.md` finding #2 marked FIXED.
- [ ] `plans/README.md` status row updated.

## STOP conditions
- The scalar path returns 0 by **deliberate design** (e.g. MSVBASE routes all distance through
  the index and the scalar is an intentional stub that other code relies on returning 0). STOP
  and report — a "fix" could break the index path; this becomes a design decision, not a bug fix.
- The fix requires changes across multiple files / the executor/planner integration (effort L,
  not M). STOP and report scope before writing a large patch.
- The rebuild + probe shows the index-path distance ordering REGRESSED after the patch. STOP and
  revert — the scalar and index paths likely share code; the fix must not touch the index path.

## Maintenance notes
- This patch must be re-validated whenever MSVBASE is re-pinned (plan 002).
- Reviewer: scrutinize that the patch touches ONLY the scalar computation, not the HNSW index
  scan's internal distance path.
- Follow-up (deferred): once scalar distance works, revisit `test/trimodal_early_term.sql` and
  the DEV-1168 design — an exact SQL re-rank may now be viable for ground-truth tests.

# Plan 084: Distinguish an initialized SPTAG submodule from its empty placeholder

> **Executor instructions**: This changes build verification, not engine C. Run the clean patch-chain
> checker and shell syntax. Do not run or claim the GX10 build off-target. Skip the advisor index.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- scripts/lib/msvbase_patches.sh scripts/ci_check_patches.sh scripts/x86build.sh scripts/gx10build.sh`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / tests / dx
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

Git leaves an empty directory for a registered but uninitialized SPTAG submodule. The shared patch
verifier treats any directory as initialized, then fails because the optional patch sentinel is not
there. The CI checker works around this by deleting the placeholder, so it no longer tests the real
predicate used by build scripts.

## Current state

- `scripts/lib/msvbase_patches.sh:53-58` checks `[[ -d "$root/thirdparty/SPTAG" ]]` before requiring
  `MultiIndexScan`.
- `scripts/ci_check_patches.sh:62-66` documents the false positive and removes the empty placeholder
  with `rm -rf` before verification.
- `scripts/lib/msvbase_patches.sh:792` already treats
  `thirdparty/SPTAG/CMakeLists.txt` as the load-bearing file for SPTAG patching.
- `scripts/x86build.sh` and `scripts/gx10build.sh` source the shared library; a fix there must serve
  both targets without target-specific duplication.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Syntax | `bash -n scripts/lib/msvbase_patches.sh scripts/ci_check_patches.sh scripts/x86build.sh scripts/gx10build.sh` | exit 0 |
| Focused | `.venv/bin/pytest tests/test_msvbase_patches.py -q` | all pass |
| Patch chain | `bash scripts/ci_check_patches.sh` | final `OK` marker, exit 0 |
| Host | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `scripts/lib/msvbase_patches.sh`
- `scripts/ci_check_patches.sh`
- `tests/test_msvbase_patches.py` (create)

**Out of scope**:
- SPTAG source/patch content, `WITH_SPTAG` default, submodule pins, or CMake behavior.
- Deleting placeholder directories in production build trees as the fix.
- GX10 build sign-off.

## Git workflow

Use assigned `dustin/dev-NNNN`; suggested commit:
`fix(build): detect initialized sptag tree`.

## Steps

### Step 1: Define one shared initialized-tree predicate

Add a side-effect-free `sptag_initialized ROOT` helper to `scripts/lib/msvbase_patches.sh`. It returns
true only when the load-bearing tracked file `ROOT/thirdparty/SPTAG/CMakeLists.txt` exists as a regular
file. A bare directory is false. Use this helper for `verify_patches` and any patch-application branch
that decides whether SPTAG exists; do not duplicate checks.

**Verify**: source the library in a temporary fixture and assert: absent path=false, empty
directory=false, directory with `CMakeLists.txt`=true.

### Step 2: Remove the CI deletion workaround

Delete the placeholder `rm -rf` and its obsolete explanation from `scripts/ci_check_patches.sh`.
Leave the registered, uninitialized submodule placeholder intact so the checker exercises the shared
predicate. Preserve cleanup of the whole temporary clone.

**Verify**: patch-chain checker passes with the empty placeholder present.

### Step 3: Prove initialized-but-unpatched still fails

In a temporary fixture, create `SPTAG/CMakeLists.txt` without the `MultiIndexScan` sentinel and invoke
the verification seam; it must fail with the existing spann-patch error. Then add the sentinel and
confirm success for that check. Do not weaken `verify_patches` to make optional initialized SPTAG skip
verification.

**Verify**: negative fixture nonzero, positive fixture zero; all shell syntax checks pass.

## Test plan

Cover absent, empty placeholder, initialized/unpatched, and initialized/patched states. Run the real
shallow patch-chain checker. Run host tests/lint. Record GX10 build as not run unless actually on the
target.

## Done criteria

- [ ] No SPTAG initialization decision uses directory existence alone.
- [ ] CI checker no longer deletes `thirdparty/SPTAG` to pass.
- [ ] Empty placeholder skips optional patch verification; initialized/unpatched tree fails.
- [ ] Patch-chain checker, syntax, host tests/lint, and diff check pass.

## STOP conditions

- Upstream no longer has `thirdparty/SPTAG/CMakeLists.txt` as a stable tracked initialization marker.
- The helper must run network/submodule mutation to answer the predicate.
- A change reaches GX10-only C or claims target build success.

## Maintenance notes

Use tracked load-bearing files, not directory existence, for future optional submodules. If upstream
restructures SPTAG, update this predicate and its four-state tests together.

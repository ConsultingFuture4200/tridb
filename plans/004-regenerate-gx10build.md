# Plan 004: Regenerate gx10build.sh from the proven recipe (it would fail on the GX10 today)

> **Executor instructions**: Follow step by step; run every verification command. On a STOP
> condition, stop and report. Update this plan's row in `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- scripts/x86build.sh scripts/gx10build.sh`
> If changed, compare excerpts below to live files first; mismatch = STOP.

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: LOW (the script does not work today, so there is little to regress)
- **Depends on**: plans/002 (commit pin), plans/003 (patch verification)
- **Category**: build / bug
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
`scripts/gx10build.sh` is the build script for the **actual target hardware** (the GX10), and
DEV-1160/1161 acceptance depends on it. But it was written *speculatively, before* the real
MSVBASE build was understood through the eight-iteration x86 effort. It would fail on the GX10
in several independent ways:
1. It builds the vectordb extension with `make -C src` (lines 110-113), but MSVBASE builds the
   extension via **CMake at the repo root** (`cd … && mkdir build && cmake .. && make`) — there
   is no `src/Makefile`, so this step fails.
2. It lacks the modern-GCC force-includes (`patch_modern_gcc_includes`), so SPTAG/vectordb fail
   to compile under GCC 12/13 (the `<mutex>` / transitive-include errors the x86 build hit).
3. It relies on `export MSVBASE_DISABLE_SPTAG=1` (line 90) to skip SPTAG — proven ineffective:
   MSVBASE's CMake includes SPTAG regardless of that variable.
4. It never builds Boost (the Dockerfile builds Boost 1.81 from source; a native build assumes
   it present).
5. Its smoke test has the HNSW index creation **commented out** (lines 129-130), so it does not
   actually validate the vector index.

The proven recipe is `scripts/x86build.sh --docker`: it builds via MSVBASE's own Dockerfile and
applies the seven idempotent patches. The only target-specific delta on ARM64 is that the
Dockerfile hardcodes an **x86_64 CMake download** that must become the aarch64 tarball. This
plan rebuilds gx10build.sh on the proven docker recipe + that one extra patch, and factors the
shared patch logic into one library so the two scripts never drift again.

## Current state
- `scripts/gx10build.sh` — native build; the broken parts:
  - lines 88-91: `export MSVBASE_DISABLE_SPTAG=1` (ineffective exclusion).
  - lines 107-113: `make -C src … PG_CONFIG=…` for the extension (no such Makefile).
  - lines 126-132: smoke test with the `CREATE INDEX … USING hnsw` line commented out.
- `scripts/x86build.sh` — the proven recipe. Relevant pieces (already present):
  - `patch_upstream_dockerfile()` (line 53): fixes Boost URL, GID 999, drops `--with-python`.
  - `apply_msvbase_patches()` (line 84): runs `scripts/patch.sh` (relaxed monotonicity).
  - `FORCE_INC` (line 94) + `patch_modern_gcc_includes()` (line 95): modern-GCC force-includes
    into SPTAG's CMakeLists.
  - the `--docker` path (around line 115-122) calls those in order, then `docker build`.
- MSVBASE Dockerfile, line ~59: `wget … cmake-3.14.4-Linux-x86_64.tar.gz` (the x86 download
  that must become `…-linux-aarch64.tar.gz` on ARM; verify the exact line:
  `grep -n 'cmake-.*Linux-x86_64' vendor/MSVBASE/Dockerfile`).
- `scripts/smoke_test.sh` is the working reference for an in-image HNSW smoke test (CREATE
  EXTENSION vectordb → float8[] table → HNSW index → top-k query). Reuse its shape.

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Bash syntax | `bash -n scripts/gx10build.sh && bash -n scripts/x86build.sh && bash -n scripts/lib/msvbase_patches.sh` | exit 0 |
| x86 build still works (heavy, dev box) | `scripts/x86build.sh --docker && bash scripts/smoke_test.sh` | `[smoke_test] PASS` |
| Find the cmake download line | `grep -n 'cmake-.*x86_64' vendor/MSVBASE/Dockerfile` | one match |
| gx10 arch guard | running `scripts/gx10build.sh` on x86_64 | refuses with a clear FATAL |

## Scope
**In scope**: `scripts/gx10build.sh` (rewrite), `scripts/x86build.sh` (refactor to source the
shared lib), `scripts/lib/msvbase_patches.sh` (create), `docs/BUILD_NOTES.md` (note the parity).
**Out of scope**: running on the GX10 (no hardware here); a native (non-Docker) GX10 build path;
`vendor/MSVBASE/`.

## Git workflow
- Branch `advisor/004-regenerate-gx10build`; commits per step (extract lib → refactor x86 →
  rewrite gx10). Conventional commit style. No push unless asked.

## Steps

### Step 1: Extract shared patch logic into `scripts/lib/msvbase_patches.sh`
Move these from `x86build.sh` into a sourceable library (functions only, no top-level
execution): `PIN_COMMIT` default (from plan 002), `FORCE_INC`, `patch_upstream_dockerfile`,
`patch_modern_gcc_includes`, `apply_msvbase_patches`, and `verify_patches` (from plan 003). The
library must not call `set -e` or run anything on source — just define.
**Verify**: `bash -n scripts/lib/msvbase_patches.sh`; `source scripts/lib/msvbase_patches.sh &&
declare -F patch_upstream_dockerfile patch_modern_gcc_includes apply_msvbase_patches verify_patches`
lists all four.

### Step 2: Refactor x86build.sh to source the library
Replace the inlined function definitions in `x86build.sh` with `source "$(dirname "$0")/lib/msvbase_patches.sh"`.
Behavior must be identical.
**Verify**: `bash -n scripts/x86build.sh`; on the dev box, `scripts/x86build.sh --docker` still
ends with a green `scripts/smoke_test.sh` (heavy — if not feasible, at minimum confirm the
`--docker` path still calls the same functions in the same order by re-reading).

### Step 3: Add the aarch64-CMake Dockerfile patch to the library
Add a function (idempotent, grep-guarded) that rewrites the Dockerfile's hardcoded
`cmake-<ver>-Linux-x86_64.tar.gz` download to the `linux-aarch64` tarball of the same version.
Call it only on ARM builds.
**Verify**: a unit-style check — run the function against a copy of `vendor/MSVBASE/Dockerfile`
and `grep -q 'linux-aarch64'` the result; running it twice does not double-edit.

### Step 4: Rewrite gx10build.sh on the docker recipe
New gx10build.sh:
1. `set -euo pipefail`; arch-guard (must be `aarch64`/`arm64`, else FATAL — keep the existing guard).
2. `source scripts/lib/msvbase_patches.sh`.
3. clone (pinned, plan 002) + `git submodule update --init --recursive`.
4. `apply_msvbase_patches "$SRC"` → `verify_patches "$SRC"` (plan 003) →
   `patch_upstream_dockerfile "$SRC/Dockerfile"` → `patch_modern_gcc_includes "$SRC"` →
   the new aarch64-cmake patch.
5. `docker build -t tridb/msvbase:gx10 .` (NOT `make -C src`).
6. A real smoke test: run the image like `scripts/smoke_test.sh` does (CREATE EXTENSION
   vectordb → HNSW index → top-k), using `test/smoke.sql`. Remove the commented-out HNSW lines.
Delete the dead native-build pieces (`make -C src`, the `MSVBASE_DISABLE_SPTAG` export, the
hand-rolled `./configure`).
**Verify**: `bash -n scripts/gx10build.sh`; running it on this x86 box hits the arch guard and
refuses cleanly (you cannot run the real build here — that is expected and is a STOP-adjacent
boundary, not a failure).

### Step 5: Note parity in BUILD_NOTES.md
State that both scripts share `scripts/lib/msvbase_patches.sh` and that gx10build adds only the
aarch64-cmake delta. Remove any claim that gx10build is independently validated (it is not until
run on the GX10).
**Verify**: `grep -n 'msvbase_patches.sh\|aarch64' docs/BUILD_NOTES.md` → matches.

## Test plan
- `bash -n` all three scripts.
- Library function smoke: source the lib, run `patch_upstream_dockerfile` /
  `patch_modern_gcc_includes` / the aarch64 patch against a *copy* of the Dockerfile/CMakeLists
  and grep for the expected results; run twice to confirm idempotency.
- Dev box (heavy, optional): `scripts/x86build.sh --docker` still green after the refactor.
- GX10 (the real acceptance, separate hardware): `scripts/gx10build.sh` builds and the smoke
  test passes — this is the actual sign-off and happens off this machine.

## Done criteria
- [ ] `scripts/lib/msvbase_patches.sh` exists and defines the shared functions; sourcing it has
      no side effects.
- [ ] `x86build.sh` sources the lib; its `--docker` path is behaviorally unchanged.
- [ ] `gx10build.sh` builds via `docker build` using the shared patches + the aarch64-cmake
      delta + `verify_patches`; the `make -C src` / `MSVBASE_DISABLE_SPTAG` / commented-HNSW
      code is gone; it has a real smoke test.
- [ ] `bash -n` passes for all three scripts.
- [ ] No patch logic is duplicated between the two scripts (`grep -c patch_upstream_dockerfile
      scripts/x86build.sh scripts/gx10build.sh` → 0 definitions in each; only in the lib).
- [ ] `plans/README.md` status row updated.

## STOP conditions
- MSVBASE's Dockerfile cannot build on aarch64 even with the cmake swap (base image arch issue,
  an x86-only `apt` package, or SPTAG truly not compiling on ARM despite the force-includes).
  STOP and report exactly what failed — a native-build path or a documented SPTAG-exclusion in
  CMake may be required, which is a larger change than this plan.
- The MSVBASE base image (`gcc:12.3.0`) has no arm64 variant. STOP and report.
- You cannot validate Step 2 without the heavy `--docker` rebuild and the function-call order is
  ambiguous after refactor. STOP rather than guess — a behavior change in x86build is a
  regression of the one proven build.

## Maintenance notes
- The GX10 sign-off (actually running gx10build.sh on the hardware) is DEV-1160/1161 and happens
  off this machine; this plan only makes the script correct-by-construction against the proven
  recipe.
- Reviewer: confirm the two scripts share the lib and gx10build no longer contains `make -C src`.
- Deferred: a native (non-Docker) GX10 build path, if Docker is unavailable on the target.

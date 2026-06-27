# Plan 002: CI — verify the fork patch-chain applies on every PR

> **Executor instructions**: Follow step by step; confirm each verification before moving on. STOP and
> report on any STOP condition. Update this plan's row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat 7bf3dca..HEAD -- .github/workflows/ci.yml scripts/lib/msvbase_patches.sh scripts/x86build.sh`
> If any changed, reconcile the excerpts below with the live files before proceeding.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (CI time / clone weight)
- **Depends on**: none
- **Category**: dx + correctness
- **Planned at**: commit `7bf3dca`, 2026-06-26

## Why this matters

The entire engine is a **patched fork**: `vendor/MSVBASE` is gitignored and re-cloned at build time, and
`scripts/lib/msvbase_patches.sh` applies the upstream submodule patches + the TriDB fork patches
(`scripts/patches/*.patch`) and then runs `verify_patches`. Today CI only runs the Python layer — the
`engine` job is `workflow_dispatch`-only. So if upstream MSVBASE drifts, or a fork patch is regenerated
wrong, **a PR can merge green while the fork no longer builds**, and nobody finds out until someone runs
`scripts/x86build.sh --docker` by hand. This adds a fast PR check that the patch chain still *applies*
and its sentinels are present — without the 9.5 GB image build.

## Current state

- `.github/workflows/ci.yml`:
  ```yaml
  on: { push: {}, pull_request: {}, workflow_dispatch: {} }
  jobs:
    python:   # runs on every push/PR: pip install, pytest tests/ -q, ruff check + format --check
    engine:   # if: github.event_name == 'workflow_dispatch'  -> SKIPS on push/PR
      steps: [checkout (submodules:false), scripts/x86build.sh --docker, make graph-test]
  ```
- `scripts/lib/msvbase_patches.sh` exposes shell functions: `apply_msvbase_patches "$root"` (runs the
  upstream `scripts/patch.sh` for the spann/hnsw/Postgres submodule patches, then `apply_tridb_fork_patches`,
  then `verify_patches`) and `verify_patches "$root"` (greps sentinels for every patch; `die`s on a miss).
  `$root` is a checked-out MSVBASE working tree with its submodules initialized.
- `scripts/x86build.sh` is the existing consumer — read it to see exactly how it clones MSVBASE at the
  pinned commit and initializes submodules (the pin is documented in `plans/002-pin-msvbase-commit.md`).
- The TriDB fork patches that must apply: `scripts/patches/tridb_*.patch` + `l2_distance_scalar.patch` +
  `sptag_optional_build.patch`. The NEON patch targets the `thirdparty/hnsw` submodule; the reloptions and
  others target `src/`. So a faithful apply needs the `thirdparty/hnsw` and `thirdparty/Postgres`
  submodules initialized; **SPTAG is opt-in** (`WITH_SPTAG=OFF` default, DEV-1228) and `verify_patches`
  only checks the spann patch `if [[ -d "$root/thirdparty/SPTAG" ]]` — so SPTAG can be skipped.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Lint the workflow | `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` | exit 0 (valid YAML) |
| Dry-run apply locally (if a clone exists) | source `scripts/lib/msvbase_patches.sh`; `verify_patches vendor/MSVBASE` | `all MSVBASE + TriDB fork patches verified present` |

## Scope

**In scope:** `.github/workflows/ci.yml` (add one job); optionally a tiny helper `scripts/ci_check_patches.sh`.
**Out of scope:** the `python` and `engine` jobs (leave as-is); `scripts/lib/msvbase_patches.sh` and the
patches themselves (this plan only *invokes* them, does not change them); anything under `vendor/`.

## Git workflow

- Branch `advisor/002-ci-patch-check`; commit `ci: verify fork patch-chain applies on PRs`. Do not push/PR unless told.

## Steps

### Step 1: Add a `patches` job to `ci.yml`

Add a third job that runs on push/PR (no `workflow_dispatch` gate). It must:
1. Clone MSVBASE at the **pinned commit** (reuse the pin source `scripts/x86build.sh` uses — do not
   hardcode a different SHA), shallow if possible.
2. Initialize ONLY the `thirdparty/hnsw` and `thirdparty/Postgres` submodules (skip SPTAG):
   `git submodule update --init --depth 1 thirdparty/hnsw thirdparty/Postgres`.
3. `source scripts/lib/msvbase_patches.sh` and run `apply_msvbase_patches <clone-root>` then
   `verify_patches <clone-root>` (apply_msvbase_patches already calls verify at the end; calling verify
   again is a cheap belt-and-suspenders).
4. Fail the job (non-zero) on any patch that does not apply or any missing sentinel — `die` already exits
   non-zero, so just don't swallow it.

Keep it a Python/bash job on `ubuntu-latest` (no Docker image build). Target run time < ~5 min.

**Verify**: YAML validity command above → exit 0. Open the file and confirm the new job has no
`if: workflow_dispatch` gate.

### Step 2: Reconcile the SPTAG-skip assumption

Read `vendor/MSVBASE/scripts/patch.sh` (or the upstream `scripts/patch.sh` the clone will run). Confirm it
does NOT hard-require the SPTAG submodule when SPTAG is off. If it `cd`s into `thirdparty/SPTAG`
unconditionally, the lightweight clone will fail at the spann patch.

**Verify**: `grep -n SPTAG vendor/MSVBASE/scripts/patch.sh` and read the surrounding lines.

- If SPTAG is required unconditionally → **STOP** and fall back to the lighter check in Step 3 instead of
  the full apply.

### Step 3 (fallback only, if Step 2 hits the STOP): static patch-applicability lint

If cloning all needed submodules is impractical in CI, instead add a job that runs
`git apply --check` for each `scripts/patches/*.patch` against the committed `vendor/MSVBASE` tree IF one
is cached, OR at minimum validates each patch is well-formed (`git apply --stat` parses) and that every
patch sentinel string in `verify_patches` appears in its corresponding patch file (catches a sentinel that
can never match — a false-green verify). Document clearly in a comment that this is weaker than a real
apply and does not catch upstream drift.

## Test plan

There are no unit tests for CI config. Verification is the CI run itself: push the branch to a test PR
and confirm the new `patches` job runs and passes; then (optionally) temporarily corrupt one patch in the
PR and confirm the job goes red, to prove it actually checks. Revert the corruption before merge.

## Done criteria

- [ ] `.github/workflows/ci.yml` has a `patches` job with no `workflow_dispatch` gate (runs on push/PR).
- [ ] YAML validates (command above exits 0).
- [ ] On a test PR, the `patches` job is green; a deliberately broken patch makes it red (then reverted).
- [ ] `advisor-plans/README.md` row updated.

## STOP conditions

- Upstream `scripts/patch.sh` requires the SPTAG submodule unconditionally (use Step 3 fallback).
- The pinned-commit source is unclear or `scripts/x86build.sh` clones differently than assumed — report;
  do not guess a SHA.
- The job's clone + submodule init exceeds ~10 min on the runner — switch to Step 3.

## Maintenance notes

- When the MSVBASE pin is bumped (`plans/002-pin-msvbase-commit.md`), this job re-validates the chain
  against the new commit automatically — that is the point.
- This job validates *apply + sentinels*, not that the C compiles. The `engine` (`workflow_dispatch`)
  job remains the compile/run gate; consider scheduling it nightly (`schedule:` cron) as a follow-up.
- Reviewer: confirm the job does NOT pull SPTAG (keeps it fast) and that a missing sentinel actually fails.

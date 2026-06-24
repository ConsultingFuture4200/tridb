# Plan 003: Fail the build loudly if any MSVBASE patch did not apply

> **Executor instructions**: Follow step by step; run every verification command. On a STOP
> condition, stop and report. Update this plan's row in `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- scripts/x86build.sh scripts/gx10build.sh`
> If changed, compare excerpts below to live files first; mismatch = STOP.

## Status
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (complements 002; 004 reuses it)
- **Category**: build / bug
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
The MSVBASE submodule patches add **relaxed monotonicity** — the entire efficiency thesis:
`amcanrelaxedorderbyop`/`xs_inorder` on PostgreSQL and `ResultIterator`/`QueryResult` on
hnswlib. They are applied by the vendored `scripts/patch.sh`, which has **no `set -e` and does
not check `git apply`'s exit code**. `x86build.sh`'s `apply_msvbase_patches` guards only the
PostgreSQL sentinel *before* patching and verifies *nothing after*. So if `hnsw.patch` or
`Postgres.patch` fails to apply on upstream drift, the build proceeds and produces **a clean
build of the wrong database** — stock Postgres with no relaxed monotonicity — undetected until
runtime. This is exactly the failure mode `docs/BUILD_NOTES.md` calls the critical one.

## Current state
- `scripts/x86build.sh:84-92` (`apply_msvbase_patches`):
  ```bash
  apply_msvbase_patches() {
    local root="$1"
    if grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" 2>/dev/null; then
      log "MSVBASE submodule patches already applied"; return 0
    fi
    log "applying MSVBASE submodule patches (scripts/patch.sh: spann, hnsw, Postgres) — relaxed monotonicity"
    ( cd "$root" && bash scripts/patch.sh )
  }
  ```
- The vendored `vendor/MSVBASE/scripts/patch.sh` applies three patches via `git apply` with no
  error handling: `spann.patch` → `thirdparty/SPTAG`, `hnsw.patch` → `thirdparty/hnsw`,
  `Postgres.patch` → `thirdparty/Postgres`.
- `scripts/gx10build.sh:83-86` calls `bash scripts/patch.sh || die "..."` — but `patch.sh`
  exits 0 even on a partial failure, so the `|| die` never fires.
- **Sentinels that prove each patch applied** (verified to exist in the patched tree):
  - PostgreSQL: `amcanrelaxedorderbyop` in `thirdparty/Postgres/src/include/access/amapi.h`
  - hnswlib: `ResultIterator` in `thirdparty/hnsw/hnswlib/result_iterator.h`
  - SPTAG/spann: find a stable added symbol by inspecting the patch:
    `grep -m1 '^+.*[A-Za-z_]' vendor/MSVBASE/patch/spann.patch` (pick an added identifier /
    new file path that the patch introduces, e.g. a `SPANN`-related symbol or a new header).
- Bash conventions: `set -euo pipefail`, `log()`/`die()` helpers already defined in both scripts.

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Bash syntax | `bash -n scripts/x86build.sh && bash -n scripts/gx10build.sh` | exit 0 |
| PG sentinel present | `grep -rq amcanrelaxedorderbyop vendor/MSVBASE/thirdparty/Postgres/src/include/access/ && echo ok` | `ok` |
| hnsw sentinel present | `grep -rq ResultIterator vendor/MSVBASE/thirdparty/hnsw/hnswlib/ && echo ok` | `ok` |
| Inspect spann patch | `grep -nE '^\+\+\+ |^\+[A-Za-z]' vendor/MSVBASE/patch/spann.patch \| head` | shows added lines/files |

## Scope
**In scope**: `scripts/x86build.sh`, `scripts/gx10build.sh`.
**Out of scope**: `vendor/MSVBASE/scripts/patch.sh` (vendored — do NOT edit upstream; verify
from our scripts instead), the patch files themselves.

## Git workflow
- Branch `advisor/003-verify-patches`; commit `build: verify MSVBASE patches applied (fail loud on drift)`.

## Steps

### Step 1: Pick the three sentinels
Inspect `vendor/MSVBASE/patch/spann.patch` and choose ONE stable added symbol or new-file path
as the SPTAG/spann sentinel (the PG and hnsw sentinels are fixed: `amcanrelaxedorderbyop`,
`ResultIterator`). Record the three in a comment.
**Verify**: each sentinel grep (table above) returns a match against the currently-patched tree.

### Step 2: Add a `verify_patches` function to x86build.sh
After `apply_msvbase_patches` runs `patch.sh`, assert all three sentinels are present, `die` on
any miss. Add a function and call it at the end of `apply_msvbase_patches` (and on the
already-applied early-return path too, so a half-applied prior state is still caught):
```bash
verify_patches() {
  local root="$1"
  grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" \
    || die "Postgres.patch NOT applied (no amcanrelaxedorderbyop) — relaxed monotonicity missing; upstream drift?"
  grep -rq 'ResultIterator' "$root/thirdparty/hnsw/hnswlib/" \
    || die "hnsw.patch NOT applied (no ResultIterator) — VBASE iterator missing; upstream drift?"
  grep -rq '<SPANN_SENTINEL>' "$root/thirdparty/SPTAG/" \
    || die "spann.patch NOT applied — upstream drift?"
  log "all three MSVBASE patches verified present"
}
```
(Replace `<SPANN_SENTINEL>` and its search path with the symbol/path chosen in Step 1.)
**Verify**: with patches applied, the function returns 0 (call it manually:
`source <(sed -n '/^verify_patches()/,/^}/p' scripts/x86build.sh); verify_patches vendor/MSVBASE` →
prints "all three … verified"). Temporarily rename a sentinel in the search string → it `die`s.
Restore.

### Step 3: Wire the same verification into gx10build.sh
gx10build.sh calls `patch.sh` directly (lines 83-86). After it, call the same sentinel
verification (copy the function, or — if plan 004 lands first — use the shared
`scripts/lib/msvbase_patches.sh`). The `|| die` on `patch.sh` stays but is no longer the only
guard.
**Verify**: `bash -n scripts/gx10build.sh`; re-read confirms `verify_patches` runs after `patch.sh`.

## Test plan
No unit tests. Behavioral checks:
- Positive: run the `verify_patches` function against the live patched `vendor/MSVBASE` → passes.
- Negative: temporarily point one sentinel at a string that does not exist → the function
  `die`s with the right message. Revert.
(Optional, heavy: a full `scripts/x86build.sh --docker` still succeeds end-to-end.)

## Done criteria
- [ ] `x86build.sh` verifies all three patch sentinels after `patch.sh`, `die`-ing on any miss.
- [ ] `gx10build.sh` does the same.
- [ ] Negative test: removing/altering a sentinel target makes the function `die`.
- [ ] `bash -n` passes for both scripts.
- [ ] `plans/README.md` status row updated.

## STOP conditions
- You cannot find a stable, patch-introduced sentinel for `spann.patch` (the patch only edits
  existing lines with no greppable added symbol). STOP and report — propose using
  `git apply --check patch/spann.patch` (expected to FAIL when already applied) as the inverted
  check instead.
- The PG or hnsw sentinel is absent from the current tree even though the build is known-good
  (the patch set changed). STOP — the sentinels in this plan are stale.

## Maintenance notes
- If MSVBASE is re-pinned (plan 002) to a version where these sentinels move, update them here.
- Reviewer: confirm the verification runs on BOTH the fresh-apply and already-applied paths.
- Deferred: a stronger `git apply --check`-based verification that detects partial application
  mid-patch; the sentinel approach catches the end-state, which is sufficient for the known
  failure mode.

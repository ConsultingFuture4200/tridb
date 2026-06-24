# Plan 002: Pin the MSVBASE upstream commit so builds are reproducible

> **Executor instructions**: Follow step by step; run every verification command. On a STOP
> condition, stop and report. Update this plan's row in `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- scripts/x86build.sh scripts/gx10build.sh docs/BUILD_NOTES.md`
> If any changed, compare the excerpts below to the live files first; mismatch = STOP.

## Status
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: build / security
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
Both build scripts clone MSVBASE from a moving `HEAD` (`PIN_COMMIT=""`). Two builds run weeks
apart — or the x86 standin vs. the eventual GX10 build — can compile *different upstream code*,
making benchmark results and "builds with documented deltas" (DEV-1160 marker #1) ambiguous,
and opening a supply-chain drift window. The exact upstream commit the working build validated
is known: `1a548db14d7a3f6f64808c99b9bc1aa01a25b71f` ("Fix vector constant parsing. (#20)").

## Current state
- `scripts/x86build.sh:24` and `scripts/gx10build.sh:20`:
  ```bash
  PIN_COMMIT=""                       # set to the pinned commit once DEV-1160 confirms it
  ```
- The checkout is conditional and **only runs in the fresh-clone block**. In `x86build.sh` the
  docker path does: `[[ -n "$PIN_COMMIT" ]] && git checkout -q "$PIN_COMMIT"` after clone; in
  `gx10build.sh:76` likewise. If `vendor/MSVBASE` already exists, the pin is never applied.
- The currently-checked-out upstream commit (validated by the working x86 build):
  `git -C vendor/MSVBASE rev-parse HEAD` → `1a548db14d7a3f6f64808c99b9bc1aa01a25b71f`.
- `docs/BUILD_NOTES.md` claims the build is "reproducible" but records no pinned commit.

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Bash syntax | `bash -n scripts/x86build.sh && bash -n scripts/gx10build.sh` | exit 0 |
| Confirm pin string | `grep -n PIN_COMMIT scripts/x86build.sh scripts/gx10build.sh` | shows the SHA |
| Current vendor HEAD | `git -C vendor/MSVBASE rev-parse HEAD` | `1a548db…` |

## Scope
**In scope**: `scripts/x86build.sh`, `scripts/gx10build.sh`, `docs/BUILD_NOTES.md`.
**Out of scope**: the patch logic (plans 003/004), `vendor/MSVBASE/`.

## Git workflow
- Branch `advisor/002-pin-msvbase-commit`; conventional commit
  (`build: pin MSVBASE commit for reproducible builds`). No push unless asked.

## Steps

### Step 1: Set the default pin in both scripts
Replace the empty default in `x86build.sh:24` and `gx10build.sh:20` with:
```bash
PIN_COMMIT="1a548db14d7a3f6f64808c99b9bc1aa01a25b71f"   # MSVBASE "Fix vector constant parsing (#20)"; the validated build base
```
Keep the `--commit` override working (do not change the arg parser).
**Verify**: `grep -n '1a548db' scripts/x86build.sh scripts/gx10build.sh` → two matches.

### Step 2: Apply the pin even when vendor/ already exists
Today the checkout only happens on fresh clone. Make the pin apply whenever cloning is not
skipped. In each script's clone block, after `git submodule update` is set up, ensure the
sequence is: clone-if-absent → `git fetch --quiet origin` → `git checkout -q "$PIN_COMMIT"` →
`git submodule update --init --recursive`. The checkout must run for both fresh and existing
checkouts (guard only with `[[ "$SKIP_CLONE" -eq 0 ]]`, not with "fresh clone only").
**Verify**: re-reading the block, the `git checkout -q "$PIN_COMMIT"` line is reachable on an
existing clone. `bash -n` both scripts.

### Step 3: Record the pin in BUILD_NOTES.md
Add a short "Pinned upstream" line to `docs/BUILD_NOTES.md` naming the SHA and that `--commit`
overrides it.
**Verify**: `grep -n '1a548db' docs/BUILD_NOTES.md` → match.

## Test plan
No unit tests. Functional check (optional, heavy, on the dev box only): a clean rebuild
`scripts/x86build.sh --docker` still produces a green `scripts/smoke_test.sh`. If you cannot
afford the rebuild, a `bash -n` + manual re-read of the clone/checkout block is the gate.

## Done criteria
- [ ] Both scripts default `PIN_COMMIT` to `1a548db14d7a3f6f64808c99b9bc1aa01a25b71f`.
- [ ] The pinned checkout applies on existing clones, not only fresh ones.
- [ ] `--commit <sha>` still overrides.
- [ ] `docs/BUILD_NOTES.md` records the pinned commit.
- [ ] `bash -n` passes for both scripts.
- [ ] `plans/README.md` status row updated.

## STOP conditions
- `git checkout -q "$PIN_COMMIT"` on the existing `vendor/MSVBASE` fails because the working
  tree has local modifications (the patches from `scripts/patch.sh` / the Dockerfile seds are
  applied). STOP and report — the correct order is reset-submodules → checkout pin → re-apply
  patches, which interacts with plans 003/004; do not force-checkout over applied patches.
- The commit `1a548db…` is not reachable from the configured remote (upstream rewrote history).
  STOP and report; a new validated pin must be chosen.

## Maintenance notes
- When intentionally moving to a newer MSVBASE, update the pin in **one** place per script and
  re-validate with `scripts/x86build.sh --docker` + `make test-all` (plan 001).
- After plan 004 factors shared logic into `scripts/lib/msvbase_patches.sh`, the pin default
  should live there once, not duplicated — revisit then.

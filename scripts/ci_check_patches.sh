#!/usr/bin/env bash
#
# ci_check_patches.sh — fast PR gate that the MSVBASE fork patch-chain still APPLIES.
#
# The engine is a patched fork: vendor/MSVBASE is gitignored + re-cloned at build time, and
# scripts/lib/msvbase_patches.sh applies the upstream submodule patches (spann/hnsw/Postgres)
# plus the TriDB fork patches (scripts/patches/*.patch), then verify_patches greps a sentinel
# per patch and die()s on any miss. The heavy `engine` CI job (9.5 GB Docker image build) is
# workflow_dispatch-only, so without this check a PR can merge green while the fork no longer
# applies — invisible until someone runs scripts/x86build.sh --docker by hand.
#
# This clones MSVBASE at the SAME pinned commit the build scripts use (PIN_COMMIT default in
# scripts/lib/msvbase_patches.sh — NOT hardcoded here so a re-pin re-validates automatically),
# initializes ONLY the submodules the patches target (thirdparty/hnsw + thirdparty/Postgres;
# SPTAG is opt-in WITH_SPTAG=OFF and verify_patches only checks the spann patch when the SPTAG
# tree is initialized), then runs apply_msvbase_patches + verify_patches. It validates apply +
# sentinels only — NOT that the C compiles. The engine job remains the compile/run gate.
#
# SPTAG-skip is safe: upstream scripts/patch.sh runs each `git apply` in a subshell with no
# `set -e`, and its last command is the Postgres apply, so a missing SPTAG dir cannot abort it.
#
# Usage: scripts/ci_check_patches.sh   (no args; clones into a temp dir, cleaned on exit)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/microsoft/MSVBASE.git}"

log() { printf '\033[1;34m[ci-patches]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[ci-patches] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
require git

# Shared clone/patch/verify logic. Provides PIN_COMMIT default, apply_msvbase_patches,
# verify_patches. Sourced AFTER any env override so PIN_COMMIT survives the default.
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/msvbase_patches.sh"
[[ -f "$LIB" ]] || die "missing shared lib: $LIB"
# shellcheck source=lib/msvbase_patches.sh
source "$LIB"

[[ -n "${PIN_COMMIT:-}" ]] || die "PIN_COMMIT is empty — expected the default from $LIB"
log "MSVBASE pin: $PIN_COMMIT"

SRC="$(mktemp -d)"
# Tolerant cleanup: leftover shallow-submodule git processes can still be writing into $SRC when the
# trap fires, so a bare `rm -rf` intermittently errors "Directory not empty" and — under `set -e` —
# would fail the job AFTER a successful verify. Never let cleanup change the exit status.
trap 'rm -rf "$SRC" 2>/dev/null || true' EXIT

# Fetch only the pinned commit (shallow) — avoids cloning full upstream history.
log "cloning MSVBASE (shallow) -> $SRC"
git init -q "$SRC"
git -C "$SRC" remote add origin "$REPO_URL"
git -C "$SRC" fetch --quiet --depth 1 origin "$PIN_COMMIT"
git -C "$SRC" checkout -q FETCH_HEAD

# Initialize ONLY the submodules the patches target. SPTAG (spann.patch) is opt-in and skipped:
# verify_patches only greps it when the tree is initialized (sptag_initialized, plan 084), and
# upstream patch.sh tolerates its absence (subshell + no set -e). This keeps the clone light and
# the run fast. The registered-but-uninitialized submodule's EMPTY placeholder directory is
# deliberately LEFT in place so this run exercises the real initialization predicate the build
# scripts use — do not rm it as a workaround.
log "init submodules: thirdparty/hnsw thirdparty/Postgres (SPTAG skipped — opt-in, kept lean)"
git -C "$SRC" submodule update --init --depth 1 thirdparty/hnsw thirdparty/Postgres

# apply_msvbase_patches already calls verify_patches at the end; the explicit re-run is a cheap
# belt-and-suspenders. Any failed apply or missing sentinel calls die() -> non-zero exit.
log "applying + verifying MSVBASE submodule patches + TriDB fork patches"
apply_msvbase_patches "$SRC"
verify_patches "$SRC"

log "OK: fork patch-chain applies and all sentinels present at pin $PIN_COMMIT"

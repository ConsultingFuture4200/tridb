#!/usr/bin/env bash
#
# gx10build.sh — reproducible MSVBASE fork build for TriDB on the GX10 (ARM64 + CUDA, 128 GB).
#
# Built on the PROVEN recipe: MSVBASE's own Dockerfile + the shared patch layer in
# scripts/lib/msvbase_patches.sh (the same set of fixes the eight-iteration x86 build validated
# in scripts/x86build.sh --docker). Two target-specific deltas vs. x86:
#   1. patch_cmake_aarch64       — upstream Dockerfile hardcodes an x86_64 CMake tarball; swap for aarch64.
#   2. patch_cmake_arm_isa_flags — MSVBASE's CMakeLists hardcodes x86 ISA flags (-msse4.2 -maes -mavx2
#      -mmwaitx) that aarch64 GCC rejects, failing every cmake compile probe (surfaces as a bogus
#      "Could NOT find OpenMP_C"); strip them so hnswlib builds via its scalar path. Found by the first
#      live GX10 run — the x86 standin never exercised these (the flags are valid there).
#
# SPTAG is NOT disabled by an env var (MSVBASE's CMake builds it regardless); the modern-GCC
# force-includes let it compile, matching the x86 image. Off-target (non-ARM64) this refuses to run.
#
# VALIDATED ON-TARGET 2026-06-25: ran on the GX10 (GB10, aarch64, 128 GB). The fork builds
# ([100%] Built target vectordb) and scripts/smoke_test.sh PASSES (vectordb extension loads,
# 100k-row HNSW index builds, early-terminating ANN Index Scan path confirmed). The first live
# run surfaced GX10 delta #2 (x86 ISA flags) and a CWD-relative smoke-test path bug, both fixed
# here. This is the DEV-1160/1161 sign-off. See docs/BUILD_NOTES.md.
#
# Usage:
#   scripts/gx10build.sh [--repo-url URL] [--commit SHA] [--image NAME] [--skip-clone]
#
set -euo pipefail

# --- config / args ---------------------------------------------------------
REPO_URL="https://github.com/microsoft/MSVBASE.git"
PIN_COMMIT="${PIN_COMMIT:-}"   # default supplied by scripts/lib/msvbase_patches.sh; --commit overrides
VENDOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vendor"
IMAGE="tridb/msvbase:gx10"
SKIP_CLONE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)   REPO_URL="$2"; shift 2 ;;
    --commit)     PIN_COMMIT="$2"; shift 2 ;;
    --image)      IMAGE="$2"; shift 2 ;;
    --skip-clone) SKIP_CLONE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m[gx10build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[gx10build] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

# Absolute dir of THIS script, resolved BEFORE any cd. The build cd's into the MSVBASE tree
# (vendor/MSVBASE) before the docker build + smoke test, so a CWD-relative dirname would point
# the smoke-test/lib lookups at vendor/MSVBASE/scripts/* and break. Capture absolute now.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Shared MSVBASE clone/patch/verify logic (PIN_COMMIT default, apply_msvbase_patches,
# verify_patches, patch_upstream_dockerfile, patch_modern_gcc_includes, patch_cmake_aarch64).
# Sourced AFTER arg parsing so --commit survives the PIN_COMMIT default. Defines only.
LIB="$SCRIPT_DIR/lib/msvbase_patches.sh"
[[ -f "$LIB" ]] || die "missing shared lib: $LIB"
# shellcheck source=lib/msvbase_patches.sh
source "$LIB"

# --- guard: this is a GX10 (ARM64) build, not an off-target convenience -----
ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  die "expected ARM64 (GX10); got '$ARCH'. The MSVBASE fork build is GX10-gated — see docs/STATUS.md. (On x86 use scripts/x86build.sh --docker.)"
fi

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
require git; require docker

# --- clone (pinned) + submodules -------------------------------------------
SRC="${VENDOR_DIR}/MSVBASE"
mkdir -p "$VENDOR_DIR"
if [[ "$SKIP_CLONE" -eq 0 ]]; then
  [[ -d "$SRC/.git" ]] || { log "cloning MSVBASE -> $SRC"; git clone "$REPO_URL" "$SRC"; }
  cd "$SRC"
  if [[ -n "$PIN_COMMIT" ]]; then log "checking out pinned commit $PIN_COMMIT"; git fetch --quiet origin && git checkout -q "$PIN_COMMIT"; fi
  log "init submodules (Postgres fork, hnsw, SPTAG)"
  git submodule update --init --recursive
fi
cd "$SRC"

# --- patches (host-side, BEFORE docker build COPYs the tree) ---------------
# Order matters: relaxed-monotonicity submodule patches first (and verified), THEN the
# Dockerfile/CMake fixes. apply_msvbase_patches verifies on both fresh and already-applied paths.
apply_msvbase_patches "$SRC"            # spann/hnsw/Postgres — relaxed monotonicity. MUST precede force-includes.
patch_upstream_dockerfile "$SRC/Dockerfile"   # dead Boost URL, GID 999, drop --with-python
patch_modern_gcc_includes "$SRC"        # modern-GCC force-includes into SPTAG (so SPTAG compiles)
patch_cmake_aarch64 "$SRC/Dockerfile"   # GX10 delta #1: x86_64 -> aarch64 CMake tarball
patch_cmake_arm_isa_flags "$SRC"        # GX10 delta #2: strip x86 ISA flags (-msse4.2/-maes/-mavx2/-mmwaitx)

# --- build via MSVBASE's own Dockerfile (NOT make -C src — there is no src/Makefile) -------
log "building MSVBASE via its Dockerfile on aarch64 -> $IMAGE"
docker build -t "$IMAGE" .
log "image built: $IMAGE"

# --- smoke test: vectordb + HNSW index + early-terminating ANN scan --------
# Reuse the proven harness; it runs test/smoke.sql in the image and asserts the ANN Index Scan
# path (relaxed monotonicity / TR-1 early termination) is live. Fails loud on any error.
log "smoke test: scripts/smoke_test.sh against $IMAGE"
bash "$SCRIPT_DIR/smoke_test.sh" "$IMAGE"

log "GX10 BUILD + SMOKE OK ($IMAGE). Update tridb_spec marker #1 ('builds with documented deltas') for ARM64."

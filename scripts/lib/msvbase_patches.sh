#!/usr/bin/env bash
#
# msvbase_patches.sh — shared MSVBASE clone/patch/verify logic for TriDB's build scripts.
#
# SOURCE this from x86build.sh and gx10build.sh. It ONLY defines functions and two defaults
# (PIN_COMMIT, FORCE_INC); it runs no clone/build/patch on source and does not set -e. The
# caller must define log() and die() before invoking any of these functions, and must source
# this AFTER its own argument parsing so a --commit override survives the PIN_COMMIT default.
#
# Single source of truth: both build scripts share this so they cannot drift. The only
# target-specific delta is patch_cmake_aarch64 (GX10/ARM only).

# Validated upstream base (plan 002). Honors a prior assignment (e.g. from --commit).
PIN_COMMIT="${PIN_COMMIT:-1a548db14d7a3f6f64808c99b9bc1aa01a25b71f}"   # MSVBASE "Fix vector constant parsing (#20)"

# Sentinels proving each MSVBASE patch applied. The vendored scripts/patch.sh has no `set -e`
# and ignores `git apply`'s exit code, so a failed patch yields a clean build of the WRONG
# database (stock Postgres, no relaxed monotonicity). Verify the end-state and die on any miss.
#   Postgres.patch -> amcanrelaxedorderbyop (relaxed monotonicity, amapi.h)
#   hnsw.patch     -> ResultIterator       (VBASE iterator, hnswlib)
#   spann.patch    -> MultiIndexScan       (new SPTAG header AnnService/inc/Core/MultiIndexScan.h)
verify_patches() {
  local root="$1"
  grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" \
    || die "Postgres.patch NOT applied (no amcanrelaxedorderbyop) — relaxed monotonicity missing; upstream drift?"
  grep -rq 'ResultIterator' "$root/thirdparty/hnsw/hnswlib/" \
    || die "hnsw.patch NOT applied (no ResultIterator) — VBASE iterator missing; upstream drift?"
  grep -rq 'MultiIndexScan' "$root/thirdparty/SPTAG/" \
    || die "spann.patch NOT applied (no MultiIndexScan) — upstream drift?"
  log "all three MSVBASE patches verified present"
}

# Apply MSVBASE's submodule patches (spann/hnsw/Postgres — relaxed monotonicity), idempotent
# via a sentinel guard, then verify on BOTH the fresh-apply and already-applied paths.
apply_msvbase_patches() {
  local root="$1"
  if grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" 2>/dev/null; then
    log "MSVBASE submodule patches already applied"
    verify_patches "$root"
    return 0
  fi
  log "applying MSVBASE submodule patches (scripts/patch.sh: spann, hnsw, Postgres) — relaxed monotonicity"
  ( cd "$root" && bash scripts/patch.sh )
  verify_patches "$root"
}

# Patch known-dead upstream URLs / build-breakers in the MSVBASE Dockerfile. Arch-independent
# bit-rot — applies equally to the x86 and GX10 builds. Idempotent.
patch_upstream_dockerfile() {
  local df="$1"
  [[ -f "$df" ]] || return 0
  if grep -q 'boostorg.jfrog.io' "$df"; then
    log "patching dead Boost URL (boostorg.jfrog.io left JFrog in 2024) -> archives.boost.io"
    sed -i 's#https://boostorg.jfrog.io/artifactory/main/release/1.81.0/source/boost_1_81_0.tar.gz#https://archives.boost.io/release/1.81.0/source/boost_1_81_0.tar.gz#g' "$df"
  fi
  # Hardcoded GID/UID 999 collides with a pre-existing group in current gcc:12.3.0 base.
  # Add -o (allow non-unique id) so the postgres group/user reuses 999 without erroring.
  if grep -q 'groupadd -r postgres --gid=' "$df"; then
    log "patching postgres GID/UID 999 collision (add -o for non-unique id)"
    sed -i 's/groupadd -r postgres --gid=/groupadd -r -o postgres --gid=/g' "$df"
    sed -i 's/useradd -m -r -g postgres --uid=/useradd -m -r -o -g postgres --uid=/g' "$df"
  fi
  # PG 13.4 plpython.h #include "eval.h" — removed in Python 3.11+, so --with-python fails
  # against the base image's modern Python. PL/Python is not used by TriDB (all C), so drop it.
  if grep -q -- '--with-python' "$df"; then
    log "dropping --with-python from PG configure (eval.h gone in Python 3.11+; PL/Python unused by TriDB v1)"
    sed -i '/--with-python/d' "$df"
  fi
}

# Modern GCC (12/13 in the gcc:12.3.0 base) no longer transitively includes <mutex>,
# <cstdint>, etc. — old SPTAG/vectordb code assumed it did. Force-include the dropped
# headers via SPTAG's own CXX flags (SPTAG resets CMAKE_CXX_FLAGS, so a global -D won't reach
# it). Idempotent. Arch-independent; same fix needed on the GX10.
FORCE_INC='-include cstdint -include mutex -include shared_mutex -include memory -include cstring -include limits -include functional'
patch_modern_gcc_includes() {
  local root="$1"
  local sptag_cm="$root/thirdparty/SPTAG/CMakeLists.txt"
  # SPTAG only: it sets CXX-only flags, so force-includes never leak onto the C compiler.
  # (Do NOT touch the top vectordb CMakeLists — it derives CMAKE_C_FLAGS from CMAKE_CXX_FLAGS,
  # so a C++-header force-include there breaks cmake's OpenMP_C probe. Use a CXX-only genexp
  # if vectordb sources ever need it.)
  if [[ -f "$sptag_cm" ]] && grep -q -- '-std=c++14 -fopenmp"' "$sptag_cm" && ! grep -q 'include cstdint' "$sptag_cm"; then
    log "force-including dropped std headers into SPTAG build (modern GCC transitive-include fix)"
    sed -i "s/-std=c++14 -fopenmp\"/-std=c++14 -fopenmp ${FORCE_INC}\"/" "$sptag_cm"
  fi
}

# GX10/ARM-only delta: the upstream Dockerfile hardcodes an x86_64 CMake tarball. The pinned
# version (3.14.4) predates Kitware's aarch64 Linux binaries, so swap BOTH arch and version to
# 3.27.9 — the first-class aarch64 release the prior ensure_cmake already used. MSVBASE only
# needs CMake >= 3.14, and the install uses `tar --strip-components=1 -C /usr/local`, so the
# tarball's internal directory name is irrelevant. Idempotent, grep-guarded. Call only on ARM.
patch_cmake_aarch64() {
  local df="$1"
  [[ -f "$df" ]] || return 0
  if grep -q 'cmake-3.14.4-Linux-x86_64.tar.gz' "$df"; then
    log "patching Dockerfile CMake download for aarch64 (3.14.4 x86_64 -> 3.27.9 aarch64; GX10 delta)"
    sed -i 's#https://github.com/Kitware/CMake/releases/download/v3.14.4/cmake-3.14.4-Linux-x86_64.tar.gz#https://github.com/Kitware/CMake/releases/download/v3.27.9/cmake-3.27.9-linux-aarch64.tar.gz#g' "$df"
  fi
}

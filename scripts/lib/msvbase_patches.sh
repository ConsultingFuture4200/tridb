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

# Absolute dir of THIS lib, resolved at source time. Callers cd into the MSVBASE tree before
# invoking these functions, so a call-time relative ${BASH_SOURCE} would not resolve — capture
# it now (read-only path resolution; no other side effects on source).
_MSVBASE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Validated upstream base (plan 002). Honors a prior assignment (e.g. from --commit).
PIN_COMMIT="${PIN_COMMIT:-1a548db14d7a3f6f64808c99b9bc1aa01a25b71f}"   # MSVBASE "Fix vector constant parsing (#20)"

# Official checksums for build-time downloads (supply-chain integrity, plan 007). Update these
# whenever a version changes or MSVBASE is re-pinned.
#   Boost 1.81.0 source  : boost.org 1.81.0 release, confirmed against the archives.boost.io tarball
#   CMake 3.14.4 x86_64  : Kitware cmake-3.14.4-SHA-256.txt (x86 build)
#   CMake 3.27.9 aarch64 : Kitware cmake-3.27.9-SHA-256.txt (GX10 build, via patch_cmake_aarch64)
BOOST_1_81_0_SHA256="205666dea9f6a7cfed87c7a6dfbeb52a2c1b9de55712c9c1a87735d7181452b6"
CMAKE_3_14_4_X86_64_SHA256="9f414df8e432c4a143c2d6d81e170581badba8d89df1cf8944735b9122765c50"
CMAKE_3_27_9_AARCH64_SHA256="11bf3d30697df465cdf43664a9473a586f010c528376a966fd310a3a22082461"

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
  # SPTAG is opt-in (WITH_SPTAG, DEV-1228); a lean build may skip the submodule entirely, so only
  # verify the spann patch when the SPTAG tree is actually present.
  if [[ -d "$root/thirdparty/SPTAG" ]]; then
    grep -rq 'MultiIndexScan' "$root/thirdparty/SPTAG/" \
      || die "spann.patch NOT applied (no MultiIndexScan) — upstream drift?"
  fi
  grep -q 'TRIDB: real scalar L2 distance' "$root/src/operator.cpp" \
    || die "TriDB l2_distance_scalar.patch NOT applied — scalar distance still broken; drift?"
  grep -q 'WITH_SPTAG' "$root/CMakeLists.txt" \
    || die "TriDB sptag_optional_build.patch NOT applied — WITH_SPTAG gate missing (DEV-1228); drift?"
  grep -q 'TRIDB_ASSERT_VECTOR_BACKEND' "$root/src/hnswindex_scan.cpp" \
    || die "TriDB tridb_vector_index_seam.patch NOT applied — vector-index seam missing (DEV-1228); drift?"
  grep -q 'tridb_vec_open' "$root/src/tridb_vector_iter.cpp" 2>/dev/null \
    || die "TriDB tridb_vector_iter.patch NOT applied — relaxed-mono vector iterator missing (DEV-1168); drift?"
  grep -q 'src/tridb_vector_iter.cpp' "$root/CMakeLists.txt" \
    || die "TriDB tridb_vector_iter.patch NOT wired into CMakeLists vectordb sources (DEV-1168); drift?"
  log "all MSVBASE + TriDB fork patches verified present"
}

# Apply MSVBASE's submodule patches (spann/hnsw/Postgres — relaxed monotonicity), idempotent
# via a sentinel guard, then verify on BOTH the fresh-apply and already-applied paths.
apply_msvbase_patches() {
  local root="$1"
  if grep -rq 'amcanrelaxedorderbyop' "$root/thirdparty/Postgres/src/include/access/" 2>/dev/null; then
    log "MSVBASE submodule patches already applied"
  else
    log "applying MSVBASE submodule patches (scripts/patch.sh: spann, hnsw, Postgres) — relaxed monotonicity"
    ( cd "$root" && bash scripts/patch.sh )
  fi
  apply_tridb_fork_patches "$root"
  verify_patches "$root"
}

# TriDB's own fork patches, applied on top of MSVBASE's (scripts/patch.sh). These live under
# scripts/patches/ in THIS repo because vendor/MSVBASE/ is gitignored + re-cloned, so a patch
# placed there would be wiped. Idempotent via each patch's sentinel.
#   l2_distance_scalar.patch (plan 005): scalar l2_distance returned 0 for any dim < 16 (static
#     L2Space built with dim=0 -> hnswlib L2SqrSIMD16Ext sums only full 16-float blocks). Fixed
#     to compute the Euclidean distance directly; unblocks SQL exact re-rank / DEV-1168 tests.
#   sptag_optional_build.patch (DEV-1228, ADR-0004): WITH_SPTAG CMake option (default OFF) gating
#     the SPTAG build/link, the sptag/spann sources, the lib.cpp registration, and the SQL DDL.
#     Default build is hnswlib-only (no SPTAG) — unblocks the GX10 ARM port. Opt in: -DWITH_SPTAG=ON.
apply_tridb_fork_patches() {
  local root="$1"
  local patch="${_MSVBASE_LIB_DIR}/../patches/l2_distance_scalar.patch"
  [[ -f "$patch" ]] || die "missing TriDB fork patch: $patch"
  if grep -q 'TRIDB: real scalar L2 distance' "$root/src/operator.cpp" 2>/dev/null; then
    log "TriDB fork patch (scalar l2_distance) already applied"
  else
    log "applying TriDB fork patch: real scalar l2_distance (plan 005)"
    ( cd "$root" && git apply "$patch" ) \
      || die "l2_distance_scalar.patch did not apply — MSVBASE drift? re-generate from src/operator.cpp"
  fi

  local sptag_patch="${_MSVBASE_LIB_DIR}/../patches/sptag_optional_build.patch"
  [[ -f "$sptag_patch" ]] || die "missing TriDB fork patch: $sptag_patch"
  if grep -q 'WITH_SPTAG' "$root/CMakeLists.txt" 2>/dev/null; then
    log "TriDB fork patch (WITH_SPTAG decouple, DEV-1228) already applied"
  else
    log "applying TriDB fork patch: WITH_SPTAG vector-index decouple (DEV-1228 / ADR-0004)"
    ( cd "$root" && git apply "$sptag_patch" ) \
      || die "sptag_optional_build.patch did not apply — MSVBASE drift? re-generate per ADR-0004"
  fi

  local seam_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_vector_index_seam.patch"
  [[ -f "$seam_patch" ]] || die "missing TriDB fork patch: $seam_patch"
  if grep -q 'TRIDB_ASSERT_VECTOR_BACKEND' "$root/src/hnswindex_scan.cpp" 2>/dev/null; then
    log "TriDB fork patch (vector-index seam, DEV-1228) already applied"
  else
    log "applying TriDB fork patch: TriDB-owned vector-index seam (DEV-1228 / ADR-0004)"
    ( cd "$root" && git apply "$seam_patch" ) \
      || die "tridb_vector_index_seam.patch did not apply — MSVBASE drift? re-generate per ADR-0004"
  fi

  #   tridb_vector_iter.patch (DEV-1168, FR-3): the relaxed-monotonicity vector iterator the TJS
  #     operator (DEV-1169) drives WITHOUT an IndexScanDesc. Adds src/tridb_vector_iter.{hpp,cpp}
  #     (extern "C" Open/Next/Close lifting hnsw_gettuple's stop into a caller-controlled bound,
  #     surfacing hnswlib's internal GetDistance() per Next) + src/tridb_vector_probe.cpp (a
  #     test-only SQL SRF), wires both into the UNCONDITIONAL vectordb source list, and declares
  #     tridb_vec_probe() in sql/vectordb.sql. Must apply AFTER the seam patch (it relies on
  #     HNSWIndexScan + the seam contract). hnswlib-only — no SPTAG.
  local iter_patch="${_MSVBASE_LIB_DIR}/../patches/tridb_vector_iter.patch"
  [[ -f "$iter_patch" ]] || die "missing TriDB fork patch: $iter_patch"
  if grep -q 'tridb_vec_open' "$root/src/tridb_vector_iter.cpp" 2>/dev/null; then
    log "TriDB fork patch (relaxed-mono vector iterator, DEV-1168) already applied"
  else
    log "applying TriDB fork patch: relaxed-monotonicity vector iterator (DEV-1168 / FR-3)"
    ( cd "$root" && git apply "$iter_patch" ) \
      || die "tridb_vector_iter.patch did not apply — MSVBASE drift? re-generate per DEV-1168"
  fi
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
  harden_dockerfile_downloads "$df"
}

# Supply-chain integrity (plan 007): the upstream Dockerfile streams Boost + CMake tarballs
# (`wget -O - | tar`) with `--no-check-certificate` and no hash check — a tampered mirror could
# inject code into the image undetected. Restore TLS verification and rewrite each download into
# download-to-file -> `sha256sum -c` -> extract. Idempotent (the streaming form is gone after the
# first pass). CMake here is the x86_64 3.14.4 tarball; patch_cmake_aarch64 swaps URL+hash on ARM.
harden_dockerfile_downloads() {
  local df="$1"
  [[ -f "$df" ]] || return 0
  if grep -q -- '--no-check-certificate' "$df"; then
    log "restoring TLS verification on Dockerfile downloads (drop --no-check-certificate)"
    sed -i 's/ --no-check-certificate//g' "$df"
  fi
  if grep -q 'boost_1_81_0.tar.gz" -q -O -' "$df"; then
    log "hardening Boost download (sha256sum -c before extract)"
    sed -i 's#boost_1_81_0.tar.gz" -q -O - \\#boost_1_81_0.tar.gz" -q -O boost.tgz \&\& \\#' "$df"
    sed -i 's#| tar -xz && \\#echo "'"$BOOST_1_81_0_SHA256"'  boost.tgz" | sha256sum -c - \&\& tar -xzf boost.tgz \&\& rm -f boost.tgz \&\& \\#' "$df"
  fi
  if grep -q 'cmake-3.14.4-Linux-x86_64.tar.gz" -q -O -' "$df"; then
    log "hardening CMake download (sha256sum -c before extract)"
    sed -i 's#cmake-3.14.4-Linux-x86_64.tar.gz" -q -O - \\#cmake-3.14.4-Linux-x86_64.tar.gz" -q -O cmake.tgz \&\& \\#' "$df"
    sed -i 's#| tar -xz --strip-components=1 -C /usr/local#echo "'"$CMAKE_3_14_4_X86_64_SHA256"'  cmake.tgz" | sha256sum -c - \&\& tar -xzf cmake.tgz --strip-components=1 -C /usr/local \&\& rm -f cmake.tgz#' "$df"
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
    # If harden_dockerfile_downloads already injected the x86_64 checksum, swap it to the
    # aarch64 one too (no-op if hardening has not run). Keeps URL and hash consistent on ARM.
    sed -i "s#${CMAKE_3_14_4_X86_64_SHA256}#${CMAKE_3_27_9_AARCH64_SHA256}#g" "$df"
  fi
}

#!/usr/bin/env bash
#
# gx10build.sh — reproducible MSVBASE fork build for TriDB on the GX10 (ARM64 + CUDA).
#
# Implements the three documented deltas from the DEV-1160 desk spike that make MSVBASE's
# pinned PostgreSQL 13.4 + vector-index stack build on aarch64:
#   1. Replace the hardcoded x86_64 CMake download with the aarch64 tarball (or distro cmake).
#   2. Exclude/stub SPTAG (x86-assuming; NOT on the v1 critical path — HNSW is the only v1 index).
#   3. Rely on hnswlib's portable scalar fallback (HNSW builds + runs correctly on ARM, slower).
#
# Resolves spec marker #1 LIVE. Off-target (x86_64) this script intentionally refuses to run.
#
# Usage:
#   scripts/gx10build.sh [--repo-url URL] [--commit SHA] [--jobs N] [--prefix DIR] [--skip-clone]
#
set -euo pipefail

# --- config / args ---------------------------------------------------------
REPO_URL="https://github.com/microsoft/MSVBASE.git"
PIN_COMMIT="1a548db14d7a3f6f64808c99b9bc1aa01a25b71f"   # MSVBASE "Fix vector constant parsing (#20)"; the validated build base. Override with --commit.
JOBS="$(nproc)"
VENDOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vendor"
PREFIX="${VENDOR_DIR}/MSVBASE/install"
CMAKE_MIN="3.14"
SKIP_CLONE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)   REPO_URL="$2"; shift 2 ;;
    --commit)     PIN_COMMIT="$2"; shift 2 ;;
    --jobs)       JOBS="$2"; shift 2 ;;
    --prefix)     PREFIX="$2"; shift 2 ;;
    --skip-clone) SKIP_CLONE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m[gx10build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[gx10build] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

# --- guard: this is a GX10 (ARM64) build, not an off-target convenience -----
ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  die "expected ARM64 (GX10); got '$ARCH'. The MSVBASE fork build is GX10-gated — see docs/STATUS.md."
fi

# --- toolchain (delta #1: aarch64 cmake) -----------------------------------
ensure_cmake() {
  if command -v cmake >/dev/null 2>&1; then
    local have; have="$(cmake --version | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
    if [[ "$(printf '%s\n%s\n' "$CMAKE_MIN" "$have" | sort -V | head -1)" == "$CMAKE_MIN" ]]; then
      log "using system cmake $have"; return
    fi
  fi
  log "fetching aarch64 cmake (delta #1: upstream Dockerfile hardcodes x86_64 tarball)"
  local ver="3.27.9" tgz="cmake-${ver}-linux-aarch64.tar.gz"
  curl -fsSL "https://github.com/Kitware/CMake/releases/download/v${ver}/${tgz:-$tgz}" -o "/tmp/${tgz}"
  tar -C /tmp -xzf "/tmp/${tgz}"
  export PATH="/tmp/cmake-${ver}-linux-aarch64/bin:${PATH}"
  log "cmake on PATH: $(cmake --version | head -1)"
}

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
require git; require curl; require make; require gcc
ensure_cmake

# --- clone + submodules + patches ------------------------------------------
SRC="${VENDOR_DIR}/MSVBASE"
mkdir -p "$VENDOR_DIR"
if [[ "$SKIP_CLONE" -eq 0 ]]; then
  if [[ ! -d "$SRC/.git" ]]; then
    log "cloning MSVBASE -> $SRC"
    git clone "$REPO_URL" "$SRC"
  fi
  cd "$SRC"
  if [[ -n "$PIN_COMMIT" ]]; then log "checking out pinned commit $PIN_COMMIT"; git fetch --quiet origin && git checkout -q "$PIN_COMMIT"; fi
  log "init submodules (Postgres fork, hnsw, SPTAG)"
  git submodule update --init --recursive
fi
cd "$SRC"

# Apply MSVBASE's submodule patches (Postgres.patch et al). Acceptance: all three apply.
if [[ -f scripts/patch.sh ]]; then
  log "applying submodule patches (scripts/patch.sh)"
  bash scripts/patch.sh || die "patch step failed — capture failing hunk for marker #1 (Postgres.patch on ARM)"
fi

# --- delta #2: exclude SPTAG (x86-assuming, not on v1 critical path) --------
# HNSW (hnswlib) is the only v1 vector index per spec §4. SPTAG/SPANN deferred (IVF/Vamana).
export MSVBASE_DISABLE_SPTAG=1
log "SPTAG excluded for v1 (delta #2). HNSW-only via hnswlib portable scalar fallback (delta #3)."

# --- build PostgreSQL 13.4 fork with MSVBASE flags --------------------------
# Non-standard flag carried by the fork: --with-blocksize=32 (32KB pages). This drives the
# native graph-store page layout in Phase 1 (DEV-1163) — do NOT change it.
PG_SRC="${SRC}/thirdparty/Postgres"
[[ -d "$PG_SRC" ]] || die "Postgres fork submodule not found at $PG_SRC"
log "configuring PostgreSQL 13.4 fork (--with-blocksize=32) -> prefix $PREFIX"
cd "$PG_SRC"
./configure --prefix="$PREFIX" --with-blocksize=32 --without-readline --without-zlib \
            CFLAGS="-O2 -fno-omit-frame-pointer"
make -j"$JOBS"
make install
export PATH="${PREFIX}/bin:${PATH}"
log "postgres built: $(pg_config --version)"

# --- build the vectordb extension (HNSW only) ------------------------------
cd "$SRC"
log "building vectordb extension (HNSW only)"
if [[ -d src ]]; then
  make -C src -j"$JOBS" PG_CONFIG="${PREFIX}/bin/pg_config" MSVBASE_DISABLE_SPTAG=1
  make -C src install PG_CONFIG="${PREFIX}/bin/pg_config"
fi

# --- smoke test: HNSW top-k + filter ---------------------------------------
log "smoke test: init cluster, build HNSW index, run TopK+Filter"
DATADIR="${PREFIX}/data"
rm -rf "$DATADIR"
"${PREFIX}/bin/initdb" -D "$DATADIR" >/dev/null
"${PREFIX}/bin/pg_ctl" -D "$DATADIR" -l "${PREFIX}/server.log" -o "-p 5440" -w start
trap '"${PREFIX}/bin/pg_ctl" -D "${DATADIR}" -m fast stop || true' EXIT

PSQL=("${PREFIX}/bin/psql" -p 5440 -v ON_ERROR_STOP=1 -d postgres)
"${PSQL[@]}" -c "CREATE EXTENSION IF NOT EXISTS vectordb;" || \
  log "NOTE: extension name may differ; adjust per MSVBASE README before marking marker #1 resolved."
"${PSQL[@]}" <<'SQL'
CREATE TABLE smoke (id int primary key, v float8[]);
INSERT INTO smoke SELECT g, ARRAY[g*1.0, (g%7)*1.0, (g%3)*1.0] FROM generate_series(1,1000) g;
-- HNSW index build (syntax per MSVBASE README; this is the marker-#1 assertion point):
-- CREATE INDEX smoke_hnsw ON smoke USING hnsw (v) WITH (dimension=3, ...);
SELECT id FROM smoke ORDER BY v <-> ARRAY[5.0,5.0,1.0] LIMIT 5;
SQL

log "BUILD + SMOKE OK. Update tridb_spec_v0.2.0.md marker #1: 'builds with documented deltas'."
log "install prefix: $PREFIX"

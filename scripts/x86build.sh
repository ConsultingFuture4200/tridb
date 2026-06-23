#!/usr/bin/env bash
#
# x86build.sh — native x86_64 dev/CI build of the MSVBASE fork for TriDB.
#
# This workstation stands in for the GX10 for all SOFTWARE work (Phases 0-2): the native
# graph store, TJS operator, and planner are architecture-independent PostgreSQL-internals C.
# x86_64 is MSVBASE's NATIVE target, so none of the ARM64 deltas (aarch64 cmake, NEON, etc.)
# apply here — this is the easier build.
#
# It does NOT substitute for the GX10 on: (a) DEV-1160 marker #1 (ARM64 build sign-off),
# (b) the 128GB-in-memory headline benchmark, (c) ARM alignment bugs. See docs/STATUS.md.
#
# SPTAG is still excluded (not on the v1 critical path; HNSW is the only v1 index) to keep
# the build lean. Hardening flags (-Wcast-align, sanitizers) are opt-in to catch latent
# ARM-portability bugs early.
#
# Usage:
#   scripts/x86build.sh [--repo-url URL] [--commit SHA] [--jobs N] [--prefix DIR]
#                       [--sanitize] [--skip-clone] [--docker]
#
set -euo pipefail

REPO_URL="https://github.com/microsoft/MSVBASE.git"
PIN_COMMIT=""
JOBS="$(nproc)"
VENDOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vendor"
PREFIX="${VENDOR_DIR}/MSVBASE/install"
SKIP_CLONE=0
SANITIZE=0
USE_DOCKER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)   REPO_URL="$2"; shift 2 ;;
    --commit)     PIN_COMMIT="$2"; shift 2 ;;
    --jobs)       JOBS="$2"; shift 2 ;;
    --prefix)     PREFIX="$2"; shift 2 ;;
    --sanitize)   SANITIZE=1; shift ;;
    --skip-clone) SKIP_CLONE=1; shift ;;
    --docker)     USE_DOCKER=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m[x86build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[x86build] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

ARCH="$(uname -m)"
[[ "$ARCH" == "x86_64" ]] || die "this is the x86_64 dev build; got '$ARCH'. For the GX10 use scripts/gx10build.sh."

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
require git

# --- fast path: MSVBASE ships an x86_64 Dockerfile (its native target) -------
if [[ "$USE_DOCKER" -eq 1 ]]; then
  require docker
  SRC="${VENDOR_DIR}/MSVBASE"
  [[ -d "$SRC/.git" ]] || git clone "$REPO_URL" "$SRC"
  cd "$SRC"
  [[ -n "$PIN_COMMIT" ]] && git checkout -q "$PIN_COMMIT"
  git submodule update --init --recursive
  log "building MSVBASE via its native x86_64 Dockerfile"
  docker build -t tridb/msvbase:dev .
  log "image built: tridb/msvbase:dev   (run: docker run --rm -it tridb/msvbase:dev)"
  exit 0
fi

# --- native (non-docker) build ----------------------------------------------
require curl; require make; require gcc; require cmake
log "cmake: $(cmake --version | head -1)  (native x86_64 — no aarch64 substitution needed)"

SRC="${VENDOR_DIR}/MSVBASE"
mkdir -p "$VENDOR_DIR"
if [[ "$SKIP_CLONE" -eq 0 ]]; then
  [[ -d "$SRC/.git" ]] || { log "cloning MSVBASE -> $SRC"; git clone "$REPO_URL" "$SRC"; }
  cd "$SRC"
  [[ -n "$PIN_COMMIT" ]] && git checkout -q "$PIN_COMMIT"
  log "init submodules"
  git submodule update --init --recursive
fi
cd "$SRC"

[[ -f scripts/patch.sh ]] && { log "applying submodule patches"; bash scripts/patch.sh || die "patch step failed"; }

export MSVBASE_DISABLE_SPTAG=1
log "SPTAG excluded (HNSW-only v1). Postgres fork: --with-blocksize=32 (drives DEV-1163 layout)."

CFLAGS_EXTRA="-O2 -fno-omit-frame-pointer"
[[ "$SANITIZE" -eq 1 ]] && {
  log "sanitizer build: -Wcast-align + ASan/UBSan (catch latent ARM-portability bugs early)"
  CFLAGS_EXTRA="-O1 -g -Wcast-align -fsanitize=address,undefined -fno-omit-frame-pointer"
}

PG_SRC="${SRC}/thirdparty/Postgres"
[[ -d "$PG_SRC" ]] || die "Postgres fork submodule not found at $PG_SRC"
log "configuring PostgreSQL 13.4 fork -> $PREFIX"
cd "$PG_SRC"
./configure --prefix="$PREFIX" --with-blocksize=32 --without-readline --without-zlib \
            CFLAGS="$CFLAGS_EXTRA"
make -j"$JOBS"
make install
export PATH="${PREFIX}/bin:${PATH}"
log "postgres built: $(pg_config --version)"

cd "$SRC"
if [[ -d src ]]; then
  log "building vectordb extension (HNSW only)"
  make -C src -j"$JOBS" PG_CONFIG="${PREFIX}/bin/pg_config" MSVBASE_DISABLE_SPTAG=1
  make -C src install PG_CONFIG="${PREFIX}/bin/pg_config"
fi

# --- smoke test --------------------------------------------------------------
log "smoke test: HNSW top-k + filter"
DATADIR="${PREFIX}/data"; rm -rf "$DATADIR"
"${PREFIX}/bin/initdb" -D "$DATADIR" >/dev/null
"${PREFIX}/bin/pg_ctl" -D "$DATADIR" -l "${PREFIX}/server.log" -o "-p 5441" -w start
trap '"${PREFIX}/bin/pg_ctl" -D "${DATADIR}" -m fast stop || true' EXIT
PSQL=("${PREFIX}/bin/psql" -p 5441 -v ON_ERROR_STOP=1 -d postgres)
"${PSQL[@]}" -c "CREATE EXTENSION IF NOT EXISTS vectordb;" || \
  log "NOTE: confirm extension name against MSVBASE README."
"${PSQL[@]}" -c "SELECT 1 AS plumbing_ok;"

log "x86 DEV BUILD OK. This is the standin for Phases 0-2 software work; GX10 still owns"
log "ARM sign-off (DEV-1160) + the 128GB headline benchmark. install prefix: $PREFIX"

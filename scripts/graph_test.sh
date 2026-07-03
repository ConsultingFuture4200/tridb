#!/usr/bin/env bash
#
# graph_test.sh — build BOTH graph-store extensions against the MSVBASE fork image and run
# a self-checking test suite (traversal iterator + TR-1 early termination + FR-7).
#
# Installs the v1 native AM (graph_store_am, src/graph_store/ — the shipped operator substrate
# since ADR-0013 Stage A) AND the v0 heap-backed extension (graph_store, src/graph_store_ext/ —
# kept building + tested until Stage C archives it). Each test SQL picks its store via its own
# CREATE EXTENSION line; the parity oracle (test/graph_v0v1_parity_test.sql) loads both.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_test.sh [image] [sql]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V0="$ROOT/src/graph_store_ext"
EXT_V1="$ROOT/src/graph_store"
TEST="$(cd "$ROOT" && realpath "${2:-test/graph_store_test.sql}")"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT_V0}:/tmp/ext_v0:ro" -v "${EXT_V1}:/tmp/ext_v1:ro" \
  -v "${TEST}:/tmp/graph_test.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build both extensions (mounts are read-only, so build in writable copies)
  for e in v1 v0; do
    cp -r /tmp/ext_$e /tmp/build_$e && cd /tmp/build_$e
    echo "=== make ($e) ==="; make PG_CONFIG=$PGC 2>&1 | tail -6
    echo "=== make install ($e) ==="; make PG_CONFIG=$PGC install 2>&1 | tail -4
  done
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== run tests ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/graph_test.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'low canse|redirecting log|logging collector'
echo "[graph_test] done"

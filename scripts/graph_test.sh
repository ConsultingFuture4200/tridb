#!/usr/bin/env bash
#
# graph_test.sh — build the graph_store extension against the MSVBASE fork and run
# its self-checking test suite (traversal iterator + TR-1 early termination + FR-7).
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store_ext"
TEST="$(cd "$ROOT" && realpath "${2:-test/graph_store_test.sql}")"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${TEST}:/tmp/graph_test.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build (mount is read-only, so build in a writable copy)
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  echo "=== make ==="; make PG_CONFIG=$PGC 2>&1 | tail -6
  echo "=== make install ==="; make PG_CONFIG=$PGC install 2>&1 | tail -4
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

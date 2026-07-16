#!/usr/bin/env bash
#
# tjs_test.sh — run the DEV-1169 TJS operator canonical end-to-end test (FR-4) against the built
# MSVBASE fork image (tridb/msvbase:dev). The TJS operator ships INSIDE vectordb.so (the image), but
# its GRAPH leg probes the graph_store extension at runtime, so this harness — like graph_test.sh —
# PGXS-builds + installs src/graph_store (the v1 native AM, graph_store_am — ADR-0013 Stage B) into a
# throwaway cluster, then runs the test SQL (which
# does CREATE EXTENSION vectordb + graph_store_am).
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/tjs_test.sh [image] [sql]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"   # v1 native AM (graph_store_am, ADR-0013 Stage B)
TEST="$(cd "$ROOT" && realpath "${2:-test/canonical_e2e_test.sql}")"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${TEST}:/tmp/tjs_test.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build of the graph_store extension (mount is read-only, so build in a writable copy).
  # Fail LOUD on a build error: piping make to `tail` previously masked the nonzero
  # exit (no pipefail in this inner shell), so a broken build looked green.
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  echo "=== make graph_store ==="
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { tail -30 /tmp/make.log; echo "BUILD FAILED"; exit 1; }
  tail -6 /tmp/make.log
  echo "=== make install ==="
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; echo "INSTALL FAILED"; exit 1; }
  tail -4 /tmp/install.log
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== run TJS canonical e2e ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/tjs_test.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'low canse|redirecting log|logging collector'
echo "[tjs_test] done"

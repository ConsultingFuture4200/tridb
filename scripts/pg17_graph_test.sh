#!/usr/bin/env bash
#
# pg17_graph_test.sh — run a graph SQL suite against STOCK PostgreSQL 17 + pgvector
# (D2 phase 2.1, the un-forked graph AM; mirror of scripts/graph_test.sh which targets
# the MSVBASE fork image).
#
# Builds the v1 native AM (src/graph_store) and the v0 heap-backed extension
# (src/graph_store_ext) with PGXS against the stock 17 headers inside
# tridb/pg17-unfork:dev (scripts/pg17/Dockerfile), initdb's a throwaway 8KB-page
# cluster, and runs the given SQL file. No fork, no GX10, no 32KB pages.
#
# Usage: scripts/pg17_graph_test.sh [image] [sql]
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V0="$ROOT/src/graph_store_ext"
EXT_V1="$ROOT/src/graph_store"
TEST="$(cd "$ROOT" && realpath "${2:-test/graph_store_am_test.sql}")"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t tridb/pg17-unfork:dev scripts/pg17/" >&2
  exit 1
}

docker run --rm --user postgres --entrypoint bash \
  -v "${EXT_V0}:/tmp/ext_v0:ro" -v "${EXT_V1}:/tmp/ext_v1:ro" \
  -v "${TEST}:/tmp/graph_test.sql:ro" "$IMAGE" -c '
  set -e
  B=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)  # works for the pg16/pg17 CI matrix
  PGC=$B/pg_config
  for e in v1 v0; do
    cp -r /tmp/ext_$e /tmp/build_$e && cd /tmp/build_$e
    echo "=== make ($e, stock PG17) ==="; make PG_CONFIG=$PGC 2>&1 | tail -2
    # the image chowns the extension/lib dirs to postgres (scripts/pg17/Dockerfile)
    make PG_CONFIG=$PGC install 2>&1 | tail -1
  done
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses=" -w start >/dev/null
  echo "=== run tests (stock PG17, BLCKSZ 8192) ==="
  $B/psql -p 5499 -d postgres -v ON_ERROR_STOP=1 -f /tmp/graph_test.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
'
echo "[pg17_graph_test] done"

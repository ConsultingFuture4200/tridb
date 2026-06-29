#!/usr/bin/env bash
#
# graph_edge_count_test.sh — build + test the store-wide gm_edge_count metapage counter (plan 006).
#
# Builds the graph_store_am access method (src/graph_store/) against the MSVBASE fork image via
# PGXS, then runs test/graph_edge_count_test.sql, which asserts gph_edge_count() tracks directed
# edges across all three gph_insert_edge branches (first-edge, append-with-room, chain-new-page)
# and rolls back atomically with the edge slot on ABORT. gm_edge_count is the FR-6 avg_out_degree
# source. Same build/fail-loud discipline as scripts/graph_am_test.sh.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_edge_count_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
SQL="$ROOT/test/graph_edge_count_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/edge_count.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build in a writable copy (mount is read-only). Fail LOUD on a build error.
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  echo "=== make (graph_store_am) ==="
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { tail -30 /tmp/make.log; echo "BUILD FAILED"; exit 1; }
  echo "=== make install ==="
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; echo "INSTALL FAILED"; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== gm_edge_count suite (plan 006) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/edge_count.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[graph_edge_count_test] done"

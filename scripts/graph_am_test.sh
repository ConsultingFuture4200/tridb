#!/usr/bin/env bash
#
# graph_am_test.sh — build + test the native adjacency-list graph store access method (DEV-1164).
#
# Builds the graph_store_am extension (src/graph_store/) against the MSVBASE fork image, runs the
# correctness suite (insert / incremental traversal / early termination / FR-7 abort), then
# RESTARTS the cluster and re-asserts the data survived — proving WAL-backed persistence.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_am_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
MAIN="$ROOT/test/graph_store_am_test.sql"
PERSIST="$ROOT/test/graph_store_am_persist.sql"
TRAV="$ROOT/test/graph_traversal_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${MAIN}:/tmp/main.sql:ro" -v "${PERSIST}:/tmp/persist.sql:ro" \
  -v "${TRAV}:/tmp/trav.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build (mount is read-only, so build in a writable copy). Fail LOUD on a build error:
  # piping make to `tail` previously masked the nonzero exit, so a broken build looked green.
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  echo "=== make ==="
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { tail -30 /tmp/make.log; echo "BUILD FAILED"; exit 1; }
  echo "=== make install ==="
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; echo "INSTALL FAILED"; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== correctness suite (DEV-1164) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/main.sql
  echo "=== traversal suite (gph_traverse, DEV-1165) ==="
  $B/psql -p 5432 -d postgres -c "CREATE DATABASE trav;" >/dev/null
  $B/psql -p 5432 -d trav -v ON_ERROR_STOP=1 -f /tmp/trav.sql
  echo "=== restart cluster (prove WAL persistence) ==="
  $B/pg_ctl -D $D -m fast -w restart >/dev/null 2>&1
  echo "=== re-assert after restart ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/persist.sql
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[graph_am_test] done"

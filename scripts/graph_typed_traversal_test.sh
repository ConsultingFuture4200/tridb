#!/usr/bin/env bash
#
# graph_typed_traversal_test.sh — build + test typed/directional/source-scoped traversal (plan 038).
#
# Builds the graph_store_am access method (src/graph_store/) against the MSVBASE fork image via
# PGXS, then runs test/graph_typed_traversal_test.sql, which asserts the edge_type dictionary, the
# 3-arg typed gph_insert_edge overload, and gph_traverse_typed(src, type_id, direction, source_id):
# one-type / any-type / wrong-type filtering, source scope, direction=in/both rejection, TR-1 early
# termination on the typed stream, and the default-path parity oracle. Same build/fail-loud
# discipline as scripts/graph_am_test.sh.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_typed_traversal_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
SQL="$ROOT/test/graph_typed_traversal_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/typed.sql:ro" "$IMAGE" -c '
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
  echo "=== typed traversal suite (plan 038, DEV-1350) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/typed.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[graph_typed_traversal_test] done"

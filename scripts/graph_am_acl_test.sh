#!/usr/bin/env bash
#
# graph_am_acl_test.sh — containment ACLs on the graph store container (advisor plan 026).
#
# Builds the graph_store_am access method (src/graph_store/) against the MSVBASE fork image via
# PGXS, then runs test/graph_am_acl_test.sql, which asserts as a NON-superuser probe role that
# the gstore container is not heap-readable and the gph_* mutators are not PUBLIC-executable,
# while the traversal read surface (gph_neighbors) stays open. Same build/fail-loud discipline
# as scripts/graph_am_test.sh. (This suite needs graph_store_am, which only the AM harnesses
# build — scripts/graph_test.sh installs the v0 graph_store extension, not the AM.)
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_am_acl_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
SQL="$ROOT/test/graph_am_acl_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/acl.sql:ro" "$IMAGE" -c '
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
  echo "=== containment ACL suite (plan 026) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/acl.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[graph_am_acl_test] done"

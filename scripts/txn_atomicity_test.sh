#!/usr/bin/env bash
#
# txn_atomicity_test.sh — FR-7 tri-store single-transaction atomicity (DEV-1166).
#
# Builds the v1 native graph_store_am extension (src/graph_store/) against the MSVBASE fork image
# and runs test/txn_atomicity_test.sql: a TRUE relational + HNSW-vector + native-graph single
# transaction, proving all three stores commit/abort as ONE unit (ONE txn manager, ONE WAL).
# This proves FR-7 on the v1 keystone (gph_insert_*), unlike the pre-existing v0-heap FR-7 test.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/txn_atomicity_test.sh [image]
#
# Build failures FAIL LOUD: make output goes to a log and a nonzero make exit aborts with the
# tail of that log (no `| tail` that would mask a broken build — see scripts/graph_am_test.sh).
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
TEST="$ROOT/test/txn_atomicity_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${TEST}:/tmp/atomicity.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  echo "=== make (graph_store_am) ==="
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== FR-7 tri-store atomicity suite ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/atomicity.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[txn_atomicity_test] done"

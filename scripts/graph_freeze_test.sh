#!/usr/bin/env bash
#
# graph_freeze_test.sh — the gph_freeze() anti-wraparound pass (advisor plan 036 / DEV-1347).
#
# Builds the graph_store_am access method (src/graph_store/) against the MSVBASE fork image via
# PGXS, then runs test/graph_freeze_test.sql, which ages rows below a horizon, runs
# graph_store.gph_freeze(horizon), and asserts visibility is byte-identical, an aborted insert
# stays invisible, relfrozenxid advanced, the re-run is idempotent, horizon validation fires, and
# the mutator is REVOKEd from PUBLIC. Same build/fail-loud discipline as scripts/graph_am_acl_test.sh.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker).
# Usage: scripts/graph_freeze_test.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"
SQL="$ROOT/test/graph_freeze_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/freeze.sql:ro" "$IMAGE" -c '
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
  echo "=== gph_freeze anti-wraparound suite (plan 036) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/freeze.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector'
echo "[graph_freeze_test] done"

#!/usr/bin/env bash
#
# join_order_cost_test.sh — build + test the FR-6 COST decision (advisor plan 031).
#
# Builds the join_order extension (src/planner/) against the MSVBASE fork image via PGXS, then
# runs test/join_order_cost_test.sql, which asserts the C port makes BIT-IDENTICAL decisions to the
# Python reference (src/planner/join_order_ref.py) for every case the contract pins.
#
# Requires the fork image (scripts/x86build.sh --docker, or scripts/gx10build.sh on the GX10).
# Usage: scripts/join_order_cost_test.sh [image]
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/planner"
# join_order_legstats.c (plan 006, now in OBJS) #includes src/graph_store/gph_page.h via the
# planner Makefile's `-I$(srcdir)/../graph_store`. Inside the container the build runs in
# /tmp/build, so that include resolves to /tmp/build/../graph_store == /tmp/graph_store — mount
# the graph_store headers there so the planner extension actually compiles.
GRAPH="$ROOT/src/graph_store"
SQL="$ROOT/test/join_order_cost_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run scripts/x86build.sh --docker (or gx10build.sh)" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${GRAPH}:/tmp/graph_store:ro" \
  -v "${SQL}:/tmp/join_order_cost_test.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build in a writable copy (the mount is read-only). Fail LOUD on a build error.
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  echo "=== make (join_order) ==="
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { tail -30 /tmp/make.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; echo "INSTALL FAILED"; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== FR-6 cost decision suite (plan 031) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/join_order_cost_test.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE "redirecting log|logging collector"
echo "[join_order_cost_test] PASS — cost decision + frozen-core-intact (plan 031)."

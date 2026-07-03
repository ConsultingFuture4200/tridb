#!/usr/bin/env bash
#
# join_order_integration_test.sh — FR-6 end-to-end (DEV-1285/DEV-1290): the join-order decision
# CHANGES EXECUTION, verified through the full lowering -> operator path.
#
# Builds BOTH extensions the lowering path spans — graph_store_ext (the canonical surface)
# and src/planner (the FROZEN decision core) — against the fork image (which must carry the
# DEV-1290 8-arg tjs()), then runs test/join_order_integration_test.sql: inverted-selectivity
# windows pick opposite drivers on BOTH companions (lowering decision + operator execution),
# and the forced bodies show materially different candidates-examined with identical answers.
#
# Requires the fork image (scripts/x86build.sh --docker, or scripts/gx10build.sh on the GX10).
# Usage: scripts/join_order_integration_test.sh [image]
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SURFACE="$ROOT/src/graph_store_ext"
PLANNER="$ROOT/src/planner"
# join_order_legstats.c #includes src/graph_store/gph_page.h via `-I$(srcdir)/../graph_store`
# (see scripts/join_order_test.sh) — mount the headers where that resolves from /tmp/planner.
GRAPH="$ROOT/src/graph_store"
SQL="$ROOT/test/join_order_integration_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run scripts/x86build.sh --docker (or gx10build.sh)" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${SURFACE}:/tmp/surface:ro" -v "${PLANNER}:/tmp/planner:ro" \
  -v "${GRAPH}:/tmp/graph_store:ro" \
  -v "${SQL}:/tmp/join_order_integration_test.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS-build both extensions in writable copies (mounts are read-only). Fail LOUD.
  cp -r /tmp/surface /tmp/build_surface && cd /tmp/build_surface
  echo "=== make (graph_store_ext) ==="
  make PG_CONFIG=$PGC >/tmp/make_s.log 2>&1 || { tail -30 /tmp/make_s.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/inst_s.log 2>&1 || { tail -20 /tmp/inst_s.log; echo "INSTALL FAILED"; exit 1; }
  cp -r /tmp/planner /tmp/build_planner && cd /tmp/build_planner
  echo "=== make (join_order) ==="
  make PG_CONFIG=$PGC >/tmp/make_p.log 2>&1 || { tail -30 /tmp/make_p.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/inst_p.log 2>&1 || { tail -20 /tmp/inst_p.log; echo "INSTALL FAILED"; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== FR-6 decision-changes-execution suite (DEV-1285/DEV-1290) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/join_order_integration_test.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE "redirecting log|logging collector"
echo "[join_order_integration_test] PASS — FR-6 decision changes execution end-to-end (DEV-1285/DEV-1290)."

#!/usr/bin/env bash
#
# join_order_lowering_test.sh — ADR-0011 Stage 2 (DEV-1285): the graph_query() lowering
# makes the FR-6 join-order decision at the call site and records it.
#
# Builds BOTH extensions the lowering path spans — graph_store_am (the v1 native AM hosting the canonical surface, ADR-0013 Stage B)
# and src/planner (the FROZEN decision core) — against the fork image, then runs
# test/join_order_lowering_test.sql: inverted-selectivity windows pick opposite orders
# through the full lowering, the decision is inert on execution (Stage 3 = DEV-1290), and
# the lowering degrades to the vector_first default without the join_order extension.
#
# Requires the fork image (scripts/x86build.sh --docker, or scripts/gx10build.sh on the GX10).
# Usage: scripts/join_order_lowering_test.sh [image]
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SURFACE="$ROOT/src/graph_store"   # v1 native AM hosts the canonical surface (ADR-0013 Stage B)
PLANNER="$ROOT/src/planner"
# join_order_legstats.c #includes src/graph_store/gph_page.h via `-I$(srcdir)/../graph_store`
# (see scripts/join_order_test.sh) — mount the headers where that resolves from /tmp/planner.
GRAPH="$ROOT/src/graph_store"
SQL="$ROOT/test/join_order_lowering_test.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run scripts/x86build.sh --docker (or gx10build.sh)" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${SURFACE}:/tmp/surface:ro" -v "${PLANNER}:/tmp/planner:ro" \
  -v "${GRAPH}:/tmp/graph_store:ro" \
  -v "${SQL}:/tmp/join_order_lowering_test.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS-build both extensions in writable copies (mounts are read-only). Fail LOUD.
  cp -r /tmp/surface /tmp/build_surface && cd /tmp/build_surface
  echo "=== make (graph_store_am) ==="
  make PG_CONFIG=$PGC >/tmp/make_s.log 2>&1 || { tail -30 /tmp/make_s.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/inst_s.log 2>&1 || { tail -20 /tmp/inst_s.log; echo "INSTALL FAILED"; exit 1; }
  cp -r /tmp/planner /tmp/build_planner && cd /tmp/build_planner
  echo "=== make (join_order) ==="
  make PG_CONFIG=$PGC >/tmp/make_p.log 2>&1 || { tail -30 /tmp/make_p.log; echo "BUILD FAILED"; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/inst_p.log 2>&1 || { tail -20 /tmp/inst_p.log; echo "INSTALL FAILED"; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  echo "=== Stage-2 lowering decision suite (DEV-1285) ==="
  $B/psql -p 5432 -d postgres -v ON_ERROR_STOP=1 -f /tmp/join_order_lowering_test.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE "redirecting log|logging collector"
echo "[join_order_lowering_test] PASS — Stage-2 decision made+recorded at the lowering (DEV-1285)."

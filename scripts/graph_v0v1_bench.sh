#!/usr/bin/env bash
#
# graph_v0v1_bench.sh — measured v0-vs-v1 graph-store microbench (advisor plan 016, spike).
#
# Builds BOTH graph stores against the MSVBASE fork image (v0 heap-backed extension in
# src/graph_store_ext/, v1 native access method in src/graph_store/), loads the SAME deterministic
# synthetic graph (50k vertices / 500k edges, one degree-5000 hub) into each — in its OWN database,
# because both extensions are relocatable=false, schema=graph_store and collide — and prints a
# comparison of bulk-load wall clock, neighbors() latency, and page reads.
#
# The numbers are the evidence base for ADR-0013 (the v0->v1 rewire decision). v1 ingest is EXPECTED
# to look bad until O(1) vid addressing lands (rider 1, graph_am.c:198) — that is the point.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker). ENGINE-GATED: does not run without it.
# Usage: scripts/graph_v0v1_bench.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V0="$ROOT/src/graph_store_ext"
EXT_V1="$ROOT/src/graph_store"
SQL="$ROOT/test/graph_v0v1_bench.sql"

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

docker run --rm --entrypoint bash \
  -v "${EXT_V0}:/tmp/ext_v0:ro" -v "${EXT_V1}:/tmp/ext_v1:ro" -v "${SQL}:/tmp/bench.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  # PGXS build both extensions (mounts are read-only, build in writable copies). Fail LOUD.
  for e in v0 v1; do
    cp -r /tmp/ext_$e /tmp/build_$e && cd /tmp/build_$e
    echo "=== make ($e) ==="
    make PG_CONFIG=$PGC >/tmp/make_$e.log 2>&1 || { tail -30 /tmp/make_$e.log; echo "BUILD FAILED ($e)"; exit 1; }
    make PG_CONFIG=$PGC install >/tmp/install_$e.log 2>&1 || { tail -20 /tmp/install_$e.log; echo "INSTALL FAILED ($e)"; exit 1; }
  done
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -c "CREATE DATABASE v0db;" >/dev/null
  $B/psql -p 5432 -d postgres -c "CREATE DATABASE v1db;" >/dev/null
  echo "=== load + probe v0 (heap-backed extension) ==="
  $B/psql -p 5432 -d v0db -v ON_ERROR_STOP=1 -v STORE=v0 -f /tmp/bench.sql
  echo "=== load + probe v1 (native access method) ==="
  $B/psql -p 5432 -d v1db -v ON_ERROR_STOP=1 -v STORE=v1 -f /tmp/bench.sql
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
' 2>&1 | tee /tmp/graph_v0v1_bench.out | grep -vE 'redirecting log|logging collector' || true

# Assemble the comparison table from the METRIC lines the SQL emitted. Each METRIC line is
# immediately followed by its value line (psql \echo pairs), so read them as pairs.
echo
echo "==================== v0 vs v1 comparison ===================="
awk '
  /^METRIC / { key=$2" "$3; getline val; printf "  %-40s %s\n", key, val; next }
' /tmp/graph_v0v1_bench.out 2>/dev/null || echo "  (no METRIC lines captured — image likely absent)"
echo "============================================================="
echo "[graph_v0v1_bench] done"

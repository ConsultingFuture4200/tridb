#!/usr/bin/env bash
#
# hotpot_stock_gate.sh — plan 095 spike: membership-vs-PPR HotpotQA recall gate on the
# STOCK engine. Mirrors scripts/pg17_graph_test.sh's docker pattern (build the v1 native AM
# + v0 heap-backed extension + tjs_pg against stock PG headers inside the image, throwaway
# 8KB-page cluster, unix-socket psql) but runs bench.hotpot_stock_gate's generated sweep SQL
# instead of a fixed test file, then parses+grades the captured output.
#
# Usage: scripts/hotpot_stock_gate.sh [image]
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V0="$ROOT/src/graph_store_ext"
EXT_V1="$ROOT/src/graph_store"
EXT_TJS="$ROOT/src/tjs_pg"
RESULTS="$ROOT/bench/results"
SQL="$RESULTS/hotpot_gate.sql"
LOG="$RESULTS/hotpot_gate.log"
PY=$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)

mkdir -p "$RESULTS"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t $IMAGE scripts/pg17/" >&2
  exit 1
}

echo "=== generating sweep SQL ==="
(cd "$ROOT" && "$PY" -m bench.hotpot_stock_gate --gen-sql "$SQL")

echo "=== running sweep on $IMAGE (this loads 1490 paragraphs + 150 queries + 1490 typed"
echo "    edges, then 2 modes x 2 k x 3 term_cond x 150 questions = 1800 tjs_open calls) ==="
docker run --rm --user postgres --entrypoint bash \
  -v "${EXT_V0}:/tmp/ext_v0:ro" -v "${EXT_V1}:/tmp/ext_v1:ro" \
  -v "${EXT_TJS}:/tmp/ext_tjs:ro" -v "${SQL}:/tmp/gate.sql:ro" "$IMAGE" -c '
  set -e
  B=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)
  PGC=$B/pg_config
  for e in v1 v0 tjs; do
    cp -r /tmp/ext_$e /tmp/build_$e && cd /tmp/build_$e
    echo "=== make ($e, stock PG) ==="
    make PG_CONFIG=$PGC >/tmp/make_$e.log 2>&1 || { tail -30 /tmp/make_$e.log; echo "BUILD FAILED ($e)"; exit 1; }
    make PG_CONFIG=$PGC install >/tmp/install_$e.log 2>&1 || { tail -20 /tmp/install_$e.log; echo "INSTALL FAILED ($e)"; exit 1; }
  done
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses= -c shared_buffers=512MB" -w start >/dev/null
  echo "=== run sweep ==="
  $B/psql -p 5499 -d postgres -v ON_ERROR_STOP=1 -f /tmp/gate.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' > "$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  echo "SWEEP FAILED — see $LOG" >&2
  tail -60 "$LOG" >&2
  exit $rc
fi
echo "=== sweep done -> $LOG ==="

echo "=== grading ==="
(cd "$ROOT" && "$PY" -m bench.hotpot_stock_gate --parse "$LOG" \
  --out "$RESULTS/hotpot_stock_gate.json" --md "$RESULTS/hotpot_stock_gate.md")
echo "[hotpot_stock_gate] done -> $RESULTS/hotpot_stock_gate.json, $RESULTS/hotpot_stock_gate.md"

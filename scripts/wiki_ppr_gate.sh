#!/usr/bin/env bash
#
# wiki_ppr_gate.sh — advisor plan 096: wiki-scale membership-vs-PPR held-out link
# prediction gate on the STOCK engine. Mirrors scripts/hotpot_stock_gate.sh's docker
# pattern (build the v1 native AM + v0 heap-backed extension + tjs_pg against stock PG
# headers inside the image, throwaway 8KB-page cluster, unix-socket psql) but loads the
# real 200k-article enwiki hyperlink slice instead of HotpotQA and runs the full
# mode x k x term_cond x budget x query sweep in ONE psql -f invocation against ONE
# server — load once, sweep with SETs, no per-point container restart.
#
# Usage: scripts/wiki_ppr_gate.sh [image] [n] [q]
#   n, q let the smoke run (--limit 5000, 20 queries) reuse this same script.
#
set -euo pipefail
IMAGE="${1:-tridb/pg17-unfork:dev}"
N="${2:-200000}"
Q="${3:-300}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_V0="$ROOT/src/graph_store_ext"
EXT_V1="$ROOT/src/graph_store"
EXT_TJS="$ROOT/src/tjs_pg"
RESULTS="$ROOT/bench/results"
SQL="$RESULTS/wiki_ppr_gate.sql"
META="$RESULTS/wiki_ppr_gate_meta.json"
LOG="$RESULTS/wiki_ppr_gate.log"
PY=$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)

mkdir -p "$RESULTS"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run: docker build -t $IMAGE scripts/pg17/" >&2
  exit 1
}

echo "=== generating load+sweep SQL (n=$N, q=$Q) ==="
(cd "$ROOT" && "$PY" -m bench.wiki_ppr_gate --n "$N" --q "$Q" \
  --gen-sql "$SQL" --meta-out "$META")

echo "=== running sweep on $IMAGE (one persistent server, one psql -f: load once," \
     "then SETs per grid point) ==="
docker run --rm --user postgres --shm-size=2g --entrypoint bash \
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
  $B/pg_ctl -D $D -o "-p 5499 -c listen_addresses= -c shared_buffers=2GB -c work_mem=64MB -c maintenance_work_mem=512MB" -w start >/dev/null
  echo "=== run load + sweep ==="
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
(cd "$ROOT" && "$PY" -m bench.wiki_ppr_gate --parse "$LOG" --meta "$META" \
  --out "$RESULTS/wiki_ppr_gate.json" --md "$RESULTS/wiki_ppr_gate.md")
echo "[wiki_ppr_gate] done -> $RESULTS/wiki_ppr_gate.json, $RESULTS/wiki_ppr_gate.md"

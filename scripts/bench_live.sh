#!/usr/bin/env bash
#
# bench_live.sh — LIVE TriDB Phase-3 benchmark (DEV-1172 harness + DEV-1173 report).
#
# Runs the ONE canonical query (spec §5) on the LIVE forked-MSVBASE engine
# (tridb/msvbase:dev) over a real corpus across many queries, capturing the REAL
# TriDB-side numbers (tjs() answer set, tjs_candidates_examined() -> SM-3, EXPLAIN
# ANALYZE Execution Time -> SM-2 TriDB-side) plus an exact in-DB ground-truth
# oracle for SM-4 parity. It then derives SM-1..SM-5 against the in-process
# baseline materialization model on the SAME corpus and renders the HTML report.
#
# Structure mirrors scripts/tjs_test.sh: in the image, PGXS-build + install
# src/graph_store (the v1 native AM, graph_store_am — ADR-0013 Stage B) into a throwaway cluster, initdb, run the generated SQL.
#
# Requires tridb/msvbase:dev (scripts/x86build.sh --docker) and a host Python with
# numpy (the repo .venv). The TriDB side is 100% live-measured here; the baseline
# side is the documented in-process model. A fair head-to-head SM-2 needs the
# multi-system stack (`make baseline-up`) + a live baseline driver, or the GX10
# 128 GB headline run — both TABLED for this standin run.
#
# Usage: scripts/bench_live.sh [image]
#   env: BENCH_ENTITIES BENCH_DIM BENCH_HUBS BENCH_FANOUT BENCH_QUERIES BENCH_K
#        BENCH_WINDOW BENCH_SEED  (sensible defaults below)
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"   # v1 native AM (graph_store_am, ADR-0013 Stage B)

ENTITIES="${BENCH_ENTITIES:-2000}"
DIM="${BENCH_DIM:-32}"
HUBS="${BENCH_HUBS:-12}"
FANOUT="${BENCH_FANOUT:-150}"
QUERIES="${BENCH_QUERIES:-12}"
K="${BENCH_K:-5}"
WINDOW="${BENCH_WINDOW:-600}"
SEED="${BENCH_SEED:-42}"

OUTDIR="$ROOT/bench/results"
WORK="$(mktemp -d)"
SQL="$WORK/bench_live.sql"
MANIFEST="$WORK/manifest.json"
RAW="$WORK/bench_raw.txt"
trap 'rm -rf "$WORK"' EXIT

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

# Pick the host python (prefer repo .venv with numpy).
PY="python3"
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

echo "[bench_live] generating corpus SQL (entities=$ENTITIES dim=$DIM hubs=$HUBS fanout=$FANOUT queries=$QUERIES k=$K)"
"$PY" "$ROOT/tools/bench_corpus.py" \
  --entities "$ENTITIES" --dim "$DIM" --hubs "$HUBS" --fanout "$FANOUT" \
  --queries "$QUERIES" --k "$K" --window "$WINDOW" --seed "$SEED" \
  --sql-out "$SQL" --manifest-out "$MANIFEST"

echo "[bench_live] running canonical query on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/bench_live.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -q -f /tmp/bench_live.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$RAW"

if ! grep -q "#BENCH DONE" "$RAW"; then
  echo "[bench_live] live run did NOT complete (no #BENCH DONE) — see output above" >&2
  exit 1
fi

mkdir -p "$OUTDIR"
echo "[bench_live] deriving SM-1..SM-5 + rendering report"
"$PY" -m bench.live_report \
  --bench-out "$RAW" --manifest "$MANIFEST" --seed "$SEED" \
  --json-out "$OUTDIR/bench_live_metrics.json" \
  --html-out "$OUTDIR/report_live.html"
rc=$?

# Keep an auditable copy of the live transcript: the meaningful lines (the #BENCH
# observations + each query's EXPLAIN ANALYZE plan + the harness log), dropping the
# thousands of corpus INSERT echoes so the committed artifact stays reviewable.
grep -E '#BENCH|QUERY PLAN|Function Scan|Execution Time|Planning Time|^\[bench' \
  "$RAW" > "$OUTDIR/bench_live_raw.txt" || cp "$RAW" "$OUTDIR/bench_live_raw.txt"
echo "[bench_live] artifacts in $OUTDIR (metrics json, report html, raw transcript)"
exit $rc

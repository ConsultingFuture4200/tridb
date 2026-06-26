#!/usr/bin/env bash
#
# bench_sm2.sh — FAIR SM-2 head-to-head: LIVE TriDB vs the LIVE multi-system
# baseline (Milvus + Neo4j + Postgres), DEV-1171.
#
# Both sides run the IDENTICAL corpus + query set + k (driven from the SAME
# tools/bench_corpus_shared deterministic generator with the SAME seed/params),
# and both are measured the SAME way:
#
#   * client-side END-TO-END wall-clock per query, over WARM connections,
#     one-time load + index build EXCLUDED, MEDIAN of >=N measured runs.
#
#   TriDB side : psql `\timing on` round-trip time of the canonical tjs() query
#                inside the tridb/msvbase:dev throwaway cluster (warm connection;
#                N repeated runs after a warm-up). See tools/bench_sm2_corpus.py.
#   baseline   : Python time.perf_counter() around the realized canonical query
#                across the three live systems, merged app-side (warm clients;
#                N runs after a warm-up). See baseline/sm2.py.
#
# Then bench/sm2_compare.py computes SM-2 = fraction of queries where TriDB
# end-to-end median latency < baseline end-to-end median latency (target >=80%),
# the median/mean ratio, per-query numbers, intermediate sizes, and answer parity.
#
# REQUIRES:
#   * tridb/msvbase:dev image (scripts/x86build.sh --docker)
#   * the multi-system baseline UP + healthy (make baseline-up)
#   * a host python (repo .venv) with numpy + pymilvus + neo4j + psycopg
#
# Usage: scripts/bench_sm2.sh [image]
#   env: BENCH_ENTITIES BENCH_DIM BENCH_HUBS BENCH_FANOUT BENCH_QUERIES BENCH_K
#        BENCH_WINDOW BENCH_SEED SM2_RUNS  (defaults below)
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store_ext"

ENTITIES="${BENCH_ENTITIES:-2000}"
DIM="${BENCH_DIM:-32}"
HUBS="${BENCH_HUBS:-12}"
FANOUT="${BENCH_FANOUT:-150}"
QUERIES="${BENCH_QUERIES:-12}"
K="${BENCH_K:-5}"
WINDOW="${BENCH_WINDOW:-600}"
SEED="${BENCH_SEED:-42}"
RUNS="${SM2_RUNS:-7}"
TERMCOND="${BENCH_TERMCOND:-0}"   # tjs() operating point (0 -> engine default 50); pin it for a fair SM-2

OUTDIR="$ROOT/bench/results"
WORK="$(mktemp -d)"
SQL="$WORK/sm2.sql"
MANIFEST="$WORK/manifest.json"
TRIDB_RAW="$WORK/tridb_sm2_raw.txt"
BASELINE_JSON="$WORK/baseline_sm2.json"
trap 'rm -rf "$WORK"' EXIT

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run scripts/x86build.sh --docker" >&2; exit 1; }

PY="python3"
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

echo "[sm2] generating identical corpus (entities=$ENTITIES dim=$DIM hubs=$HUBS fanout=$FANOUT queries=$QUERIES k=$K seed=$SEED runs=$RUNS term_cond=$TERMCOND)"
"$PY" "$ROOT/tools/bench_sm2_corpus.py" \
  --entities "$ENTITIES" --dim "$DIM" --hubs "$HUBS" --fanout "$FANOUT" \
  --queries "$QUERIES" --k "$K" --window "$WINDOW" --seed "$SEED" --runs "$RUNS" \
  --term-cond "$TERMCOND" \
  --sql-out "$SQL" --manifest-out "$MANIFEST"

# ----- TriDB side: live tjs() client wall-clock inside the engine image --------
echo "[sm2] TriDB side: timing canonical tjs() on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/sm2.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  # -q so only \echo markers + the psql `Time:` lines surface (no row echoes).
  $B/psql -p 5432 -d postgres -q -f /tmp/sm2.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$TRIDB_RAW"

if ! grep -q "#SM2 DONE" "$TRIDB_RAW"; then
  echo "[sm2] TriDB live timing did NOT complete (no #SM2 DONE) — see output above" >&2
  exit 1
fi

# ----- baseline side: live multi-system end-to-end wall-clock ------------------
echo "[sm2] baseline side: timing the live multi-system stack (Milvus+Neo4j+Postgres)"
"$PY" "$ROOT/baseline/sm2.py" \
  --manifest "$MANIFEST" --seed "$SEED" --k "$K" --runs "$RUNS" \
  --out "$BASELINE_JSON"

# ----- compare ----------------------------------------------------------------
mkdir -p "$OUTDIR"
echo "[sm2] computing SM-2 head-to-head"
"$PY" -m bench.sm2_compare \
  --tridb-raw "$TRIDB_RAW" --baseline-json "$BASELINE_JSON" \
  --manifest "$MANIFEST" \
  --json-out "$OUTDIR/sm2_metrics.json" \
  --md-out "$ROOT/docs/benchmark_sm2_v0.1.0.md"
rc=$?

# keep an auditable copy of the TriDB timing transcript (markers + Time: lines).
grep -E '#SM2|Time:' "$TRIDB_RAW" > "$OUTDIR/sm2_tridb_raw.txt" || cp "$TRIDB_RAW" "$OUTDIR/sm2_tridb_raw.txt"
cp "$BASELINE_JSON" "$OUTDIR/sm2_baseline.json"
echo "[sm2] artifacts in $OUTDIR (sm2_metrics.json, sm2_tridb_raw.txt, sm2_baseline.json) + docs/benchmark_sm2_v0.1.0.md"
exit $rc

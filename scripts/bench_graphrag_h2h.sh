#!/usr/bin/env bash
#
# bench_graphrag_h2h.sh — REAL-workload head-to-head (GTM #1): the canonical fused
# tjs() query on the LIVE forked-MSVBASE engine vs the tuned multi-store baseline
# (Milvus + Neo4j + app-side rerank), on the SAME HotpotQA corpus + queries + k,
# both measured client-side end-to-end (warm, median of N). Closes the GTM
# "strawman baseline" gap on a recognized public dataset.
#
# REQUIRES: data/hotpot/manifest.json (make fetch-hotpot + hotpot_corpus), the
# engine image (scripts/x86build.sh --docker), and the baseline stack UP
# (make baseline-up — Milvus + Neo4j healthy).
#
# Usage: scripts/bench_graphrag_h2h.sh [image]
#   env: H2H_K=10 H2H_RUNS=5 H2H_TERMCOND=0
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store"   # v1 native AM (graph_store_am, ADR-0013 Stage B)
cd "$ROOT"

K="${H2H_K:-10}"; RUNS="${H2H_RUNS:-5}"; TERMCOND="${H2H_TERMCOND:-0}"
MANIFEST="$ROOT/data/hotpot/manifest.json"
PY="python3"; [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

[ -f "$MANIFEST" ] || { echo "manifest missing — run: make fetch-hotpot && python -m tools.hotpot_corpus" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built (ENGINE-GATED)" >&2; exit 1; }
docker ps --format '{{.Names}}' | grep -q tridb-baseline-milvus || { echo "baseline stack not up — run make baseline-up" >&2; exit 1; }

OUTDIR="$ROOT/bench/results"; mkdir -p "$OUTDIR"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
SQL="$WORK/h2h.sql"; RAW="$WORK/h2h_tridb_raw.txt"

echo "[h2h] emitting canonical tjs() \\timing SQL (k=$K runs=$RUNS term_cond=$TERMCOND)"
"$PY" -m bench.h2h_report --manifest "$MANIFEST" --k "$K" --runs "$RUNS" --termcond "$TERMCOND" --emit-sql "$SQL"

echo "[h2h] TriDB side: timing tjs() on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/h2h.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin; PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -f /tmp/h2h.sql
  rc=$?; $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true; exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$RAW" >/dev/null

grep -q "#H2H DONE" "$RAW" || { echo "[h2h] TriDB run did NOT complete (no #H2H DONE)" >&2; tail -20 "$RAW" >&2; exit 1; }

echo "[h2h] baseline side: live Milvus+Neo4j multi-store + grading both vs gold"
"$PY" -m bench.h2h_report --manifest "$MANIFEST" --k "$K" --runs "$RUNS" \
  --tridb-raw "$RAW" \
  --json-out "$OUTDIR/h2h_metrics.json" \
  --md-out "$ROOT/docs/benchmark_h2h_v0.1.0.md"
grep -E '#H2H|Time:' "$RAW" > "$OUTDIR/h2h_tridb_raw.txt" || cp "$RAW" "$OUTDIR/h2h_tridb_raw.txt"
echo "[h2h] artifacts in $OUTDIR + docs/benchmark_h2h_v0.1.0.md"

#!/usr/bin/env bash
#
# bench_filtered.sh — LIVE filtered vector-search benchmark (VectorDBBench IntFilter
# methodology) on the forked-MSVBASE engine. The on-thesis vector axis: a relational
# predicate fused with ANN ordering + early termination (TR-1), measured as recall@k
# and latency across filter SELECTIVITY on REAL SIFT-128 vectors.
#
# Sibling of bench_sm2.sh / bench_public.sh: generate corpus+oracle host-side ->
# run the engine recipe in ONE container -> grade host-side into bench/results/.
#
# WHAT IS MEASURED vs GATED
#   host-side: the EXACT filtered top-k oracle (tools/filtered_corpus.py, numpy).
#   LIVE: engine recall + median latency per selectivity (this script). On the x86
#     standin keep FILT_LIMIT small; the at-scale headline (full SIFT-1M, and the
#     VDBB 768D1M1P Cohere case via bench/vdbb_tridb.py) runs on the GX10 (NEON HNSW).
#
# Usage: scripts/bench_filtered.sh [image]
#   env: FILT_LIMIT=50000 FILT_QUERIES=20 FILT_K=10 FILT_RUNS=5 FILT_SEED=42
#        FILT_SEL="1 10 50 99"   HDF5=data/public/sift-128-euclidean.hdf5
#   GX10 headline: FILT_LIMIT=1000000 scripts/bench_filtered.sh tridb/msvbase:gx10
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT="$ROOT/src/graph_store_ext"
cd "$ROOT"

LIMIT="${FILT_LIMIT:-50000}"
QUERIES="${FILT_QUERIES:-20}"
K="${FILT_K:-10}"
RUNS="${FILT_RUNS:-5}"
SEED="${FILT_SEED:-42}"
SEL="${FILT_SEL:-1 10 50 99}"
HDF5="${HDF5:-$ROOT/data/public/sift-128-euclidean.hdf5}"

PY="python3"; [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

[ -f "$HDF5" ] || { echo "dataset $HDF5 missing — run: make fetch-dataset PUBLIC_DATASET=sift-128-euclidean" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not built — the live run is ENGINE-GATED" >&2; exit 1; }

OUTDIR="$ROOT/bench/results"; mkdir -p "$OUTDIR"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
SQL="$WORK/filtered.sql"; MANIFEST="$WORK/filtered_manifest.json"; RAW="$WORK/filtered_raw.txt"

echo "[filtered] building corpus+oracle (limit=$LIMIT q=$QUERIES k=$K runs=$RUNS sel='$SEL' seed=$SEED)"
"$PY" -m tools.filtered_corpus --hdf5 "$HDF5" --limit "$LIMIT" --queries "$QUERIES" \
  --k "$K" --runs "$RUNS" --seed "$SEED" --selectivities $SEL \
  --sql-out "$SQL" --manifest-out "$MANIFEST"

echo "[filtered] running filtered ANN on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/filtered.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin; PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  make PG_CONFIG=$PGC >/tmp/make.log 2>&1 || { echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; }
  make PG_CONFIG=$PGC install >/tmp/install.log 2>&1 || { echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; }
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -q -f /tmp/filtered.sql
  rc=$?; $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true; exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$RAW"

grep -q "#FILT DONE" "$RAW" || { echo "[filtered] live run did NOT complete (no #FILT DONE)" >&2; exit 1; }

echo "[filtered] grading recall@$K + latency vs the exact filtered oracle"
"$PY" -m bench.filtered_report --raw "$RAW" --manifest "$MANIFEST" \
  --json-out "$OUTDIR/filtered_metrics.json" \
  --md-out "$ROOT/docs/benchmark_filtered_v0.1.0.md"
grep -E '#FILT|Time:' "$RAW" > "$OUTDIR/filtered_raw.txt" || cp "$RAW" "$OUTDIR/filtered_raw.txt"
echo "[filtered] artifacts in $OUTDIR + docs/benchmark_filtered_v0.1.0.md"

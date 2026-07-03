#!/usr/bin/env bash
#
# bench_public.sh — one-command LIVE benchmark on a RECOGNIZED PUBLIC dataset (GTM make-or-break).
#
# This is the launch artifact docs/gtm_opensource_v0.1.0.md names as make-or-break: the canonical
# tjs() query, run on the LIVE forked-MSVBASE engine, over a topical graph synthesized on REAL public
# embeddings (default gist-960-euclidean: dim 960, L2), with recall@k graded against an exact numpy
# oracle. Sibling of scripts/bench_live.sh + scripts/bench_gx10_sweep.sh: image guard -> generate
# SQL + oracle manifest with a Python tool -> run the engine recipe in ONE container -> grade
# host-side into bench/results/.
#
# WHAT IS MEASURED vs GATED
#   measurable host-side (no engine): the EXACT recall oracle over the real public embeddings
#     (tools/real_corpus.py: numpy top-k). The dataset DOWNLOAD is network-gated (tools/fetch_dataset.py).
#   LIVE / GX10-/stack-gated: the engine run itself (tjs() answer set, tjs_candidates_examined(),
#     EXPLAIN ANALYZE latency). This script GUARDS on the engine image like its siblings and refuses
#     to fabricate a live number off-target.
#
# Usage: scripts/bench_public.sh [image]
#   env (defaults: small public-dataset slice so the live run is bounded on a shared box):
#     PUBLIC_DATASET=gist-960-euclidean   # which pinned set (tools/fetch_dataset.py REGISTRY)
#     PUBLIC_LIMIT=20000                  # take first N rows of the train matrix (0 = all)
#     PUBLIC_HUBS=16 PUBLIC_FANOUT=200 PUBLIC_QUERIES=8 PUBLIC_K=10 PUBLIC_WINDOW=600 PUBLIC_SEED=42
#   Headline (GTM gate — run on a quiet GX10): PUBLIC_LIMIT=100000 make bench-public
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The SQL does CREATE EXTENSION graph_store_am, so we PGXS-build the src/graph_store tree in the image,
# exactly as scripts/bench_live.sh / bench_gx10_sweep.sh do.
EXT="$ROOT/src/graph_store"   # v1 native AM (graph_store_am, ADR-0013 Stage B)
# Run real_corpus as `-m tools.real_corpus` from the repo root so its `from tools.bench_corpus
# import build_sql` (the shared canonical SQL emitter) resolves — running it by path does not.
cd "$ROOT"

DATASET="${PUBLIC_DATASET:-gist-960-euclidean}"
LIMIT="${PUBLIC_LIMIT:-20000}"
HUBS="${PUBLIC_HUBS:-16}"
FANOUT="${PUBLIC_FANOUT:-200}"
QUERIES="${PUBLIC_QUERIES:-8}"
K="${PUBLIC_K:-10}"
WINDOW="${PUBLIC_WINDOW:-600}"
SEED="${PUBLIC_SEED:-42}"

CACHE="$ROOT/data/public"
HDF5="$CACHE/${DATASET}.hdf5"

OUTDIR="$ROOT/bench/results"
WORK="$(mktemp -d)"
SQL="$WORK/bench_public.sql"
MANIFEST="$WORK/public_manifest.json"
RAW="$WORK/bench_public_raw.txt"
trap 'rm -rf "$WORK"' EXIT

# Pick the host python (prefer repo .venv with numpy + h5py for the real-dataset loader/oracle).
PY="python3"
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

# --- DATASET guard: the file must be present (download is network-gated; we never fetch here) ----
if [ ! -f "$HDF5" ]; then
  echo "[bench_public] dataset not present: $HDF5" >&2
  echo "[bench_public] fetch it first (network-gated, not run by this script):" >&2
  echo "               make fetch-dataset            # or" >&2
  echo "               $PY -m tools.fetch_dataset --dataset $DATASET" >&2
  exit 1
fi

# --- ENGINE guard: the live run needs the forked-MSVBASE image (GX10/stack-gated) ---------------
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[bench_public] engine image '$IMAGE' not built — the LIVE run is ENGINE-GATED." >&2
  echo "[bench_public] build it on a supported host: scripts/x86build.sh --docker (x86) /" >&2
  echo "               scripts/gx10build.sh (GX10). The dataset + oracle are ready; only the" >&2
  echo "               live engine measurement is gated." >&2
  exit 1
fi

# --- generate the canonical SQL + exact numpy oracle manifest over the REAL public embeddings ---
echo "[bench_public] generating canonical SQL + oracle over $DATASET" \
     "(limit=$LIMIT hubs=$HUBS fanout=$FANOUT queries=$QUERIES k=$K window=$WINDOW seed=$SEED)"
EXTRA=()
if [ "${LIMIT}" != "0" ]; then EXTRA+=(--limit "$LIMIT"); fi
"$PY" -m tools.real_corpus \
  --vectors "$HDF5" --hdf5-dataset train \
  --hubs "$HUBS" --fanout "$FANOUT" --queries "$QUERIES" --k "$K" \
  --window "$WINDOW" --seed "$SEED" \
  "${EXTRA[@]}" \
  --sql-out "$SQL" --manifest-out "$MANIFEST"

# --- run the canonical query on the LIVE engine (mirrors scripts/bench_live.sh exactly) ---------
echo "[bench_public] running the canonical query on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/bench_public.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  $B/psql -p 5432 -d postgres -q -f /tmp/bench_public.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$RAW"

if ! grep -q "#BENCH DONE" "$RAW"; then
  echo "[bench_public] live run did NOT complete (no #BENCH DONE) — see output above" >&2
  exit 1
fi

mkdir -p "$OUTDIR"
echo "[bench_public] grading engine recall@k vs the exact numpy oracle"
# real_corpus --report-recall parses the live #BENCH TRIDB_RESULT transcript and grades it against
# the manifest's exact oracle (report_recall). Tee the human-readable summary AND keep it as an artifact.
"$PY" -m tools.real_corpus --report-recall \
  --manifest "$MANIFEST" --results "$RAW" | tee "$OUTDIR/bench_public_recall.txt"

# Keep an auditable copy of the live transcript: the #BENCH observations + each query's EXPLAIN
# ANALYZE plan, dropping the corpus INSERT echoes so the committed artifact stays reviewable.
grep -E '#BENCH|QUERY PLAN|Function Scan|Execution Time|Planning Time|^\[bench' \
  "$RAW" > "$OUTDIR/bench_public_raw.txt" || cp "$RAW" "$OUTDIR/bench_public_raw.txt"
cp "$MANIFEST" "$OUTDIR/bench_public_manifest.json"
echo "[bench_public] artifacts in $OUTDIR (bench_public_recall.txt, bench_public_raw.txt, bench_public_manifest.json)"
echo "[bench_public] NOTE: recall@k above is LIVE-engine vs the exact oracle on REAL public embeddings."
echo "[bench_public]       Latency (EXPLAIN ANALYZE) is in bench_public_raw.txt; a fair multi-store"
echo "[bench_public]       SM-2 head-to-head needs 'make baseline-up' + the tuned baseline (baseline/TUNING.md)."

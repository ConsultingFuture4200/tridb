#!/usr/bin/env bash
#
# bench_gx10_sweep.sh — one-command LIVE HNSW index-quality x term_cond sweep (DEV-1286).
#
# Codifies the previously hand-built recipe that produced bench/results/neon_sweep_* into a
# committed, reproducible script — the "one-command repro" the GTM plan (docs/gtm_opensource_v0.1.0.md)
# names as launch make-or-break. Mirrors scripts/bench_live.sh: image guard -> generate SQL+manifest
# with a Python tool -> run the engine recipe in ONE container -> grade host-side into bench/results/.
#
# What it measures (per index config x term_cond, all LIVE on the forked-MSVBASE engine):
#   recall@k (vs an exact host-side numpy oracle), corpus examined-% (tjs_candidates_examined()),
#   EXPLAIN ANALYZE Execution Time, and CREATE INDEX build time. tools/sweep_corpus.py emits the
#   self-contained sweep SQL + the numpy oracle manifest, and grades the captured transcript.
#
# GX10/ENGINE-GATED: needs the tridb/msvbase:* image (scripts/x86build.sh --docker / gx10build.sh).
# The image's vectordb.so is already built from the committed fork-patch chain
# (scripts/lib/msvbase_patches.sh — incl. tridb_neon_l2_distance + tridb_hnsw_reloptions), so the
# DEFAULT path uses the engine AS BUILT and does NOT rebuild it. See the optional in-image refresh
# block below for the only case where a vectordb.so rebuild happens.
#
# Usage: scripts/bench_gx10_sweep.sh [image]
#   env (defaults are the args that produced the committed 20k/128 result):
#     SWEEP_ENTITIES=20000 SWEEP_DIM=128 SWEEP_HUBS=16 SWEEP_FANOUT=200 SWEEP_QUERIES=8
#     SWEEP_K=10 SWEEP_INDEX_CONFIGS="16:200,32:400" SWEEP_TERMCONDS="20,50,200,1000" SWEEP_SEED=42
#   Headline (GTM gate, DEV-1286 — run on a quiet GX10):
#     SWEEP_ENTITIES=100000 SWEEP_DIM=768 make sweep
#
set -euo pipefail
IMAGE="${1:-tridb/msvbase:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The sweep SQL does CREATE EXTENSION graph_store, so we need the graph_store_ext source tree
# (extension name "graph_store") — the same tree scripts/bench_live.sh PGXS-builds in the image.
EXT="$ROOT/src/graph_store_ext"

ENTITIES="${SWEEP_ENTITIES:-20000}"
DIM="${SWEEP_DIM:-128}"
HUBS="${SWEEP_HUBS:-16}"
FANOUT="${SWEEP_FANOUT:-200}"
QUERIES="${SWEEP_QUERIES:-8}"
K="${SWEEP_K:-10}"
INDEX_CONFIGS="${SWEEP_INDEX_CONFIGS:-16:200,32:400}"
TERMCONDS="${SWEEP_TERMCONDS:-20,50,200,1000}"
SEED="${SWEEP_SEED:-42}"

OUTDIR="$ROOT/bench/results"
WORK="$(mktemp -d)"
SQL="$WORK/sweep.sql"
MANIFEST="$WORK/sweep_manifest.json"
RAW="$WORK/sweep_raw.txt"
trap 'rm -rf "$WORK"' EXIT

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "image $IMAGE not built — run scripts/x86build.sh --docker / gx10build.sh" >&2; exit 1; }

# Pick the host python (prefer repo .venv with numpy — sweep_corpus needs numpy for the oracle).
PY="python3"
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

echo "[bench_gx10_sweep] generating sweep SQL + oracle manifest" \
     "(entities=$ENTITIES dim=$DIM hubs=$HUBS fanout=$FANOUT queries=$QUERIES k=$K" \
     "configs=$INDEX_CONFIGS term_conds=$TERMCONDS seed=$SEED)"
"$PY" "$ROOT/tools/sweep_corpus.py" \
  --entities "$ENTITIES" --dim "$DIM" --hubs "$HUBS" --fanout "$FANOUT" \
  --queries "$QUERIES" --k "$K" --index-configs "$INDEX_CONFIGS" \
  --term-conds "$TERMCONDS" --seed "$SEED" \
  --sql-out "$SQL" --manifest-out "$MANIFEST"

echo "[bench_gx10_sweep] running the NEON+reloptions sweep on the LIVE engine ($IMAGE)"
docker run --rm --entrypoint bash \
  -v "${EXT}:/tmp/ext:ro" -v "${SQL}:/tmp/sweep.sql:ro" "$IMAGE" -c '
  set -e
  B=/u01/app/postgres/product/13.4/bin
  PGC=$B/pg_config

  # --- engine: prefer the image vectordb.so AS BUILT (committed patch chain) ----------------
  # The image vectordb.so is built at image-build time from scripts/lib/msvbase_patches.sh, which
  # includes tridb_neon_l2_distance.patch (DEV-1234) + tridb_hnsw_reloptions.patch (DEV-1286).
  # So in the normal case we do NOT rebuild it — we run the engine exactly as the committed patch
  # chain produced it (no parallel ad-hoc rebuild that could rot vs the chain).
  #
  # OPTIONAL in-image refresh: the gx10 image keeps the MSVBASE build tree at /tmp/vectordb
  # (build dir /tmp/vectordb/build). If that tree is present we do an INCREMENTAL `make` there and
  # reinstall vectordb.so. WHY: it lets a maintainer pick up a vectordb source change (e.g. a freshly
  # re-applied reloptions/NEON patch) without a full multi-hour image rebuild — the same refresh the
  # original hand-built committed run used. Guarded so the default (no persistent tree) path is a
  # clean no-op and never silently builds a divergent .so.
  # Opt-in ONLY (SWEEP_REFRESH_VECTORDB=1). By default the image .so IS the build we want (the
  # committed patch chain, or a full gx10build --image rebuild), so no refresh is needed — and the
  # incremental `make install` reinstalls a ROOT-owned vectordb.control that the non-root container
  # user cannot chmod ("Operation not permitted"), which would abort a sweep on a freshly-rebuilt
  # image for no benefit. Reserve the refresh for the maintainer picking up a bare vectordb source
  # change against an OLD image without a multi-hour rebuild.
  if [ "${SWEEP_REFRESH_VECTORDB:-0}" = "1" ] && [ -d /tmp/vectordb/build ]; then
    echo "#SWEEP NOTE refreshing vectordb.so from persistent /tmp/vectordb/build (incremental, opt-in)"
    if ! make -C /tmp/vectordb/build vectordb >/tmp/vbuild.log 2>&1; then
      echo "VECTORDB REBUILD FAILED:"; tail -40 /tmp/vbuild.log; exit 1; fi
    if ! make -C /tmp/vectordb/build install >/tmp/vinstall.log 2>&1; then
      echo "VECTORDB INSTALL FAILED:"; tail -40 /tmp/vinstall.log; exit 1; fi
  else
    echo "#SWEEP NOTE using image vectordb.so as built (committed patch chain; no rebuild)"
  fi

  # --- graph_store_ext: the image ships vectordb but NOT graph_store; PGXS-build+install it -----
  # Same `make PG_CONFIG=$PGC [install]` pattern as scripts/bench_live.sh / crash_recovery_test.sh.
  echo "#SWEEP NOTE building graph_store_ext (PGXS)"
  cp -r /tmp/ext /tmp/build && cd /tmp/build
  if ! make PG_CONFIG=$PGC >/tmp/make.log 2>&1; then echo "BUILD FAILED:"; tail -40 /tmp/make.log; exit 1; fi
  if ! make PG_CONFIG=$PGC install >/tmp/install.log 2>&1; then echo "INSTALL FAILED:"; tail -40 /tmp/install.log; exit 1; fi

  # --- fresh throwaway cluster, load corpus, run the sweep in one psql session -----------------
  D=/tmp/pg; rm -rf $D; mkdir -p $D
  $B/initdb -A trust -D $D >/dev/null 2>&1
  $B/pg_ctl -D $D -o "-p 5432" -w start >/dev/null 2>&1
  # -q keeps the transcript to #SWEEP / Time: / Execution Time: lines the grader parses.
  $B/psql -p 5432 -d postgres -q -f /tmp/sweep.sql
  rc=$?
  $B/pg_ctl -D $D -m fast stop >/dev/null 2>&1 || true
  exit $rc
' 2>&1 | grep -vE 'redirecting log|logging collector' | tee "$RAW"

if ! grep -q "#SWEEP DONE" "$RAW"; then
  echo "[bench_gx10_sweep] sweep did NOT complete (no #SWEEP DONE) — see output above" >&2
  exit 1
fi

mkdir -p "$OUTDIR"
echo "[bench_gx10_sweep] grading transcript -> $OUTDIR/neon_sweep_metrics.json"
"$PY" "$ROOT/tools/sweep_corpus.py" --report "$RAW" --manifest "$MANIFEST" \
  > "$OUTDIR/neon_sweep_metrics.json"

# Keep an auditable copy of the transcript: the #SWEEP observations + build/exec timings, dropping
# the corpus INSERT echoes so the committed artifact stays reviewable (mirrors bench_live.sh).
grep -E '#SWEEP|Time:|Execution Time:' "$RAW" > "$OUTDIR/neon_sweep_raw.txt" || cp "$RAW" "$OUTDIR/neon_sweep_raw.txt"
cp "$MANIFEST" "$OUTDIR/sweep_manifest.json"
echo "[bench_gx10_sweep] artifacts in $OUTDIR (neon_sweep_metrics.json, neon_sweep_raw.txt, sweep_manifest.json)"

#!/usr/bin/env bash
#
# gpu_build_index.sh — OFFLINE GPU index build for TriDB (Plan 008, Step 3). GX10-ONLY.
#
# WHAT THIS DOES (on the GX10)
# ----------------------------
# Builds a CAGRA graph over a 768-dim corpus with NVIDIA cuVS on the GX10 GPU
# (GB10, ARM64 + CUDA, sm_121, 128 GB unified memory), then exports the finished
# graph to **hnswlib on-disk HNSW format** so TriDB's EXISTING CPU iterator (the
# relaxed-monotone Open/Next/Close VBASE scan + NEON L2 kernel) loads and searches
# it UNCHANGED at query time. The GPU is touched ONLY here, at build time; nothing
# CUDA is resident when the engine serves queries.
#
# WHY THIS IS SAFE w.r.t. the golden rules
# ----------------------------------------
#   * TR-1 (no blocking operator): this build is OFFLINE and OUTSIDE the Volcano
#     iterator. An offline graph-construction step has no Open/Next/Close surface
#     and cannot introduce a blocking operator. TR-1 is structurally irrelevant.
#   * Zero serving-path GPU footprint (operator's hard constraint): the produced
#     artifact is a CPU-loadable HNSW index file. This process EXITS before the
#     engine serves. No GPU/CUDA state survives into query time. See
#     docs/gpu_index_build_v0.1.0.md for the full footprint analysis.
#   * Bit-format-identical output: the export targets the SAME hnswlib format the
#     fork's `hnsw` AM already loads (ADR-0004). The toggle changes only WHICH
#     MACHINE built the graph, never runtime behavior.
#
# DEFAULT-OFF TOGGLE (mirrors WITH_SPTAG, ADR-0004)
# -------------------------------------------------
# This script is the `--gpu-build` opt-in. It REFUSES TO RUN off-CUDA (the guard
# below), so the x86 standin and any non-GX10 ARM box keep building HNSW on CPU
# exactly as today (`CREATE INDEX ... USING hnsw`). The companion CMake flag is
# `WITH_CUVS` (default OFF) — see the design note; this script is the runnable
# build-driver half of that toggle.
#
# !! UNBUILT-HERE !!  cuVS/CAGRA requires CUDA + an NVIDIA GPU. This script CANNOT
# be exercised on the x86 standin. The off-CUDA guard makes it a clean no-op there.
# The A/B numbers (recall@10 parity vs a CPU-built index; build wall-clock delta vs
# the 137 s / 489 s CPU baselines in docs/benchmark_neon_sweep_v0.1.0.md) are
# GX10-MEASURED and recorded in docs/gpu_index_build_v0.1.0.md — NOT claimed here.
#
# Usage (on the GX10):
#   scripts/gpu_build_index.sh --vectors data/public/gist-960-euclidean.hdf5 \
#       --out data/index/cagra_hnsw.bin [--m 32 --ef-construction 200 --metric l2]
#
set -euo pipefail

# --- config / args ---------------------------------------------------------
VECTORS=""
OUT=""
M=32
EF_CONSTRUCTION=200
METRIC="l2"
HDF5_DATASET="train"
LIMIT=0

usage() {
  grep '^# ' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vectors)         VECTORS="$2"; shift 2 ;;
    --out)             OUT="$2"; shift 2 ;;
    --m)               M="$2"; shift 2 ;;
    --ef-construction) EF_CONSTRUCTION="$2"; shift 2 ;;
    --metric)          METRIC="$2"; shift 2 ;;
    --hdf5-dataset)    HDF5_DATASET="$2"; shift 2 ;;
    --limit)           LIMIT="$2"; shift 2 ;;
    -h|--help)         usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

# --- NO-CUVS NO-OP GUARD (the default-OFF half of the toggle) ---------------
# The real GX10 dependency is NVIDIA cuVS (built for ARM64 + sm_121), NOT merely
# "some NVIDIA GPU present". cuVS does not install/run on a non-GX10 GPU (e.g. an
# old sm_61 GTX-1070 dev box), so we gate on cuVS being IMPORTABLE — that is the
# precise capability this build needs. On any box without it (the x86 standin,
# non-GX10 ARM) this is a clean no-op: the CPU build path (CREATE INDEX USING
# hnsw) is the supported, bit-identical route there.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE_PY="$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)"
if ! "$PROBE_PY" -c "import cuvs" >/dev/null 2>&1; then
  echo "[gpu_build_index] NVIDIA cuVS not available — GPU index build is GX10-only"
  echo "[gpu_build_index] (cuVS for ARM64 + sm_121; a non-GX10 GPU does not qualify)."
  echo "[gpu_build_index] This is the default-OFF path: build HNSW on CPU instead with"
  echo "[gpu_build_index]   CREATE INDEX ... USING hnsw (...);   (bit-identical output)."
  echo "[gpu_build_index] No-op. See docs/gpu_index_build_v0.1.0.md (WITH_CUVS toggle)."
  exit 0
fi

# --- cuVS-capable path (GX10) -----------------------------------------------
# Reached ONLY where cuVS imports (the GX10). Delegates to the Python builder,
# which owns the cuVS CAGRA build + the CAGRA->HNSW export
# (cuvs.neighbors.cagra.build + the hnsw from_cagra/save path). Python keeps the
# numpy loaders shared with bench/rabitq_sim.py and tools/real_corpus.py.
[[ -n "$VECTORS" ]] || { echo "--vectors is required" >&2; exit 1; }
[[ -n "$OUT" ]]     || { echo "--out is required" >&2; exit 1; }

echo "[gpu_build_index] cuVS available — building CAGRA graph on the GPU (GX10)."
command -v nvidia-smi >/dev/null 2>&1 && \
  nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader || true

exec "$PROBE_PY" "$ROOT/scripts/gpu_build_index.py" \
  --vectors "$VECTORS" \
  --out "$OUT" \
  --m "$M" \
  --ef-construction "$EF_CONSTRUCTION" \
  --metric "$METRIC" \
  --hdf5-dataset "$HDF5_DATASET" \
  --limit "$LIMIT"

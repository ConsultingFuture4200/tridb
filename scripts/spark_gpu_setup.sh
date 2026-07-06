#!/usr/bin/env bash
#
# spark_gpu_setup.sh — repeatable GPU compute-path setup for the DGX Spark (GB10).
#
# WHAT THIS DOES
# --------------
# Installs, into the repo .venv, the wheels that give TriDB a REAL GPU path on the
# Spark for the two operations a fused wiki-scale benchmark needs:
#   (a) GPU embeddings  — torch 2.12 (cu130, aarch64/sbsa) + sentence-transformers
#   (b) GPU ANN index   — NVIDIA cuVS (cuvs-cu13) CAGRA build + search
# then VERIFIES each actually executes on the GPU (not a silent CPU fallback).
#
# TARGET: DGX Spark ONLY — NVIDIA GB10 (Grace ARM64 + Blackwell GPU, sm_121,
# CUDA 13.0 driver, 128 GB coherent unified memory). Run it over `ssh spark`.
# It is a clean no-op / early-exit on any non-CUDA box (the x86 standin).
#
# WHY NOT onnxruntime-gpu (the fastembed CUDAExecutionProvider path)
# -----------------------------------------------------------------
# There is NO aarch64 onnxruntime-gpu wheel on PyPI (`pip index versions
# onnxruntime-gpu` -> "No matching distribution"), so fastembed cannot get a
# CUDAExecutionProvider from PyPI on the Spark. The GPU-embedding requirement is
# instead met by the torch path below (measured 96% GPU util). See
# docs/spark_gpu_path_findings_v0.1.0.md for the exact command + error and the
# NVIDIA-hosted-ORT alternative if the ORT path is ever wanted.
#
# VERIFIED WORKING 2026-07-06 on the Spark — see the findings doc for versions,
# timings, and observed nvidia-smi utilization. This script records the exact
# commands that produced that result; it is hardware-independent to keep in the
# repo but only does real work on the Spark.
#
# Usage (on the Spark):
#   scripts/spark_gpu_setup.sh            # install + verify
#   scripts/spark_gpu_setup.sh --verify   # verify only (skip installs)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
VERIFY_ONLY=0
[[ "${1:-}" == "--verify" ]] && VERIFY_ONLY=1

# --- CUDA guard: real work only on a CUDA box (the Spark) -------------------
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[spark_gpu_setup] no nvidia-smi — this is not the Spark. Nothing to do."
  echo "[spark_gpu_setup] GPU wheels (torch cu130 / cuvs-cu13) install & run only on GB10."
  exit 0
fi

echo "[spark_gpu_setup] GPU:"
nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader || true

[[ -x "$VENV/bin/python" ]] || { echo "[spark_gpu_setup] no .venv at $VENV — create it first" >&2; exit 1; }
# shellcheck disable=SC1091
source "$VENV/bin/activate"
PY="$VENV/bin/python"

if [[ "$VERIFY_ONLY" -eq 0 ]]; then
  echo "[spark_gpu_setup] installing GPU wheels into $VENV ..."
  # (1) cuVS for CUDA 13 (aarch64) — CAGRA build+search on the GPU.
  #     Pulls its own CUDA 13.3 runtime stack (rmm/pylibraft/libcuvs/nvidia-*).
  pip install "cuvs-cu13==26.6.0"

  # (2) torch — the DEFAULT PyPI aarch64 wheel IS the CUDA (cu130) build; it pulls
  #     nvidia-* cu13 libs and, on GB10, reports torch.cuda.is_available()==True.
  #     NOTE: torch pins slightly older nvidia-* libs than cuVS did and pip will
  #     DOWNGRADE cublas/cusolver/cusparse/nccl. This is fine — cuVS CAGRA and
  #     torch were both re-verified working AFTER this downgrade (findings doc).
  #     (Do NOT use the download.pytorch.org cu130 index here: its R2 CDN threw an
  #     SSL handshake failure from the Spark — PyPI is the reliable route.)
  pip install torch

  # (3) sentence-transformers — real embedding models on cuda (all-MiniLM-L6-v2).
  pip install sentence-transformers
fi

echo "[spark_gpu_setup] verifying GPU execution ..."
"$PY" - <<'PYEOF'
import time
import numpy as np

# --- torch CUDA -------------------------------------------------------------
import torch
assert torch.cuda.is_available(), "torch.cuda.is_available() is False"
print("torch", torch.__version__, "cuda", torch.version.cuda,
      "device", torch.cuda.get_device_name(0), "cc", torch.cuda.get_device_capability(0))

# --- GPU embedding (torch / sentence-transformers) --------------------------
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cuda")
sents = [f"verification sentence number {i} about wikipedia and graphs" for i in range(2048)]
_ = m.encode(sents[:64], batch_size=64, convert_to_numpy=True)  # warmup
torch.cuda.synchronize(); t0 = time.time()
emb = m.encode(sents, batch_size=256, convert_to_numpy=True,
               normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
torch.cuda.synchronize()
print(f"GPU embed OK: {emb.shape} in {time.time()-t0:.2f}s "
      f"(peak torch GPU mem {torch.cuda.max_memory_allocated()/1e9:.2f} GB)")

# --- cuVS CAGRA build + search on the GPU -----------------------------------
from cuvs.neighbors import cagra
from pylibraft.common import device_ndarray
idx = cagra.build(cagra.IndexParams(metric="sqeuclidean", graph_degree=32), emb)
q = device_ndarray(emb[:512].copy())
_ = cagra.search(cagra.SearchParams(itopk_size=64), idx, q, 10)  # warmup
t1 = time.time()
D, I = cagra.search(cagra.SearchParams(itopk_size=64), idx, q, 10)
Ih = np.asarray(I.copy_to_host())
self_hit = float(np.mean(Ih[:, 0] == np.arange(512)))
print(f"cuVS CAGRA OK: build+search on {emb.shape[0]} vecs, "
      f"search 512 q k=10 in {(time.time()-t1)*1e3:.2f} ms, self-recall@1={self_hit:.3f}")
print("ALL GPU PATHS VERIFIED")
PYEOF

echo "[spark_gpu_setup] done. Watch 'nvidia-smi -l 1' in another shell to see GPU util."

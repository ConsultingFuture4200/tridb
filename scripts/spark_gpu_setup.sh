#!/usr/bin/env bash
#
# spark_gpu_setup.sh — repeatable, ISOLATED GPU compute-path setup for the DGX
# Spark (GB10). Advisor plan 086: the GPU environment is its own locked venv.
#
# WHAT THIS DOES
# --------------
# Installs, into a DEDICATED venv (${GPU_VENV:-<repo>/.venv-gpu} — NEVER the core
# .venv), the exact locked wheel set that gives TriDB a REAL GPU path on the
# Spark for the two operations a fused wiki-scale benchmark needs:
#   (a) GPU embeddings  — torch 2.12 (cu130, aarch64/sbsa) + sentence-transformers
#   (b) GPU ANN index   — NVIDIA cuVS (cuvs-cu13) CAGRA build + search
# then VERIFIES each actually executes on the GPU (not a silent CPU fallback).
#
# All installs come from requirements-gpu-gb10.lock — the full ARM64/Linux
# transitive closure (with hashes) generated ON the GB10 from the exact-pinned
# direct requirements in requirements-gpu-gb10.in. No floating installs: the
# old sequential `pip install cuvs; pip install torch` dance (which downgraded
# shared nvidia-* libs mid-flight) is replaced by one resolver pass + one lock.
#
# TARGET: DGX Spark ONLY — NVIDIA GB10 (Grace ARM64 + Blackwell GPU, sm_121,
# CUDA 13.0 driver, 128 GB coherent unified memory). Run it over `ssh spark`.
# On any other host (no nvidia-smi, or non-aarch64 — the lock is aarch64-only)
# it prints SKIP and exits 0 without creating or modifying ANY venv.
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
# Usage (on the Spark):
#   scripts/spark_gpu_setup.sh            # create .venv-gpu if absent, install lock, verify
#   scripts/spark_gpu_setup.sh --verify   # verify only (no venv creation, no installs)
#   scripts/spark_gpu_setup.sh --lock     # regenerate requirements-gpu-gb10.lock from the .in
#                                         # (GB10-only; rerun install+verify in a CLEAN venv after)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${GPU_VENV:-$ROOT/.venv-gpu}"
LOCK="$ROOT/requirements-gpu-gb10.lock"
REQ_IN="$ROOT/requirements-gpu-gb10.in"

MODE="install"
case "${1:-}" in
  "")         ;;
  --verify)   MODE="verify" ;;
  --lock)     MODE="lock" ;;
  *) echo "usage: $0 [--verify|--lock]" >&2; exit 2 ;;
esac

# --- GB10 guard: real work only on the Spark ---------------------------------
# The lock names its platform (gb10 = aarch64 + CUDA 13); any other combination
# is unverified and out of scope. SKIP must not create or modify anything.
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "SKIP: GB10/CUDA unavailable (no nvidia-smi) — this is not the Spark."
  echo "      No venv created or modified. GPU wheels install & run only on GB10."
  exit 0
fi
if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "SKIP: GB10/CUDA unavailable ($(uname -m) != aarch64) — the GB10 lock is aarch64-only."
  echo "      No venv created or modified."
  exit 0
fi

echo "[spark_gpu_setup] GPU:"
nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader || true

# --- lock mode: regenerate the full-closure lock from the exact-pinned .in ---
if [[ "$MODE" == "lock" ]]; then
  command -v uv >/dev/null 2>&1 || { echo "[spark_gpu_setup] uv required for --lock" >&2; exit 1; }
  echo "[spark_gpu_setup] compiling $LOCK from $REQ_IN (aarch64/CUDA closure, with hashes) ..."
  UV_CUSTOM_COMPILE_COMMAND="scripts/spark_gpu_setup.sh --lock  # ON the GB10 only" \
    uv pip compile --python-version 3.12 --generate-hashes -o "$LOCK" "$REQ_IN"
  echo "[spark_gpu_setup] wrote $LOCK ($(grep -cE '^[a-zA-Z0-9]' "$LOCK") pinned lines)."
  echo "[spark_gpu_setup] now re-run install+verify in a CLEAN venv before committing it."
  exit 0
fi

# --- install mode: dedicated venv, lock-only install --------------------------
if [[ "$MODE" == "install" ]]; then
  [[ -f "$LOCK" ]] || { echo "[spark_gpu_setup] $LOCK missing — run: $0 --lock" >&2; exit 1; }
  if [[ ! -x "$VENV/bin/python" ]]; then
    echo "[spark_gpu_setup] creating GPU venv at $VENV (python3: $(python3 --version)) ..."
    python3 -m venv "$VENV"
  fi
  echo "[spark_gpu_setup] installing the GB10 lock into $VENV (hash-enforced) ..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --require-hashes --python "$VENV/bin/python" -r "$LOCK"
  else
    "$VENV/bin/python" -m pip install --require-hashes -r "$LOCK"
  fi
  "$VENV/bin/python" -m pip check
fi

[[ -x "$VENV/bin/python" ]] || { echo "[spark_gpu_setup] no GPU venv at $VENV — run $0 (install) first" >&2; exit 1; }
PY="$VENV/bin/python"

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

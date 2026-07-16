# Spark (GB10) GPU compute-path findings — v0.1.0

**Date:** 2026-07-06
**Host:** DGX Spark — NVIDIA GB10 (Grace 20-core ARM64 + Blackwell GPU), 128 GB
coherent unified memory. `ssh spark`.
**Goal:** stand up + VERIFY a real GPU path for (a) embeddings and (b) a cuVS CAGRA
ANN index build/search, so a later fused wiki-scale benchmark can use the GPU.
**Repeatable installer:** `scripts/spark_gpu_setup.sh` (this doc is its evidence).

> These artifacts are hardware-independent to keep in the repo (x86 standin), but
> every number below was **measured on the Spark**, not on the x86 box.

## TL;DR

| Path | Status | How |
|---|---|---|
| **cuVS CAGRA build + search on GPU** | **WORKS** | `cuvs-cu13==26.6.0` (aarch64) |
| **GPU embeddings** | **WORKS** | `torch 2.12.1+cu130` + `sentence-transformers` (measured 96% GPU util) |
| **onnxruntime-gpu (fastembed CUDAExecutionProvider)** | **FAILED — no aarch64 wheel** | not on PyPI; torch path substitutes |

Both GPU operations the fused benchmark needs are verified. The only blocked route
is the *fastembed→ONNX Runtime* GPU provider; the torch embedding path fully
replaces it.

## Environment probed

```
$ nvidia-smi
Driver Version: 580.159.03   CUDA Version: 13.0
GPU 0: NVIDIA GB10   compute_cap 12.1 (sm_121, Blackwell)
$ uname -m            -> aarch64
$ python --version    -> 3.12.3
$ nvcc                -> not present (driver-only; no CUDA toolkit installed)
```

Memory note: the GPU/unified pool was heavily loaded during this work — a resident
`vLLM::EngineCore` held ~85 GB and a CPU link-pred job ~11 GB, leaving ~13 GB
system-available. All GPU probes here were kept bounded (≤20k×384 float32,
≤1.2 GB torch alloc) and coexisted with those jobs without disturbing them.
`nvidia-smi` reports **memory-used as `[N/A]`** on GB10 (unified memory) — GPU
**utilization %** is the reliable liveness signal, and it is what is quoted below.

## (a) GPU embeddings — WORKS (torch, not ONNX Runtime)

`onnxruntime-gpu` has **no aarch64 distribution on PyPI**, so fastembed cannot get a
`CUDAExecutionProvider` this way:

```
$ pip index versions onnxruntime-gpu
ERROR: No matching distribution found for onnxruntime-gpu
$ pip install onnxruntime-gpu
ERROR: Could not find a version that satisfies the requirement onnxruntime-gpu
       (from versions: none)
$ python -c "import onnxruntime as o; print(o.__version__, o.get_available_providers())"
1.27.0 ['AzureExecutionProvider', 'CPUExecutionProvider']   # CPU-only, as installed
```

**Blocker / likely cause:** Microsoft ships `onnxruntime-gpu` wheels only for
x86_64 Linux/Windows. aarch64 CUDA ORT exists only as NVIDIA-hosted builds for
Jetson/JetPack (a different platform than GB10/sbsa) — not on PyPI. Escalation path
if the ORT provider is ever required: NVIDIA's sbsa ORT build or building ORT from
source with `--use_cuda` on the Spark (not attempted; sudo/toolchain-gated).

**Working path — torch + sentence-transformers on cuda:**

```
$ pip install torch                     # DEFAULT PyPI aarch64 wheel == cu130 build
$ pip install sentence-transformers
$ python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
2.12.1+cu130 13.0 True
```

Measured, embedding **20,000 real enwiki articles** (`data/wiki/enwiki/articles-00000.jsonl`,
title + text[:512]) with `all-MiniLM-L6-v2` (384-dim) on cuda, batch 256:

```
model loaded on cuda:0, dim=384
GPU embed: 20000 articles -> (20000, 384) in 15.37s = 1301 art/s
peak torch GPU mem: 1.18 GB
observed nvidia-smi utilization during embed: peak 96%
```

1,301 art/s is a **floor**, not a ceiling — the GPU was contended by the resident
vLLM engine, and this is a single small model with no tuning. Enough to prove the
path; throughput tuning is a later benchmark concern.

## (b) cuVS CAGRA — WORKS

```
$ pip install cuvs-cu13==26.6.0         # pulls rmm/pylibraft/libcuvs + CUDA 13.3 stack
```

Two workloads run, both on the GPU (util observed 40–96%):

**Synthetic 100k × 384 float32 (random gaussian):**
```
device0 NVIDIA GB10 cc 12 1
CAGRA build:  N=100000 D=384 -> 1.79s
CAGRA search (warm): 1000 q, k=10 -> 21.5 ms/batch = 21.5 us/query
self-recall@1 (identity) = 0.71   # low is EXPECTED on random noise (worst case for graph ANN)
```

**Real enwiki embeddings (20k × 384, MiniLM), end-to-end from the embed step above:**
```
CAGRA build over REAL enwiki embeddings: N=20000 d=384 -> 1.52s
CAGRA search: 1000 q, k=10 -> 11.2 us/query
self-recall@1 on real embeddings = 0.999   # index is sane on real data
```

The recall jump from 0.71 (random noise) to 0.999 (real embeddings) is the expected
signature of a correctly-functioning graph index — random high-dim noise has no
neighborhood structure to exploit; real embeddings do.

### API notes (for whoever wires the benchmark)

- cuVS `cagra.build(...)` accepts a **host** numpy float32 array (copies internally).
- cuVS `cagra.search(...)` requires **device** memory for the queries — wrap them:
  `from pylibraft.common import device_ndarray; q = device_ndarray(queries)`.
  Passing host queries raises `RAFT failure ... queries should have device
  compatible memory`.
- First `search` call JIT-compiles kernels for sm_121 (cuVS wheel ships PTX for an
  older arch; the CUDA 13 driver JITs it forward to sm_121) — always **warm up**
  before timing.

## Install-order caveat (verified benign)

Installing `torch` after `cuvs-cu13` **downgrades** shared `nvidia-*` libs
(`cublas 13.6.0.2→13.1.1.3`, `cusolver`, `cusparse`, `nccl 2.30.7→2.29.7`). Both
cuVS CAGRA and torch were **re-verified working after the downgrade** — they
coexist in one venv. `scripts/spark_gpu_setup.sh` installs in this order and
re-runs the full verification at the end.

Also: **do not** use the `download.pytorch.org/whl/cu130` index from the Spark — it
threw `SSL: SSLV3_ALERT_HANDSHAKE_FAILURE` from its R2 CDN host. The default PyPI
`torch` wheel is already the cu130 aarch64 build and installs reliably.

## Verified versions (pip freeze excerpt)

```
torch==2.12.1                (2.12.1+cu130)      triton==3.7.1
cuvs-cu13==26.6.0            pylibraft-cu13==26.6.0   rmm-cu13==26.6.0
cuda-python==13.3.1          cuda-bindings==13.3.1
nvidia-cublas==13.1.1.3      nvidia-cuda-runtime==13.0.96
sentence-transformers==5.6.0 transformers==5.13.0     tokenizers==0.22.2
numpy==2.5.0                 scikit-learn==1.9.0      scipy==1.18.0
onnxruntime==1.27.0          # CPU-only; no GPU wheel for aarch64 (see above)
```

## Reproduce

```bash
ssh spark
cd ~/code/tridb
scripts/spark_gpu_setup.sh            # install + verify (prints "ALL GPU PATHS VERIFIED")
scripts/spark_gpu_setup.sh --verify   # re-verify without reinstalling
# In another shell, watch the GPU:  nvidia-smi -l 1  (utilization %, memory shows N/A on GB10)
```

---

## Addendum A (2026-07-16, advisor plan 086): isolated, locked GPU environment

The floating-install flow above (sequential `pip install` into the core `.venv`)
is retired. The GPU environment is now its own venv with a platform lock:

| Artifact | Role |
|---|---|
| `${GPU_VENV:-.venv-gpu}` | dedicated GPU venv — the core `.venv` is never touched |
| `requirements-gpu-gb10.in` | exact-pinned DIRECT requirements (top-level verified set + direct imports of the GPU tools: pandas/scipy/fastembed/hnswlib) |
| `requirements-gpu-gb10.lock` | full aarch64/Linux transitive closure WITH sha256 hashes, generated ON the GB10 |

### Prerequisites (the only environment this lock is valid for)
- DGX Spark / NVIDIA GB10: `aarch64`, CUDA 13.0 driver (`580.159.03` at lock time),
  compute cap 12.1 (sm_121)
- Python 3.12 (locked/verified with 3.12.3)
- `uv` for lock generation (0.11.25 at lock time); installs use
  `pip/uv pip install --require-hashes`

### Commands
```bash
make gpu-setup    # scripts/spark_gpu_setup.sh — create .venv-gpu if absent,
                  # hash-enforced install of requirements-gpu-gb10.lock, pip check,
                  # then the full GPU verification ("ALL GPU PATHS VERIFIED")
make gpu-verify   # --verify: verification only, no venv creation, no installs
make gpu-lock     # --lock:   regenerate the lock from requirements-gpu-gb10.in
```
All three are a clean `SKIP: GB10/CUDA unavailable` (exit 0, nothing created or
modified) on any host without `nvidia-smi` OR not `aarch64` — the lock carries its
platform in its name; an x86 CUDA box is NOT this target.

### Install-order rationale (supersedes the sequential-pip caveat above)
The old flow installed `cuvs-cu13` then `torch`, letting pip downgrade shared
`nvidia-*` libs mid-flight. The lock replaces order-dependence with ONE resolver
pass over the full closure: the resolver picks a single consistent `nvidia-*` set
and the GB10 verification gate re-proves it. Comment ordering in the `.in` is
documentation only.

### Provenance of the committed lock (generated + verified 2026-07-16)
- Host: DGX Spark, Linux 6.17.0-1021-nvidia aarch64, NVIDIA GB10 cc 12.1,
  driver 580.159.03; Python 3.12.3; uv 0.11.25. 83 pinned packages, all hashed.
- Two CLEAN venvs installed from this lock: `pip check` clean in both,
  `pip freeze` byte-identical across both, and BOTH re-ran the full GPU gate
  (torch CUDA embed + cuVS CAGRA build/search) to `ALL GPU PATHS VERIFIED`
  (`torch 2.12.1+cu130`, GPU embed (2048, 384), CAGRA self-recall@1=1.000).
- Key versions vs the §"Verified versions" set: torch/cuvs/pylibraft/
  sentence-transformers/numpy/scipy/nvidia-cuda-runtime identical. Transitive
  drift accepted after live re-verification: `transformers 5.13.0 → 5.14.1`,
  `nvidia-cublas 13.1.1.3 → 13.1.0.3` (the resolver's single-pass pick).

### Refresh rule
Only a clean GB10 run may refresh this lock: `make gpu-lock`, then install into a
FRESH venv and re-run `make gpu-setup`/`gpu-verify` to the success marker before
committing. Never hand-edit the lock, never generate it off-target, and never
install GPU extras into the core `.venv` (its lock, `requirements.lock`, is a
separate closure — see the `lock` Make target). A future CUDA/architecture target
needs its own `requirements-gpu-<target>.in/.lock` pair, not edits to this one.

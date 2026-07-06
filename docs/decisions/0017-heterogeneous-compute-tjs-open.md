# ADR-0017: CPU/GPU heterogeneous execution for `tjs_open` on the GB10 unified-memory part

- **Status:** Proposed. The GPU building blocks are PROVEN on the Spark (Phase 2, `docs/spark_gpu_path_findings_v0.1.0.md`); the fused in-operator integration is HYPOTHESIS and is GX10/engine-gated C. 2026-07-06.
- **Inputs:** `docs/spark_gpu_path_findings_v0.1.0.md` (measured GPU paths), `scripts/spark_gpu_setup.sh` (repeatable verify), ADR-0012 (`tjs_open` pipeline + FR/PPR/RRF contract), ADR-0007/0006 (TR-1 termination), `docs/gpu_index_build_v0.1.0.md` (PERF-08), `docs/perf_research_v0.1.0.md` (PERF-10 RaBitQ), `docs/wiki_scale_benchmark_spec_v0.1.0.md` (the I/O-bound target).
- **Design detail:** `docs/heterogeneous_compute_design_v0.1.0.md`.

## Context

`tjs_open` (ADR-0012) is the seedless open-domain operator: ANN top-`m_seeds` from the
vector leg → graph-reachable bridges expanded from all seeds → a Fagin/FR-bounded merge
that emits early-terminating top-k. Its pipeline is deliberately **two different kinds of
work welded together**: the vector leg is dense, data-parallel arithmetic (distance
computations over high-dim float vectors); the graph leg is branchy pointer-chasing over
an adjacency list, and the termination brain (FR best-worst bound / `consecutive_drops`,
ADR-0006/0007) is inherently serial control flow. These have opposite hardware affinities.

Until now every leg runs on the CPU. The target hardware — the **GB10 (Grace ARM64 +
Blackwell GPU, 128 GB coherent unified memory)** — has a large idle GPU during a `tjs_open`
query, and, uniquely, a **single coherent address space** shared by CPU and GPU. On a
discrete GPU, a hybrid operator pays a host↔device copy on every frontier round, which
historically erases the benefit of offloading anything with a serial/iterative structure.
The GB10's unified memory removes that copy in principle, which is what makes a fused
CPU-graph / GPU-vector operator worth evaluating at all.

Phase 2 (`docs/spark_gpu_path_findings_v0.1.0.md`) established what the GPU can actually do
on this part **today**, measured on the Spark under a contended GPU:

- **GPU embeddings — WORKS** via `torch 2.12.1+cu130` + sentence-transformers. 20,000 real
  enwiki articles → (20000, 384) in 15.37 s = **1,301 art/s** (a floor under vLLM
  contention), peak 96 % GPU util.
- **cuVS CAGRA ANN — WORKS** via `cuvs-cu13==26.6.0`. Real enwiki 20k × 384: build 1.52 s,
  **11.2 µs/query** warm, self-recall@1 0.999. (PERF-08's build path, validated.)
- **`onnxruntime-gpu` (fastembed's CUDAExecutionProvider) — FAILED**, no aarch64 wheel on
  PyPI. Not blocking: the torch path fully substitutes for embeddings.
- Both GPU paths **coexist in one venv** after the install-order lib downgrade; the
  end-to-end verifier prints `ALL GPU PATHS VERIFIED`.

So the two GPU primitives a heterogeneous `tjs_open` needs — batch distance/ANN and
embedding — are real on the GB10. What is NOT yet real is calling them from **inside a
Postgres access method / custom scan** and fusing them with the CPU graph walk over shared
buffers without violating TR-1.

## Decision

Adopt a **CPU/GPU heterogeneous execution model** for `tjs_open` on the GB10 unified-memory
part, with this fixed work-partition:

| Leg | Runs on | Why |
|---|---|---|
| ANN seed retrieval + candidate batch-distance (the vector ranking stream) | **GPU** (cuVS CAGRA + batch `<->`) | dense data-parallel float arithmetic; proven 11.2 µs/query on the GB10 |
| RaBitQ quantization / in-scan rerank (PERF-10) | **GPU** | vectorized bit ops + full-precision rerank over the frontier |
| Embedding (query + offline corpus) | **GPU** (torch) | proven 1,301 art/s floor |
| Native adjacency traversal (pointer-chasing, PPR forward-push) | **CPU** | branchy, cache/latency-bound, GPU-hostile |
| `tjs_open` early-termination control (FR / `consecutive_drops`) | **CPU** | serial control flow; the TR-1 brain |
| Relational filter | **CPU** | Postgres executor |
| Transaction manager / WAL | **CPU** | Postgres is a CPU process engine; golden rule 2 (never leave the process) |

The GPU is an **arithmetic co-processor for the vector leg only**; the operator's identity —
graph-native traversal + early-terminating merge inside one Postgres transaction — stays on
the CPU. This preserves all four golden rules: the graph stays a native adjacency AM (rule
3, not moved to the GPU as a matrix), we never leave the Postgres process (rule 2), and the
termination logic that makes the operator non-blocking (rule 1 / TR-1) is untouched.

The **unified-memory bet** is the specific thing being adopted: because CPU and GPU share
one coherent address space, the GPU writes candidate distances into the *same buffers* the
CPU's FR merge reads, with no marshaling — a zero-copy fused operator. On a discrete GPU
this partition would not be worth it; on the GB10 it is the hypothesis worth testing.

**TR-1 guard (non-negotiable).** GPU offload MUST NOT become a blocking materialization. The
GPU computes distances over a **bounded, windowed frontier** handed to it by the CPU merge —
never "compute all 7M distances then rank." The CPU still drives Open/Next/Close and stops
the operator on the FR bound (ADR-0012 addendum); the GPU is called per bounded window
inside that loop. A design that batches the whole corpus to the GPU to rank it forfeits
early termination and is rejected exactly as the blocking composition (A) was in ADR-0012.

## Consequences

- **Proven vs unproven is explicit.** The GPU embed path and cuVS CAGRA build/search are
  proven on the GB10 (Phase 2). The *fused, zero-copy, in-access-method* operator is
  unbuilt and its win is a HYPOTHESIS — it must be falsified against CPU-only on the
  I/O-bound 7M wiki workload before any "GPU makes `tjs_open` faster" claim ships. The
  design doc pins that experiment (fixed-accuracy latency, candidates examined, pages
  touched, GPU/CPU util) and its kill criterion.
- **Integration is the hard, unsolved part.** Calling CUDA from inside a PG custom scan
  raises real process-model questions not yet answered: per-backend CUDA context init cost,
  GPU memory living outside `shared_buffers`, and kernel-launch latency versus the *tiny*
  per-query frontier `tjs_open` examines (ADR-0012 measured ≈171 candidates on HotpotQA).
  If launch overhead dominates at that frontier size, the offload loses even with zero copy
  — this is the primary risk and the first thing the experiment must measure.
- **The safe fallback already has value.** PERF-08 (GPU CAGRA *offline index build*) is
  proven and useful regardless of this ADR — it needs no in-operator GPU call. If the fused
  serving-path hypothesis is falsified, the GPU stays an offline-build accelerator and the
  serving operator remains CPU-only and TR-1-clean. This ADR does not bet the roadmap on the
  fused path; it authorizes measuring it.
- **`onnxruntime-gpu` is off the table on this platform.** The embedding leg is torch, not
  fastembed-on-CUDA. Documented so nobody re-litigates the missing wheel.
- Build is GX10/engine-gated C (the CUDA-in-AM glue, like the other `tjs_open` fork work).
  The GPU primitives and the copy-hybrid/CPU-only baselines are buildable/measurable on the
  Spark now.

## Alternatives rejected

- **CPU-only forever (status quo).** Correct and TR-1-clean, but leaves the Blackwell GPU
  idle during every query on the one part where unified memory could make a fused operator
  pay. Kept as the falsification baseline and the fallback, not as the ceiling.
- **Copy-hybrid on a discrete-GPU model** (marshal candidate ids host→device, distances
  device→host each frontier round). This is the design unified memory exists to beat; it is
  kept only as the *middle* arm of the experiment (to isolate how much of any win is the
  zero-copy, versus GPU arithmetic alone). Not adopted as the product.
- **Push the graph leg onto the GPU** (adjacency as a sparse matrix, GPU SpMV traversal).
  Violates golden rule 3 (graph is a native adjacency AM, not linear algebra) and fights the
  hardware — Wikipedia-scale traversal is latency-bound pointer-chasing the GPU is bad at,
  and it would move topology off the Postgres process. Rejected.
- **Full-GPU operator.** Postgres is a CPU process engine with one transaction manager and
  one WAL (golden rule 2); the termination brain is serial control flow. Moving them to the
  GPU is neither possible nor desirable. Rejected.

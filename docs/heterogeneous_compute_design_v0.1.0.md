# Heterogeneous CPU/GPU execution for `tjs_open` on the GB10 — design v0.1.0

**Date:** 2026-07-06
**Decision of record:** ADR-0017.
**Grounding:** every GPU capability cited as PROVEN was measured on the Spark (NVIDIA GB10)
in Phase 2 — `docs/spark_gpu_path_findings_v0.1.0.md`, reproduced by
`scripts/spark_gpu_setup.sh --verify`. Everything cited as HYPOTHESIS is unbuilt and is the
subject of §5.

> TR-1 (golden rule 1) governs this whole document: no leg may materialize a full
> intermediate result. The GPU is called per **bounded window** inside the CPU's
> early-terminating loop, never handed the whole corpus to rank.

## 1. The work-partition on coherent unified memory

The GB10 is a Grace ARM64 CPU + Blackwell GPU behind **128 GB of coherent unified memory** —
one physical address space both processors read and write, cache-coherently, with no
explicit copy. `tjs_open` (ADR-0012) is two workloads with opposite hardware affinities
welded into one operator, so we split it along that seam:

| Work | Processor | Rationale | Phase-2 status |
|---|---|---|---|
| Query + corpus **embedding** | GPU (torch) | dense matmul | **PROVEN** — 1,301 art/s floor, 96% util |
| **OFFLINE index build** (CAGRA graph → hnswlib export, PERF-08) | GPU (cuVS CAGRA), *offline* | data-parallel graph-ANN build, exits before serving | **PROVEN** — build 1.52 s; the 11.2 µs/q / recall@1 0.999 was a *standalone* CAGRA search, not in-AM |
| **ANN seed retrieval** at serve time (top-`m_seeds`) | **CPU** (hnswlib, served from the PERF-08 export) | no GPU call on the query path (PERF-08 §2); the served index is CPU-native | n/a (CPU) |
| **Batch candidate distances** (the vector ranking stream) | GPU (batched `<->`) | one kernel over the frontier | HYPOTHESIS (in-AM) |
| **RaBitQ** 4-bit quant + in-scan rerank (PERF-10) | GPU | vectorized bit ops + FP rerank | HYPOTHESIS (host sim only today) |
| **Native adjacency traversal** / PPR forward-push | **CPU** | branchy, latency-bound pointer-chasing — GPU-hostile | n/a (CPU) |
| **Early-termination control** (FR bound / `consecutive_drops`) | **CPU** | serial control flow; the TR-1 brain | n/a (CPU) |
| **Relational filter** | **CPU** | Postgres executor | n/a (CPU) |
| **Txn manager / WAL** | **CPU** | one process, one WAL (golden rule 2) | n/a (CPU) |

The invariant: **the GPU is an arithmetic co-processor for the vector leg; the operator's
identity stays on the CPU.** Graph topology is never linearized onto the GPU (golden rule 3);
the termination logic that makes the operator non-blocking is never offloaded (rule 1); we
never leave the Postgres process (rule 2).

### Why not the graph leg on the GPU
Wikipedia-scale traversal is latency-bound pointer-chasing over an adjacency list with
skewed degree — the exact workload a GPU is worst at and a large L2/coherent cache is best
at. PPR forward-push (ADR-0012 addendum) is a priority-queue local push: serial, data-
dependent, `O(1/(α·r_max))` work independent of |V|. Moving it to the GPU would forfeit that
bound and drag topology off the Postgres process. It stays on the Grace cores.

## 2. Why unified memory is the whole bet

Map the ADR-0012 pipeline onto the two processors and watch the boundary crossings:

```
  ANN top-m_seeds        graph bridges          FR merge → top-k
  (CPU: HNSW,            (CPU: adjacency PPR)   (CPU control + GPU distances)
   PERF-08 export)
       |                       |                        |
       v                       v                        v
  seed ids  ------------> frontier of candidate ids --> distances(frontier) --> W/B bounds --> stop?
                          (grows each hop)              (GPU batch <->)          (CPU)
```

The load-bearing crossing is **frontier ids → distances**, once per expansion round. The CPU
graph walk produces a bounded set of candidate vertex ids each round; the GPU must score
them against the query so the CPU's FR merge can update its best-worst bounds and decide
whether to stop.

- **Discrete GPU (the industry default):** each round copies candidate ids host→device and
  distances device→host. For `tjs_open`'s *small, iterative* frontier (ADR-0012 measured
  ≈171 candidates examined on HotpotQA, expanded over several hops), the per-round copy +
  launch latency swamps the arithmetic. This is why hybrid vector+graph operators
  historically don't beat CPU-only — the marshaling tax is paid on the critical path, every
  hop, forever.
- **GB10 coherent memory:** the candidate-id buffer and the distance buffer live in one
  address space. The CPU appends ids to the frontier buffer; the GPU reads that buffer and
  writes distances into an adjacent slot buffer; the CPU's FR merge reads those slots
  **directly**, no copy. The GPU can be scoring round *n*'s frontier while the CPU walks
  edges for round *n+1* over the same memory. **Zero-copy fusion is only plausible here** —
  which is exactly why this is a GB10 design and not a portable one.

The concrete fused loop (one Open/Next/Close pass, TR-1-preserving):

```
open:   embed(query) on GPU; ANN → m_seeds on CPU (hnswlib, PERF-08 export); seed the CPU PPR frontier
next (repeat until FR-bound stop OR k settled):
  CPU:  pop max-residue node, push α·residue to reserve, spread to out-neighbors
        (native graph_store_am), appending newly-touched candidate ids to the shared frontier buffer
  GPU:  batch-score the appended window (<-> or RaBitQ+rerank) into the shared distance buffer
  CPU:  update W(d)/B(d) per ADR-0012 §2; emit any candidate whose W settles into top-k;
        STOP when the FR best-worst bound is met (never pull the whole corpus)
close:  release the per-query GPU frontier/distance buffers
```

The GPU touches only the windowed frontier the CPU already bounded. The stop decision is
100% CPU. That is the TR-1 line: **the GPU makes each round's arithmetic cheaper; it does not
get a vote on when to stop, and it never sees more than a bounded window.**

## 3. Where cuVS/CAGRA plugs in, and the integration reality

- **Vector leg = cuVS CAGRA** for seed retrieval, tied to **PERF-08** (`docs/gpu_index_build_v0.1.0.md`,
  plan 008). PERF-08's *offline* build path — `cagra.build` → `from_cagra(hierarchy="cpu")`
  → `hnsw.save` (into an hnswlib on-disk layout) plus the GPU-side recall of that export — is
  **GB10-validated** and needs **no in-operator GPU call**. But the *last* step of that path —
  the fork's `vectordb`/`hnsw` AM actually **loading and serving** the cuVS-26.06 `hnsw.save`
  layout (format-compat + a fork-load recall A/B, PERF-08 §3 and §5) — is **GX10-pending and a
  potential STOP**: whether the fork's (older) hnswlib reads the cuVS-26.06 layout is unresolved.
  So PERF-08 is the **low-risk half** (build + export + GPU-side recall proven), **not yet a
  proven end-to-end CPU serve path**. This design adds the *serving-path* GPU use (batch
  distances over the frontier), which is the separately unproven half.
- **RaBitQ (PERF-10)** is the footprint lever for the 7M/chunk-level regime that exceeds
  128 GB. Today it is a host numpy simulator (`bench/rabitq_sim.py`, real-SIFT: 4-bit +
  in-scan full-precision rerank ≈ recall@10 1.0). Under this model the 4-bit distance +
  full-precision rerank is a natural GPU kernel over the same frontier window. Constraint
  (ADR-0006): the rerank MUST be **in-scan**, never a SQL round-trip — which is precisely
  why it belongs in the fused per-window GPU call, not a second query.
- **Integration reality — calling CUDA from inside a PG access method / custom scan.** This
  is the unsolved engineering core, not a detail:
  - **CUDA context per backend.** Each PG backend is a process; a CUDA context init is
    hundreds of ms. A per-query init is fatal to latency. Needs a persistent context
    (per-backend lazy init amortized over many queries, or a context pool) — unproven under
    the PG process model.
  - **GPU memory vs `shared_buffers`.** The frontier/distance buffers must live where both
    the CUDA kernel and the executor see them. On unified memory the allocation is the
    subtlety: `cudaMallocManaged`-style memory that the PG executor can also address, versus
    pinning executor-owned memory for the GPU. Getting genuine zero-copy (not a hidden
    migration fault) is the make-or-break.
  - **Kernel-launch latency vs frontier size.** At ≈171 candidates/query the GPU may be
    launch-bound, not compute-bound. Batching multiple concurrent queries' frontiers, or
    keeping a persistent kernel, may be required — measured in §5 before any win is claimed.
  - **First-search JIT** (Phase 2 finding): cuVS ships PTX JIT-compiled forward to sm_121 on
    first search. A serving path must warm kernels at backend start, never on the query
    critical path.

## 4. What is PROVEN today vs HYPOTHESIS

**PROVEN on the GB10 (Phase 2, measured, reproducible):**
- GPU embeddings via torch (`onnxruntime-gpu` is dead on aarch64 — no wheel; torch
  substitutes): 20k real enwiki → 384-d in 15.37 s = 1,301 art/s (floor), 96% util.
- cuVS CAGRA build (real enwiki 20k×384: 1.52 s) + warm search (11.2 µs/q, self-recall@1
  0.999). PERF-08 offline build→HNSW-export path validated separately (`docs/gpu_index_build_v0.1.0.md`).
- Both coexist in one venv on the GB10 after the documented lib-downgrade; verifier green.

**HYPOTHESIS (unbuilt — this design's claims to falsify):**
- That a **zero-copy fused** operator (CPU graph + GPU vector over one address space) beats
  CPU-only at fixed accuracy on the 7M wiki workload (dim-384, RAM-resident, compute-bound
  regime — §5; I/O-bound is the expected-fail regime, not the win regime).
- That CUDA can be called from inside a PG custom scan at acceptable per-query cost
  (context, memory, launch) — §3's integration reality.
- That GPU batch-distance over `tjs_open`'s *small* per-round frontier is compute-bound
  enough to matter (not launch-bound).
- That RaBitQ-in-engine holds its host-sim recall (≈1.0 at 4-bit) as a GPU in-scan kernel.

No line of this design authorizes claiming the fused operator is fast. It authorizes building
the three-arm experiment that could show it is — or kill it.

## 5. The falsifiable experiment

**Hypothesis (H1).** On the full-Wikipedia corpus (6,900,039 articles / 224 M edges,
`docs/wiki_scale_benchmark_spec_v0.1.0.md`), a zero-copy fused `tjs_open` (CPU-graph +
GPU-vector, one address space) achieves **lower latency at fixed retrieval accuracy** than
CPU-only `tjs_open`, and the advantage comes from the coherent-memory zero-copy, not GPU
arithmetic alone.

**Regime — where this can win, and where it cannot.** A GPU vector co-processor can only move
end-to-end latency if the vector-distance stream is a large enough fraction of that latency
(the gate-2 "vector-distance fraction ≥ 30%" Amdahl bound, §6 step 2). That is a
**RAM-resident, large-frontier,
vector-compute-bound** regime. The genuinely **I/O-bound** regime is the *opposite*: when
page-fetch dominates latency the vector fraction is small, gate-2 fails, and no zero-copy trick
can help — I/O-bound is exactly where this ADR is **expected to FAIL**, not win (the wiki-spec
"or the speed thesis dies" half). H1 is therefore a compute-bound hypothesis. At this design's
only proven embed + CAGRA parameters (dim-384 float32; §4), the 7M working set is RAM-resident
(≈26 GB of 128 GB — §5), so the benchmark lands in the regime where the win is *possible*; the
open question gate-2 settles is whether the frontier is large enough to make it *real*. The
spec's 44–80 GB I/O-pressure math depends on dim-768 float8, which this design does not adopt or
re-validate (see §5).

**Three arms, same operator semantics, same corpus, same accuracy target:**

| Arm | Vector leg | Graph leg | Frontier crossing |
|---|---|---|---|
| **CPU-only** | CPU `<->` | CPU adjacency | none (baseline + fallback) |
| **copy-hybrid** | GPU batch `<->` | CPU adjacency | explicit host↔device copy per round (discrete-GPU emulation) |
| **zero-copy fused** | GPU batch `<->` | CPU adjacency | shared unified buffers, no copy |

The middle arm isolates the claim: if fused ≈ copy-hybrid, any win is just GPU arithmetic and
unified memory bought nothing; if fused ≫ copy-hybrid, the zero-copy is the lever (the ADR-0017
bet). If CPU-only ≥ fused, the whole serving-path offload is dead and the GPU reverts to
offline index build (PERF-08) only.

**Metrics (all measured client-side end-to-end, warm connections, median over the pinned
150-question HotpotQA-fullwiki set; both baselines the SAME way):**

1. **Latency at FIXED accuracy** — the GTM metric. Fix multi-hop joint evidence recall@k
   (the ADR-0012 quality bar); report ms/query at that recall. A faster wrong answer is
   worthless (`wiki_scale_benchmark_spec` SM metric). **Precondition: measured on a
   dedicated/uncontended GPU** (evict/pause any resident GPU tenant first — see "How to run");
   a contended GPU biases this verdict and is not decision-grade.
2. **Candidates examined** (SM-3) — must be ~equal across arms at fixed accuracy (same
   operator semantics); if the GPU arm examines *more* to hit the same recall, the offload is
   changing the algorithm, not just its speed. Guard, not a win metric.
3. **Pages touched** (SM-3, same-query guard) — the native-graph page-locality signal;
   independent of processor, so it should match CPU-only. Confirms the arms are the same
   query. (Not an "I/O-bound proof": at the dim-384 RAM-resident working set of §5 this
   workload is not I/O-bound; pages-touched here is a cross-arm equality check, not evidence
   of an I/O-pressure regime.)
4. **CPU/GPU overlap** — measured **per round with CUDA events + NVTX**, not nvidia-smi.
   A `tjs_open` query here is single-digit ms (CAGRA search 11.2 µs, ~171-candidate frontier
   over a few hops) and per-round GPU work is sub-ms; nvidia-smi util% is a ~1 Hz coarse
   duty-cycle estimate and **physically cannot resolve sub-ms per-round overlap during a
   query**. Instrument each per-round GPU distance call with `cudaEventRecord` around the
   kernel (`cudaEventElapsedTime` → GPU-time) inside an NVTX range, and stamp CPU wall-clock
   around the FR-merge; the overlap is GPU-time and CPU-time on the *same* round advancing
   concurrently. Reserve **nvidia-smi util%** (GB10 reports `memory.used = [N/A]` on unified
   memory, Phase 2) only for **aggregate liveness over a sustained back-to-back query loop
   (throughput mode)** — a coarse sanity check that the GPU is busy at all, never a
   per-query overlap measurement.
5. **Per-round crossing cost** (fused vs copy-hybrid) — the ONLY clean isolation of the
   zero-copy claim (H1, §5 line 161), so it too is measured with **CUDA events + NVTX**:
   the delta between the fused arm's per-round GPU-time and the copy-hybrid arm's
   (kernel-time + explicit `cudaMemcpy` time) is the zero-copy saving, in µs/round. This is
   sub-millisecond and has no viable nvidia-smi measurement path.

**Kill criterion (pre-registered).** If, at fixed recall@k on the 7M workload (dim-384,
RAM-resident per §5's "How to run"),
zero-copy fused does **not** beat CPU-only by **≥ 20% median latency** (the pre-registered
effect-size budget — well outside run-to-run noise, and it must also beat copy-hybrid to
attribute the win to zero-copy rather than GPU arithmetic alone), H1 is falsified:
the serving-path heterogeneous model is abandoned, the GPU stays an offline-index-build
accelerator (PERF-08), and the serving operator remains CPU-only and TR-1-clean. Publishing
that negative is a valid outcome (it's the honest half of the `wiki_scale_benchmark_spec`
"or the speed thesis dies" framing).

**How to run.** Extend the Plan-015 harness at full-wiki scale (`wiki_scale_benchmark_spec`
Phase 3): drive all three arms through the same `tjs_open(table, k, term_cond, m_seeds, hops,
attr, filter, order)` surface on the Spark, GPU index built via PERF-08 (CAGRA→HNSW), edges
in native `graph_store_am`. The copy-hybrid arm is a build flag on the same C operator that
inserts explicit `cudaMemcpy` at the frontier crossing; CPU-only disables the GPU distance
kernel. Warm CUDA kernels at backend start (§3).

**Benchmark parameters (pinned so the regime is not ambiguous).** The run uses **dim-384
float32** — the only embed + CAGRA path proven in Phase 2 (§4). At 6,900,039 vectors × 384 × 4 B
that is ≈11 GB raw vectors, ≈22 GB with the HNSW graph, ≈4 GB native adjacency ≈ **~26 GB
working set of the 128 GB unified pool → decisively RAM-resident, not I/O-bound**. This places
the benchmark in the compute-bound regime where a GPU vector co-processor *can* help (per the H1
regime note); it is **not** the spec's 44–80 GB I/O-pressure regime, which requires dim-768
float8 and would additionally require re-validating the torch-embed + cuVS CAGRA path at 768
(Phase-2 evidence covers only 384).

**GPU contention (decisive run must be clean).** The top-line kill-criterion measurement
(metric #1, the ≥20% median-latency verdict) MUST run on a **dedicated/uncontended GPU**: evict
or pause the resident vLLM `EngineCore` (and any other GPU tenant) before the timed three-arm
run, and **record GPU-resident state as a precondition, not just an annotation**. A resident LLM
engine contending the Blackwell GPU biases the GPU arms' latency upward toward a false null —
it could falsely kill H1 and makes any null un-attributable ("fused doesn't help" vs "GPU was
contended"). Annotating contention (as Phase 2 did) is not controlling it. The per-round
CUDA-events measurements (metrics #4/#5) are less contention-sensitive, but the 20% verdict is
not decision-grade unless measured clean. Do not launch competing multi-hour CPU jobs on the
Spark while measuring. The contended-GPU path is retained only for metric #4's throughput-mode
liveness sanity check.

## 6. Sequencing

1. **Now (low-risk, no in-AM GPU):** PERF-08 offline CAGRA→HNSW build feeds the 7M vector leg —
   *once the fork-AM load of the cuVS-26.06 export is confirmed*. The GPU build + `hnsw.save`
   export + GPU-side recall are GB10-validated, but the fork's `hnsw` AM loading/serving that
   export (format-compat + recall A/B, PERF-08 §3/§5) is GX10-pending and a potential STOP; if
   it does not load, this step needs a backend-port first. Conditional on that load check, it is
   upside that unblocks the wiki-scale load regardless of the fused hypothesis, and — with the
   single open()-time seed ANN — is the genuinely high-leverage GPU win, needing **neither** the
   fused per-round machinery **nor** zero-copy.
2. **Pre-gate (CPU-only, no GX10 C — do this BEFORE step 3):** the determinant of whether ANY
   serving-path GPU win is even possible is the actual 7M frontier size and the vector-leg
   fraction of end-to-end latency — this is the primary risk (ADR-0017 §Consequences), so
   measure it first and cheaply. On the full 7M corpus, using the wiki-scale harness CPU-only
   (no CUDA-in-AM glue), measure (a) the actual per-query frontier / candidates-examined at
   the fixed-recall target, and (b) the vector-distance fraction of end-to-end latency.
   **Performance budget (pre-registered gate to proceed to step 3):**
   - **Minimum viable frontier** — the per-round GPU batch-distance must be large enough that
     plausible GPU arithmetic is ≥ ~10× the measured kernel-launch overhead. At HotpotQA's
     ≈171 candidates × 384-d that is ~66K MACs/query (microseconds on one Grace core, well
     under a single kernel launch), so a frontier that stays ≈171 on 7M **fails the gate** —
     order-of-thousands candidates/round, or many-query frontier batching, is the floor.
   - **Vector-distance fraction ≥ 30% of end-to-end latency.** Below that, Amdahl caps any
     vector-only speedup under ~1.4× even at infinite GPU speed — no zero-copy trick moves
     metric #1, so building the CUDA-in-AM glue cannot pay.
   If the 7M workload is I/O-bound with a small frontier (the wiki-spec expectation), the gate
   fails here: stop, keep PERF-08-only, and do not build the fused machinery to accelerate the
   lowest-arithmetic part of the pipeline.
3. **Integration spike (§3, GX10-gated) — only if step 2 passes:** stand up a persistent
   per-backend CUDA context + a zero-copy frontier/distance buffer in the fork's custom scan;
   prove a single GPU batch-distance call from inside `execTJS` at acceptable latency. If the
   integration cost is fatal, stop here and keep PERF-08-only.
4. **The experiment (§5):** three-arm run on 7M wiki; report the five metrics; apply the
   kill criterion honestly.

Only after step 4 returns a positive, reproduced result may any material claim GPU
`tjs_open` as faster. Until then: PERF-08 is the shipped GPU value; the fused operator is a
measured hypothesis.

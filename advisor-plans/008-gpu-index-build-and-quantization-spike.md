# Plan 008: Put the idle GX10 GPU to work — offline CAGRA/cuVS index build (default-OFF flag) + RaBitQ quantization spike

> **Executor instructions**: This is a **design + measurement spike** with a GX10-gated build. Do the
> design + host measurements that can be done off-target; clearly mark the GX10-only build/run steps
> and do NOT claim they pass off-target. Follow each step; run every verification command. On a "STOP
> condition", stop and report. Update this plan's row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**:
> `git diff --stat 8b19cb5..HEAD -- scripts/ docs/decisions/0004-decouple-vector-index-sptag-optional.md`
>
> **Hardware gate**: the GPU build runs **only on the GX10** (NVIDIA GB10, ARM64 + CUDA, sm_121,
> 128 GB unified memory). cuVS/CAGRA cannot be built or run on this x86 standin. Design + the
> RaBitQ recall measurements (numpy) run here; the CAGRA build + the engine A/B run on the GX10.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED (new external GPU dependency `cuVS`; mitigated by being **offline-only** and **default-OFF**)
- **Depends on**: none (but read ADR-0004 — the vector-index seam this plugs into)
- **Category**: perf / direction (GX10 leverage + 128 GB launch headline)
- **Planned at**: commit `8b19cb5`, 2026-06-28
- **Horizon**: v2

## Why this matters

The GX10 is **ARM64 + CUDA with 128 GB unified memory**, and TriDB uses the GPU for **nothing** — the
hottest loop (HNSW distance) runs on a hand-written NEON CPU kernel (`tridb_neon_l2_distance.patch`).
The 2026 *"To GPU or Not to GPU"* study benchmarks the GX10's literal sibling (DGX Spark / GB10) and
finds GPU beats CPU on every query/index pair under unified memory **with no PCIe transfer tax**. Two
levers follow, both surfaced by the 2026-06-28 research audit:

1. **Offline GPU index build (CAGRA → HNSW export).** The single safest GPU entry: HNSW/CAGRA
   construction runs ~10× faster on the GPU; NVIDIA cuVS exports the finished graph into **HNSW
   on-disk format**, so TriDB's relaxed-monotone Open/Next/Close iterator and NEON search kernel run
   **unchanged on the CPU at query time**. Because the build is *offline and outside the Volcano
   iterator*, **TR-1 is structurally irrelevant to it** — there is no way for an offline build step to
   introduce a blocking operator. It directly kills the documented HNSW build-time pain (489 s for a
   100k×768 `m=32` index; `benchmark_neon_sweep_v0.1.0.md`).
2. **RaBitQ / Extended RaBitQ quantization.** 8–32× smaller vector footprint at equal recall → a far
   larger corpus fits in the 128 GB unified memory (the launch-headline lever). And its distance
   estimates carry an **unbiased error bound** that can *strengthen* the VBASE early-termination
   guarantee (a principled lower bound on remaining candidates) rather than relying on a heuristic drop
   count — so it composes with plan 007's termination work instead of fighting TR-1.

**Operator's explicit requirements (from the planning discussion, honored as hard constraints):** the
GPU path must (a) hold **zero serving-path footprint** — GPU touched only at build time, nothing
resident at query time; (b) be a **default-OFF build flag** so non-CUDA machines (this x86 box, any
non-GX10 ARM) build HNSW on CPU exactly as today; (c) produce a **bit-format-identical** index either
way, so the toggle only changes *which machine built the graph*, never runtime behavior. The repo
already has this exact pattern: `WITH_SPTAG` (default OFF, ADR-0004) and `__ARM_NEON` gating.

## Current state

- `docs/decisions/0004-decouple-vector-index-sptag-optional.md` — establishes the **vector-index seam**
  (`tridb_vector_index.hpp`) and the `WITH_SPTAG` default-OFF build-flag pattern. Quote (ADR-0004
  decision #5): the seam is *"the documented place a future non-hnswlib, non-SPTAG backend would plug
  in"* and (Consequences) full dependency inversion comes *"if/when a third backend (e.g.
  DiskANN/Vamana) actually lands."* A GPU **builder** is narrower than a new backend — it produces the
  *same* hnswlib-format index — so it plugs in at the build path, not the iterator.
- `scripts/lib/msvbase_patches.sh` — the patch/clone/verify machinery. MSVBASE edits ship as
  `scripts/patches/*.patch`, wired here with sentinel `verify_patches` asserts. The `WITH_SPTAG` gate
  and the NEON kernel both live here (`grep -n WITH_SPTAG scripts/lib/msvbase_patches.sh`).
- `scripts/x86build.sh` / `scripts/gx10build.sh` — the two build entry points; `gx10build.sh` is the
  ARM64+CUDA target. The build is CMake-based (`cmake .. && make` per ADR-0004).
- Index build today: `CREATE INDEX ... USING hnsw (...)` builds via hnswlib on CPU; build times in
  `docs/benchmark_neon_sweep_v0.1.0.md` (137 s / 489 s at 100k×768).
- **Known fork quirk to respect**: scalar `<->` returns 0 outside an index scan; the only authoritative
  distance is the in-scan one (ADR-0006). Any quantized distance must be computed *inside* the scan and
  the full-precision rerank (if any) must stay on the in-scan path — do not reintroduce a SQL re-rank.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Python tests / lint (here) | `make test` / `make lint` | pass, exit 0 |
| RaBitQ recall sim (here) | `python -m bench.rabitq_sim --dataset <npy>` | prints recall@10 vs full-precision per bit-width |
| GX10 engine build | `scripts/gx10build.sh` | builds the fork image |
| GX10 build A/B | `make sweep` (with the corpus envvars) | index build wall-clock + recall curve |

## Scope

**In scope** (create/modify):
- `bench/rabitq_sim.py` (create) — a **pure-numpy** RaBitQ/Extended-RaBitQ recall simulator (runs
  here): quantize the seed/real corpus vectors to 2/4-bit RaBitQ codes, measure recall@10 of
  quantized-distance ranking vs full-precision, and report the empirical distance-error vs the RaBitQ
  theoretical bound. No engine needed — this de-risks the recall claim before any C.
- `tests/test_rabitq_sim.py` (create) — unit tests for the quantizer (reconstruction error, bound).
- `scripts/gpu_build_index.py` or `scripts/gpu_build_index.sh` (create) — the **GX10-only** offline
  CAGRA build: cuVS builds a CAGRA graph, exports to HNSW format, writes an index file the engine
  loads. Guarded so it no-ops with a clear message off-CUDA.
- A **design note** `docs/gpu_index_build_v0.1.0.md` (create) — records the toggle design, the
  serving-path-footprint analysis, the cuVS→HNSW export format compatibility check, and the A/B
  results. This note is the contract for any later production wiring.
- `Makefile` — add `gpu-build-index` (GX10-guarded like `graphrag-live`) and `rabitq-sim` (host) targets.

**Out of scope** (do NOT touch):
- The query-time iterator (`tridb_vector_iter`, `hnswindex*`) — the whole point is the CPU search path
  stays **unchanged**. If making CAGRA's export load requires touching the iterator, STOP (it means the
  format isn't compatible and this becomes a backend port, a different/larger plan).
- The relaxed-monotonicity / termination contract (ADR-0006) — RaBitQ feeds the bound (plan 007), it
  does not replace the iterator's stop logic in this plan.
- `WITH_SPTAG` / SPTAG sources.

## Steps

### Step 1 (here): RaBitQ recall simulator — prove the footprint/recall trade before any C

In `bench/rabitq_sim.py`, implement Extended-RaBitQ quantization in numpy (the rotation + per-vector
scalar + binary code; cite the SIGMOD'24/'25 papers in the docstring). For a real dataset (reuse
`tools/real_corpus.py`'s loaders for `.npy/.fvecs/.hdf5`), measure recall@10 of ranking by the
quantized distance estimate vs full-precision L2, at 1/2/4 bits per dimension, and report the
empirical error against the theoretical RaBitQ bound.

**Verify**: `python -m bench.rabitq_sim --dataset <path>` prints a recall@10 + footprint table; a unit
test asserts the quantizer's reconstruction error stays within the published bound on random vectors.
`make test` green.

### Step 2 (here): Toggle design + serving-path-footprint analysis (the design note)

In `docs/gpu_index_build_v0.1.0.md`, specify:
- the **default-OFF build flag** (`WITH_CUVS` CMake option, or a `--gpu-build` flag on the build
  script) following the `WITH_SPTAG` pattern; CPU build path **bit-unchanged** when off.
- the **serving-path footprint analysis**: confirm (from cuVS docs + the export format) that nothing
  GPU/CUDA is resident at query time — the produced HNSW index is loaded and searched by the existing
  CPU iterator; the GPU build process exits before serving.
- the **format-compatibility check plan**: cuVS `CAGRA → HNSW` export produces an hnswlib-loadable
  index; the design note states exactly which hnswlib version/format the fork uses and how the export
  is validated (Step 3).
- the **toggle-to-hardware** story for the operator's question: GX10 opts in; any non-CUDA box builds
  on CPU; identical output.

**Verify**: the design note exists and a reviewer can answer "does this add GPU headspace at serving
time?" → **no** — from the note alone.

### Step 3 (GX10 only): CAGRA build → HNSW export → recall/build-time A/B

On the GX10: confirm cuVS builds for ARM64 + sm_121. Build a CAGRA graph over the 768-dim seed/real
corpus, export to HNSW format, and load it through the **existing** CPU iterator. Compare against a
CPU-built hnswlib index on the same corpus:
- recall@10 must be **bit-comparable** (within stated tolerance) — same iterator, just a
  GPU-constructed graph;
- index build wall-clock should drop materially (target the ~10× the literature reports vs the 137 s /
  489 s CPU baselines in `benchmark_neon_sweep_v0.1.0.md`).

**Verify** (GX10): `scripts/gpu_build_index.sh` produces an index; the engine recall@10 over it matches
the CPU-built index within tolerance; build wall-clock recorded in the design note. **Mark these
numbers GX10-measured; do not claim them here.**

### Step 4 (here, deferred-flag): note the RaBitQ-in-engine follow-on

Record in the design note that wiring RaBitQ codes alongside HNSW node vectors in the engine (and
feeding the error bound into the plan-007 termination math) is the **next** step after Step 1 proves
the recall, and is a GX10 fork patch under `scripts/patches/` — NOT part of this spike. Keep this plan
to the *measurement + offline build*; the in-engine quantized storage is its own plan once recall is
confirmed.

## Test plan

- `tests/test_rabitq_sim.py` (here): quantizer reconstruction-error bound; recall@10 monotonic in
  bit-width; determinism given a fixed rotation seed. Model after `tests/` bench tests.
- GX10: recall A/B (CAGRA-built vs CPU-built index) as a `make`-driven check on the engine.
- Verification: `make test` + `make lint` green here; the GX10 A/B recorded in the design note.

## Done criteria

ALL must hold:
- [ ] `make test` / `make lint` exit 0; `bench/rabitq_sim.py` + its test exist and pass (here).
- [ ] `python -m bench.rabitq_sim` reports recall@10 vs footprint for 1/2/4-bit RaBitQ on a real dataset.
- [ ] `docs/gpu_index_build_v0.1.0.md` exists and (a) specifies the default-OFF `WITH_CUVS`/`--gpu-build`
      toggle, (b) shows zero serving-path GPU footprint, (c) states the CPU build path is bit-unchanged
      when off.
- [ ] (GX10) a CAGRA-built, HNSW-exported index loads through the **unchanged** CPU iterator and
      matches a CPU-built index's recall@10 within tolerance; build-time delta recorded. (Marked
      GX10-measured.)
- [ ] No query-time iterator code modified (`git diff --stat 8b19cb5..HEAD -- vendor/MSVBASE/src/`
      shows no change to `*hnswindex*` / `tridb_vector_iter*`).
- [ ] `advisor-plans/README.md` status row updated.

## STOP conditions

- cuVS does not build for ARM64 + sm_121 on the GX10, or the CAGRA→HNSW export targets an hnswlib
  format the fork can't load — report; this becomes a backend-port question (larger), not an offline
  builder.
- The CAGRA-built index needs the iterator changed to be searchable — STOP (out of scope; that's a
  DiskANN/Vamana-style backend, ADR-0004's "if/when a third backend lands", not this plan).
- RaBitQ recall@10 at 2–4 bits is materially below full-precision on the real corpus — report the
  honest trade; it caps the footprint lever and changes the launch-headline framing.
- Any step would leave GPU/CUDA state resident at query time — STOP; that violates the operator's
  zero-serving-footprint requirement.

## Maintenance notes

- The toggle must stay **default-OFF** so CI and the x86 standin keep building HNSW on CPU. A reviewer
  should confirm `make test-all` on a non-CUDA box is unaffected (mirrors ADR-0004's `WITH_SPTAG=OFF`
  acceptance).
- Follow-ons explicitly deferred: RaBitQ codes stored in-engine + fed to the termination bound (its own
  GX10 patch, after Step 1); GPU *search* (rejected for the canonical path — GPU batched top-k is
  blocking, see the audit's rejected list; GPU build is the safe lever); DiskANN/Vamana as a full
  backend behind the ADR-0004 seam (separate, larger).
- If a future rebase moves the fork's hnswlib version, the cuVS export-format compatibility (Step 3)
  must be re-validated — note the version pin in the design note.

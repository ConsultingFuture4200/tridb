# GPU offline index build (CAGRA/cuVS → HNSW) + RaBitQ quantization — design note v0.1.0

> **Plan**: advisor-plans/008. **Status of measurements**: the RaBitQ recall/footprint
> simulator (Step 1) runs **here** on this x86 standin (pure numpy); the CAGRA GPU build A/B
> (Step 3) is **GX10-pending** (cuVS needs ARM64 + sm_121 — UNBUILT-HERE). This note is the
> contract any later production wiring must satisfy.

## TL;DR

- The GX10's GPU is idle; TriDB uses it for nothing. The single safest lever is an **offline
  GPU index build**: NVIDIA cuVS builds a CAGRA graph on the GPU (~10× faster than CPU HNSW
  construction under unified memory, no PCIe tax) and **exports it to hnswlib on-disk HNSW
  format**. TriDB's existing CPU iterator (relaxed-monotone Open/Next/Close VBASE scan + NEON
  L2 kernel) loads and searches that file **unchanged**. The GPU is touched **only at build
  time**.
- **Answer to the operator's load-bearing question — "does this add GPU headspace at serving
  time?" → NO.** The produced artifact is a CPU-loadable HNSW file. The build process exits
  before the engine serves; **zero CUDA/GPU state is resident at query time**. (Full analysis
  in §2.)
- The toggle is **default-OFF**, mirroring `WITH_SPTAG` (ADR-0004): a `WITH_CUVS` CMake option
  + the `scripts/gpu_build_index.sh --gpu-build` driver. Off (the x86 standin, any non-GX10
  box) the CPU build path (`CREATE INDEX ... USING hnsw`) runs **bit-unchanged**. The toggle
  changes only **which machine built the graph**, never runtime behavior.
- **RaBitQ** is the separate 128 GB-headline lever (smaller footprint per vector). The Step-1
  numpy simulator (`bench/rabitq_sim.py`) proves the recall/footprint trade before any C. The
  in-engine quantized storage is an explicitly **deferred** follow-on (§5).

---

## 1. The default-OFF toggle (mirrors WITH_SPTAG, ADR-0004)

The repo already has the exact pattern this needs: `WITH_SPTAG` (default OFF) gates an entire
optional backend out of the build so non-target machines build only what they need (ADR-0004),
and `__ARM_NEON` gates the SIMD kernel. The GPU builder follows it.

| Aspect | Design |
|---|---|
| **CMake flag** | `WITH_CUVS`, **default `OFF`**. Guards the cuVS link/include exactly as `WITH_SPTAG` guards SPTAG. The default build links **no** cuVS/CUDA — the x86 standin and any non-GX10 ARM box are unaffected. |
| **Build driver** | `scripts/gpu_build_index.sh` (the `--gpu-build` opt-in) + `scripts/gpu_build_index.py`. The shell script **no-ops with a clear message** unless `import cuvs` succeeds (the precise GX10 capability — see §3 on why we gate on cuVS, not merely "a GPU is present"). |
| **What the flag changes** | **Only which machine builds the graph.** ON (GX10): build CAGRA on the GPU, export to HNSW. OFF (everywhere else): `CREATE INDEX ... USING hnsw` builds via hnswlib on the CPU. **Both produce the same hnswlib on-disk format**; the runtime iterator is identical. |
| **Runtime path** | **Unchanged in both cases.** No iterator code is touched (plan's hard out-of-scope). The query-time path is the existing `hnsw` AM + the relaxed-monotone iterator + NEON kernel. |
| **CI / standin invariant** | `make test-all` on a non-CUDA box is unaffected (it never sets `WITH_CUVS`, and the build driver no-ops). Mirrors ADR-0004's `WITH_SPTAG=OFF` acceptance. |

**Why this is narrower than a new backend (ADR-0004 framing).** ADR-0004 reserves full
dependency inversion for "if/when a third backend (e.g. DiskANN/Vamana) actually lands." A GPU
**builder** is *not* a new backend: it produces the **same** hnswlib-format index the `hnsw` AM
already loads. It plugs in at the **build path**, not the iterator. If making the export load
ever required changing the iterator, that would mean the format is incompatible — and per the
plan that is a **STOP** (it becomes a backend port, a larger/different plan), not this spike.

---

## 2. Serving-path footprint analysis — "does this add GPU headspace at serving time?" → **NO**

This is the operator's hard constraint (a, from the plan): **zero serving-path footprint — the
GPU is touched only at build time, nothing resident at query time.** The design satisfies it
structurally, not by best-effort:

1. **The artifact is CPU-native.** cuVS's `CAGRA → HNSW` export writes an **hnswlib on-disk
   HNSW index file**. That file format is a CPU data structure (adjacency lists + the level
   hierarchy + the stored vectors). It contains **no GPU handles, no device pointers, no CUDA
   context** — nothing that requires a GPU to load or read.
2. **The build process is separate and exits.** `scripts/gpu_build_index.{sh,py}` is an
   **offline batch job**: it allocates the GPU, builds the graph, writes the file, and
   **exits**. The Postgres serving process never imports cuVS, never opens a CUDA context, and
   never links libcuvs (the `WITH_CUVS` default-OFF build does not link it at all).
3. **The serving path is the unchanged CPU iterator.** At query time the engine loads the HNSW
   file through the existing `hnsw` access method and searches it with the relaxed-monotone
   Open/Next/Close iterator + the NEON L2 kernel — **the same code that runs today** over a
   CPU-built index. There is no GPU call on the query path.
4. **No second WAL / no sidecar (golden rule 2).** The build writes a plain index file consumed
   by the existing AM; it is not a separate system, has no transaction surface of its own, and
   does not leave the Postgres process at serving time.

**Conclusion: a reviewer can answer the headspace question from this note alone — NO.** The GPU
adds **build-time** speed; it adds **zero** serving-time resident footprint. (If any future
wiring would leave GPU/CUDA state resident at query time, that is a plan **STOP** condition.)

### TR-1 note (golden rule 1)

The offline build is **outside the Volcano iterator** — it has no Open/Next/Close surface, so it
**cannot** introduce a blocking operator. **TR-1 is structurally irrelevant to the build step.**
TR-1 governs the query-time iterator, which this plan does not touch.

---

## 3. Format-compatibility check plan (cuVS export → the fork's hnswlib)

The export is only safe if the file cuVS writes is one the fork's `hnsw` AM can load.

- **Format pin.** The fork's `hnsw`/`pase_hnsw` AMs are written against **hnswlib** (ADR-0004:
  "written entirely against `hnswlib::HierarchicalNSW / ResultIterator / SpaceInterface`"). The
  host-side bench mirror pins **`hnswlib>=0.8`** (`requirements.txt`). The builder records the
  pin string `hnswlib>=0.8 (PG13.4 fork hnsw AM; ADR-0004)` in its output and in
  `scripts/gpu_build_index.py` (`HNSWLIB_FORMAT_PIN`).
- **Export path.** cuVS provides `cuvs.neighbors.hnsw.from_cagra(index)` + `hnsw.save(...)`,
  which serialize a CAGRA graph into the hnswlib on-disk layout. The builder uses exactly this
  path (the CAGRA graph degree maps to HNSW `M`; `ef_construction` is passed through).
- **Validation (Step 3, GX10).** On the GX10: build CAGRA → export → **load the file through the
  unchanged `hnsw` AM** and run the engine's normal ANN scan. If the AM loads it and returns
  results, the format is compatible. If the AM **cannot** load it (a format/version mismatch),
  that is a plan **STOP**: it means the export targets an hnswlib format the fork can't read,
  making this a backend-port question, not an offline builder.
- **Why gate the driver on `import cuvs`, not on "a GPU exists".** cuVS is built for **ARM64 +
  sm_121** (the GB10). A non-GX10 NVIDIA GPU (e.g. this dev host's **sm_61 GTX-1070** pair) has
  CUDA but **cannot run cuVS** — so a `nvidia-smi`-present check would wrongly try to build on a
  GTX-1070. The driver therefore no-ops unless `import cuvs` actually succeeds, which is the
  precise capability gate. Verified here: on this GTX-1070 box the driver prints the no-op
  message and exits 0.

### Maintenance note (rebase risk)

If a future rebase moves the fork's hnswlib version, the cuVS export-format compatibility above
**must be re-validated** before trusting the GPU-built index. The version pin lives in
`scripts/gpu_build_index.py` (`HNSWLIB_FORMAT_PIN`) and in `requirements.txt`; bump both together
and re-run the Step-3 load check on the GX10.

---

## 4. RaBitQ recall/footprint — Step-1 measurements (host, numpy)

`bench/rabitq_sim.py` implements Extended-RaBitQ quantization in pure numpy (centroid-center →
random orthonormal rotation → symmetric uniform B-bit grid with a per-vector scale → distance
estimate from the reconstructed code), and reports, per bit-width:

- **raw recall@10** — top-k purely by the quantized estimate. This is the *intrinsic code
  resolution* and is the quantity that would feed the plan-007 early-termination bound (the code
  alone must order candidates). Conservative.
- **rerank recall@10** — top-R shortlist by the cheap estimate, then re-scored by exact
  full-precision L2 (the standard RaBitQ ANN deployment; the published numbers are reported this
  way). This is the *end-to-end* ANN recall. **ADR-0006 constraint:** in the fork this rerank
  must stay **inside the index scan** on the authoritative in-scan distance — **never** a SQL
  re-rank. Here it is a host-side recall *simulation*, not the engine path.
- **footprint** (bits/vector = `bits·dim + 32` for the per-vector scale) and the compression
  ratio vs float32.
- **empirical reconstruction error vs the closed-form grid bound** (the host-checkable core of
  the RaBitQ guarantee; the unit test asserts empirical ≤ bound for every bit-width).

### Measured (SYNTHETIC clustered corpus — real-dataset numbers are DATA-GATED)

This standin has **no real `.npy`/`.fvecs`/`.hdf5` dataset checked into the worktree**, so the
table below is on a **synthetic clustered corpus** (`n=5000, dim=128`, 16 clusters, `rerank=200`).
The **shape of the trade is real** (recall and reconstruction error both improve strictly with
bit-width; the bound holds throughout); the **absolute recall numbers depend on the dataset** and
are data-gated until a real corpus is supplied (`make rabitq-sim DATASET=…`).

| bits | raw recall@10 | rerank@10 (R=200) | bits/vec | vs fp32 | emp. recon err | grid bound | within bound |
|---:|---:|---:|---:|---:|---:|---:|:--:|
| 1 | 0.031 | 0.656 | 160 | 25.6× | 72.95 | 128.00 | yes |
| 2 | 0.069 | 0.775 | 288 | 14.2× | 4.73 | 14.22 | yes |
| 4 | 0.400 | 1.000 | 544 | 7.5× | 0.189 | 0.569 | yes |
| 8 | 0.956 | 1.000 | 1056 | 3.9× | 0.0007 | 0.0020 | yes |

**Honest reading (a real STOP-condition check from the plan):** at **2–4 bits the *raw*
estimator recall is low** on this corpus (0.07 / 0.40) — the coarse code alone does not order
near-neighbours well. **With a full-precision rerank shortlist, 4-bit recovers to 1.000** at a
**7.5× footprint reduction**, and 8-bit raw is already 0.956. So the footprint lever is real but
**leans on the rerank** (which, per ADR-0006, must live in-scan, not in SQL): the headline should
be framed as "**N-bit RaBitQ + in-scan full-precision rerank**", not "N-bit alone". The
aggressive 8–32× framing in the plan's *Why* section is a 1-bit-style figure; at the bit-widths
that hold recall here (4-bit + rerank) the realistic compression is **≈7–8×**, still a large
unified-memory win but not 32×. **This must be re-measured on a real embedding corpus before any
launch-headline number is quoted** — that is the data-gated part.

Reproduce: `make rabitq-sim` (synthetic) or `make rabitq-sim DATASET=data/public/sift-128-euclidean.hdf5`.

---

## 5. Step 3 (GX10-pending) and the deferred RaBitQ-in-engine follow-on

### Step 3 — CAGRA build → HNSW export → recall/build-time A/B (GX10 only, UNBUILT-HERE)

On the GX10 (cuVS for ARM64 + sm_121): build a CAGRA graph over the 768-dim corpus, export to
HNSW, load through the **unchanged** CPU iterator, and compare against a CPU-built hnswlib index
on the same corpus:

- **recall@10 must be bit-comparable** within stated tolerance (same iterator, just a
  GPU-constructed graph);
- **build wall-clock** should drop materially vs the documented CPU baselines (**137 s / 489 s**
  at 100k×768, `docs/benchmark_neon_sweep_v0.1.0.md`); the literature reports ~10×.

**These numbers are GX10-MEASURED. They are NOT claimed here** — `scripts/gpu_build_index.{sh,py}`
is authored and the off-cuVS guard is verified on this box, but the build itself is UNBUILT-HERE.
Record the measured recall delta + build-time delta in this section when the GX10 run lands.

### Deferred (its own plan, after Step 1 proves recall)

Wiring RaBitQ **codes stored in-engine** alongside the HNSW node vectors, and feeding the RaBitQ
**error bound into the plan-007 termination math** (a principled lower bound on remaining
candidates that *strengthens* TR-1's early-termination rather than fighting it), is a **GX10 fork
patch under `scripts/patches/`** — **NOT part of this spike**. This plan stays scoped to the
*measurement* (Step 1) + the *offline build* (Step 3). The in-engine quantized storage is its own
plan once the recall is confirmed on a real corpus.

GPU **search** is explicitly **rejected** for the canonical path: GPU batched top-k is a blocking
operator (forfeits TR-1). The **GPU build** is the safe lever; the GPU never touches the query
path.

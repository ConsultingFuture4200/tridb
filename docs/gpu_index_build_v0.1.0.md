# GPU offline index build (CAGRA/cuVS → HNSW) + RaBitQ quantization — design note v0.1.0

> **Plan**: advisor-plans/008. **Status of measurements**: the RaBitQ recall/footprint
> simulator (Step 1) runs **here** on this x86 standin (pure numpy); the CAGRA GPU build + HNSW
> export (Step 3) are **VALIDATED on the GB10** (cuVS 26.06, sm_121, CUDA 13 — see §5); the
> recall A/B vs a CPU-built index is the one remaining Step-3 piece. This note is the contract any
> later production wiring must satisfy.

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
  numpy simulator (`bench/rabitq_sim.py`) proves the recall/footprint trade before any C, now
  measured on **real SIFT-128** (§4): **4-bit RaBitQ + in-scan full-precision rerank reaches
  recall@10 = 1.0 at a 7.5× footprint reduction**; 1-bit is unusable on real data (rerank does
  not rescue it). The realistic recall-preserving compression is **≈7.5×**, not the plan's
  aspirational 8–32×. The in-engine quantized storage is an explicitly **deferred** follow-on (§5).

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
- **Export path.** cuVS provides `cuvs.neighbors.hnsw.from_cagra(IndexParams(), index)` +
  `hnsw.save(path, index)`, which serialize a CAGRA graph into the hnswlib on-disk layout (verified
  on cuVS 26.06 / the GB10, §5). The builder uses exactly this path (the CAGRA graph degree maps to
  HNSW `M`; `ef_construction` is a CPU search-time param applied via the `hnsw` AM reloptions).
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

### Measured — REAL SIFT-128 (ann-benchmarks `sift-128-euclidean.hdf5`)

These are the **real-dataset** numbers, measured on this x86 standin against the public
**SIFT-128-euclidean** corpus (`data/public/sift-128-euclidean.hdf5`, 1M × 128 float32). They
**replace** the earlier synthetic placeholder. Two independent runs agree:

**Run A — held-out queries** (corpus = first 49,800 `train` rows; 200 held-out `train` rows as
queries; truth = exact L2 top-10 over the corpus; `rerank` shortlist R=100; `seed=42`). This is
what `make rabitq-sim DATASET=…` reproduces:

| bits | raw recall@10 | rerank@10 (R=100) | bits/vec | vs fp32 | emp. recon err | grid bound | within bound |
|---:|---:|---:|---:|---:|---:|---:|:--:|
| 1 | 0.018 | 0.072 | 160 | 25.6× | 71.43 | 128.00 | yes |
| 2 | 0.344 | 0.774 | 288 | 14.2× | 4.75 | 14.22 | yes |
| 4 | 0.860 | 1.000 | 544 | 7.5× | 0.188 | 0.569 | yes |
| 8 | 0.991 | 1.000 | 1056 | 3.9× | 0.0007 | 0.0020 | yes |

**Run B — canonical SIFT test set** (corpus = first 50,000 `train` rows; queries = first 100
**real `test`** rows; truth = exact L2 top-10 over the corpus; shows the rerank-shortlist
sensitivity at R=100 and R=500):

| bits | raw recall@10 | rerank@10 (R=100) | rerank@10 (R=500) | vs fp32 |
|---:|---:|---:|---:|---:|
| 1 | 0.030 | 0.106 | 0.233 | 25.6× |
| 2 | 0.418 | 0.830 | 0.946 | 14.2× |
| 4 | 0.877 | 1.000 | 1.000 | 7.5× |
| 8 | 0.990 | 1.000 | 1.000 | 3.9× |

**Honest reading (the plan's RaBitQ STOP-condition check, now on real data):**

- **1-bit is unusable on real SIFT.** raw recall ≈ 0.02–0.03 and — unlike the synthetic corpus,
  where a rerank pulled 1-bit up to ~0.66 — on real SIFT the rerank **does not rescue it**
  (0.07 at R=100, still only 0.23 at R=500). The 1-bit code is too coarse to bring the true
  neighbours into any practical shortlist. So the aspirational 25× (1-bit) footprint figure in
  the plan's *Why* section **does not hold at recall** on a real corpus. This is a change from
  the synthetic finding, where 1-bit + rerank looked viable; it was an artifact of the easy
  synthetic clustering.
- **4-bit is the recall-preserving sweet spot.** raw recall ≈ 0.86–0.88, and **with an
  in-scan full-precision rerank it reaches 1.000 at a 7.5× footprint reduction** (even at the
  small R=100 shortlist). 2-bit + rerank lands at ~0.77–0.83 (R=100) / 0.95 (R=500) — a
  candidate if 2× more compression is worth a recall give-up, but 4-bit is the safe headline.
- **8-bit raw is already 0.99** (no rerank needed) at 3.9×.

**Net: the realistic recall-preserving compression on real SIFT is ≈7.5× (4-bit + in-scan
rerank), not the 8–32× the plan's *Why* section quotes.** The footprint lever is real and still
a large unified-memory win, but the launch-headline number must be **"4-bit RaBitQ + in-scan
full-precision rerank ⇒ recall@10 = 1.0 at 7.5×"**, not a bare 1-bit/raw-estimator figure. Per
ADR-0006 that rerank must live **inside the index scan** on the authoritative in-scan distance,
never as a SQL re-rank.

Reproduce: `make rabitq-sim DATASET=data/public/sift-128-euclidean.hdf5` (Run A; the held-out
default). Full command for Run A: `python -m bench.rabitq_sim --dataset
data/public/sift-128-euclidean.hdf5 --limit 50000 --queries 200 --k 10 --bits 1 2 4 8
--rerank 100`. (h5py is an optional dep — `pip install h5py` for the .hdf5 loader.)

---

## 5. Step 3 (GX10-pending) and the deferred RaBitQ-in-engine follow-on

### Step 3 — CAGRA build → HNSW export → recall/build-time A/B (build+export VALIDATED on GB10; recall A/B remaining)

On the GX10 (cuVS for ARM64 + sm_121): build a CAGRA graph over the 768-dim corpus, export to
HNSW, load through the **unchanged** CPU iterator, and compare against a CPU-built hnswlib index
on the same corpus:

- **recall@10 must be bit-comparable** within stated tolerance (same iterator, just a
  GPU-constructed graph);
- **build wall-clock** should drop materially vs the documented CPU baselines (**137 s / 489 s**
  at 100k×768, `docs/benchmark_neon_sweep_v0.1.0.md`); the literature reports ~10×.

> **CAGRA build + HNSW export are VALIDATED on the GB10 (2026-06-29) — the blocker is gone.**
> cuVS **26.06.00** was installed on the GX10 (DGX Spark, ssh `spark`; aarch64 + CUDA **13.0** +
> compute capability **12.1 / sm_121**) into an isolated user-space `~/cuvs-env` (uv venv, no sudo,
> no system change). Verified live on the GB10:
> - `cuvs.neighbors.cagra.build(IndexParams(graph_degree=32, intermediate_graph_degree=64), X)`
>   builds a CAGRA graph over a **20 000 × 128** corpus in **~1.96 s**;
> - `cuvs.neighbors.hnsw.from_cagra(hnsw.IndexParams(), index)` + `hnsw.save(path, index)` exports it
>   to an hnswlib on-disk file (**13.2 MB** for that corpus).
>
> `scripts/gpu_build_index.py` was **reconciled to this verified cuVS-26.06 API**: the prior
> `from_cagra(index)` / `save(..., ef_construction=)` form was wrong — `from_cagra` requires the
> `IndexParams` object as its first arg. The earlier "GENUINELY GATED" note (cuVS absent) is
> superseded — the user installed cuVS and the build/export path is proven on the real target.
>
> **Remaining — the recall A/B (now a staging task, not a provisioning blocker):** load the
> cuVS-exported HNSW file through the fork's `hnsw` AM and compare recall@10 + build wall-clock vs a
> CPU-built hnswlib index on the same 768-dim corpus (the A/B specified above, against the
> 137 s / 489 s CPU baselines). This needs (a) the repo + a 768-dim corpus staged on the Spark and
> (b) the **format-compat check (§3)** — confirming the fork's hnswlib version loads the cuVS-26.06
> export layout. The build/export blocker is resolved; the A/B is the outstanding piece.
>
> (For reference, the x86 dev host ships `nvcc 12.0` but its GPUs are sm_61 GTX-1070s that cuVS does
> not target — the build only runs on the GB10. The driver still gates on `import cuvs`, which now
> succeeds on the Spark and no-ops everywhere else.)

#### Recall A/B — measured on the GB10 (2026-06-29) + a latent-bug fix

The recall half of the A/B was run on the GB10 (cuVS 26.06) on a **clustered synthetic 100k × 128**
corpus (500 clusters + unit noise — ANN-friendly structure; a pure-Gaussian corpus is a curse-of-
dimensionality worst case and gave a meaningless ~0.2 for every method), exact-L2 oracle, search
`ef=200`, `k=10`:

| Build path | recall@10 | build time | note |
|---|---|---|---|
| `from_cagra(hierarchy="none")` | **0.06** | CAGRA ~1.3 s | flat graph — no HNSW navigation |
| `from_cagra(hierarchy="gpu")` ← **cuVS default** | **0.17** | CAGRA ~1.3 s | the default is recall-broken |
| `from_cagra(hierarchy="cpu")` | **0.83** | CAGRA ~1.3 s + CPU hierarchy | the usable export |
| `hnsw.build(ace_params=AceParams())` (cuVS ACE) | **0.9998** | ~1.5 s | GPU-accelerated; recall-optimal |

**Two findings, both load-bearing:**
1. **Latent bug fixed.** `scripts/gpu_build_index.py` called `from_cagra(IndexParams())`, i.e. the
   cuVS **default `hierarchy="gpu"`** — a near-useless index (recall ~0.17). Fixed to
   `hierarchy="cpu"` (recall ~0.83). This bug was invisible until run on the real GB10.
2. **ACE is the recall-optimal GPU build** (~0.9998) and is the better path than a raw CAGRA export
   if the goal is a high-recall index; both `from_cagra` and ACE produce an `hnsw.save`-able file.

**Caveats (honest):** these are clustered-synthetic absolute numbers — the `from_cagra(cpu)` 0.83 vs
ACE 0.9998 gap and the exact recall should be re-quantified on **real SIFT/BGE-768** (a corpus staged
on the Spark) before any launch claim; and the **format-compat check (§3)** — whether the fork's
(older) hnswlib actually loads the cuVS-26.06 `hnsw.save` layout, and whether its CPU search navigates
the exported hierarchy — is the remaining **engine-integration** step (needs the `tridb/msvbase:gx10`
image + a load path). Build + export + GPU-side recall are now measured; the fork-load A/B is the last piece.

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

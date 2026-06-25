# ADR-0004: Decouple the vector-index abstraction — hnswlib first-class, SPTAG/SPANN build-optional

**Status:** Accepted (2026-06-25)
**Issue:** DEV-1228
**Related:** DEV-1160 (GX10 ARM64 build — this unblocks it), DEV-1168 (HNSW relaxed-monotonicity iterator), ADR-0001 (architecture overview)
**Scope decision:** build-flag decoupling + a thin TriDB-owned interface seam (not a full dependency-inversion rewrite of the working HNSW path)

## Context

The GX10 (ARM64 + CUDA) build of the MSVBASE fork reached the **final** step — the `vectordb`
extension compile — and stalled there (DEV-1160, comment 2026-06-25). The blocker is SPTAG:
its core is x86-assuming (CMake SIMD flags, `cpuid.h`/`mm_malloc.h`, `__m128/256/512` bodies,
`_mm_prefetch`, then `-march` hitting `eor3`/SHA3). Seven distinct x86 layers were patched and
more remained. We were porting a library the v1 vector index (HNSW) does not need.

A precise read of `vendor/MSVBASE/src` on the x86 standin (the coupling map below) shows the
issue's framing — "the HNSW path goes through SPTAG types" — is **empirically false**, and the
truth makes the fix smaller than a dependency inversion:

- The extension registers **four** index access methods in one `vectordb.so`:
  `sptag` and `spann` (SPTAG-backed), `hnsw` and `pase_hnsw` (hnswlib-backed).
- The **`hnsw`/`pase_hnsw` path already has zero real SPTAG dependency** — it is written
  entirely against `hnswlib::HierarchicalNSW / ResultIterator / SpaceInterface`. (The two
  `SPTAG::` tokens in those files are stale doc-comments.)
- The SPTAG coupling is confined to the `sptag` AM (`index*.cpp`, `index_builder*`,
  `index_scan*`) and the `spann` AM (`spannindex*`), plus three **build/link** seams.
- Every test (`test/smoke.sql`, `test/trimodal_*.sql`) uses `USING hnsw`. **Nothing uses
  `sptag`/`spann`.**

So SPTAG is load-bearing only because CMake **compiles and links it unconditionally** into the
single shared library, not because the executor's vector path needs it.

### Coupling map (evidence)

| Seam | Where | Nature |
|---|---|---|
| SPTAG build + link | `thirdparty/CMakeLists.txt`: `add_subdirectory(SPTAG)`, `SPTAGLibStatic`, `AnnService` include | **Compiles all of SPTAG** — the actual ARM blocker |
| SPTAG sources in lib | `CMakeLists.txt` `add_library(vectordb SHARED ...)` lists `index*.cpp`, `spannindex*.cpp` | One compile-unit set drags SPTAG headers in |
| Registration | `lib.cpp` `#include "index.hpp"` + `sptag_para_relopt_kind`; `sql/vectordb.sql` `CREATE ACCESS METHOD sptag/spann` + opclasses | `CREATE EXTENSION` fails if `sptag_handler`/`spann_handler` symbols are absent |
| `sptag` AM | `index.{hpp,cpp}`, `index_builder.{hpp,cpp}`, `index_scan.{hpp,cpp}` | `VectorIndex/VectorSet/MetadataSet/ResultIterator/BasicResult/ByteArray` |
| `spann` AM | `spannindex.{hpp,cpp}`, `spannindex_scan.{hpp,cpp}` | `SPANN::Index/SPANNResultIterator/VectorIndex` |
| HNSW AMs | `hnswindex*`, `pase_hnswindex*`, `operator.cpp`, `topk.cpp`, `multicol_topk.cpp`, `util.cpp`, `model_mng.cpp` | **No real SPTAG.** (`IndexScan` in `topk` = Postgres's `T_IndexScan` plan node, not SPTAG.) |

Note on prior state: `scripts/x86build.sh` exports `MSVBASE_DISABLE_SPTAG=1`, but **nothing
consumes it** (no `src/Makefile`; the build is CMake-only, and the validated `--docker` path
runs plain `cmake .. && make`). SPTAG is fully built/linked in today's x86 image. DEV-1228 is
genuinely unstarted; this ADR replaces that dead placeholder with a real switch.

## Decision

Introduce a **`WITH_SPTAG` CMake option, default `OFF`**, that gates SPTAG entirely, and make
**hnswlib the only backend in the default build**. SPTAG/SPANN remain buildable with
`-DWITH_SPTAG=ON`. Concretely:

1. **`thirdparty/CMakeLists.txt`** — guard `add_subdirectory(SPTAG)`, the `AnnService` include,
   and the `SPTAGLibStatic` link behind `if(WITH_SPTAG)`. hnswlib stays unconditional. This is
   the change that removes the ARM blocker: with the flag off, SPTAG is never compiled.
2. **`CMakeLists.txt`** — list the SPTAG-backed sources (`index*.cpp`, `spannindex*.cpp`) only
   when `WITH_SPTAG`; pass `-DWITH_SPTAG` to the compiler so source guards see it.
3. **`lib.cpp`** — `#ifdef WITH_SPTAG` around the `#include "index.hpp"` and the
   `sptag_para_relopt_kind` registration so the default TU pulls no SPTAG headers.
4. **`sql/vectordb.sql`** — split the `sptag`/`spann` AM + opclass DDL into a fragment emitted
   only when `WITH_SPTAG`. The `hnsw` opclass is already `DEFAULT FOR TYPE float8[]` *within
   the hnsw AM*, so removing the `sptag` default opclass does not orphan the default — each AM
   keeps its own default opclass.
5. **TriDB-owned interface seam** — add `tridb_vector_index.hpp` documenting the minimal
   contract the executor actually needs (open / next / close iterator + a distance method +
   build/insert) that the `hnsw` AM already honors. This is a **seam, not a rewrite**: it names
   the contract and lets the HNSW glue assert conformance, without wrapping the validated
   `hnswlib::` types behind new indirection. It is the documented place a future non-hnswlib,
   non-SPTAG backend would plug in.

All MSVBASE edits ship as **TriDB fork patches** under `scripts/patches/` applied by
`scripts/lib/msvbase_patches.sh` (sentinel-guarded, idempotent, verified), because
`vendor/MSVBASE/` is gitignored and re-cloned — exactly like `l2_distance_scalar.patch`.

### Why not full dependency inversion

The issue's *Design* section sketches a `tridb::VectorIndex` abstract interface with SPTAG and
hnswlib as peer implementers and *no third-party types in the core*. Rejected for v1:

- The portability blocker is **100% SPTAG**. hnswlib already ships the portable scalar fallback
  (DEV-1160 finding #3: "HNSW builds on ARM64"). Wrapping `hnswlib::` types behind a new
  abstract base removes **no** blocker.
- It would rewrite the **validated, green** HNSW path (smoke + the DEV-1168 relaxed-monotonicity
  iterator) for an abstraction with one real implementer — violating "smallest correct change."
- All four DEV-1228 acceptance criteria are met by the build-flag decoupling. The interface
  seam (decision #5) captures the architectural intent without the rewrite cost; full inversion
  can come if/when a third backend (e.g. DiskANN/Vamana) actually lands.

## Consequences

**Positive**
- The default build compiles/links **no SPTAG** → the GX10 build then needs only an hnswlib
  port (already portable), not the multi-layer SPTAG SIMD grind. DEV-1160 unblocked.
- Leaner default image; faster extension compile; smaller attack/maintenance surface.
- `make test-all` is unaffected — every suite uses `hnsw`.

**Negative / risks**
- `sptag`/`spann` become opt-in; anyone wanting SPANN must build `-DWITH_SPTAG=ON`. Acceptable:
  spec §2 pins HNSW as the only v1 index; the BM25/SPANN seam is "architected but closed for
  v1" (CLAUDE.md golden rule 5).
- The `spann.patch` and `patch_modern_gcc_includes` SPTAG machinery stay in the patch layer
  (SPTAG source is still checked out, just not built) so the `-DWITH_SPTAG=ON` path keeps
  working. They are no-ops for the default build.
- A future `gx10build.sh` run should pass `-DWITH_SPTAG=OFF` explicitly (it is also the default)
  and may skip the SPTAG submodule init to save clone time — tracked as a GX10 follow-up, not
  part of this x86 work.

## Migration plan (bounded increments, each rebuilt + smoke-tested, Linus-reviewed before merge)

Branch `dustin/dev-1228`. After each increment: `scripts/x86build.sh --docker` +
`scripts/smoke_test.sh`.

- **A — CMake gating.** `WITH_SPTAG` option (default OFF) in `CMakeLists.txt` +
  `thirdparty/CMakeLists.txt`. Default build no longer compiles/links SPTAG.
- **B — registration + SQL.** `#ifdef WITH_SPTAG` in `lib.cpp`; conditional `sptag`/`spann`
  DDL fragment in the installed SQL. `CREATE EXTENSION` green on the hnsw default opclass.
- **C — interface seam.** Add `tridb_vector_index.hpp`; HNSW glue asserts conformance.
- **D — opt-in proof.** Build `-DWITH_SPTAG=ON` on x86 (SPTAG's native target) to prove the
  SPANN path still compiles/links; confirm default-OFF `make test-all` green.

Acceptance (DEV-1228): HNSW path zero SPTAG; ARM build needs only an hnswlib port; SPANN
buildable behind an opt-in flag; `make test-all` green.

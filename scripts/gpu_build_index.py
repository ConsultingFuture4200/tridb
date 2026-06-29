"""Offline GPU (CAGRA/cuVS) index builder for TriDB — GX10-ONLY (Plan 008, Step 3).

!! UNBUILT-HERE !!  This module requires NVIDIA cuVS + CUDA and runs ONLY on the
GX10 (GB10, ARM64 + CUDA, sm_121). It CANNOT be exercised on the x86 standin. It
is the on-CUDA half of `scripts/gpu_build_index.sh`; the shell wrapper's off-CUDA
guard makes the whole thing a clean no-op on non-CUDA boxes. cuVS is imported
LAZILY inside :func:`build_cagra_export_hnsw` so this file imports (and lints)
cleanly without cuVS installed — but calling the builder off-CUDA raises a clear,
actionable error rather than pretending to work.

WHAT IT DOES (on the GX10)
--------------------------
1. Loads a 768-dim corpus via the SHARED numpy loaders (tools/real_corpus.py), so
   the GPU build consumes the SAME .npy/.fvecs/.hdf5 inputs as the CPU benchmark.
2. Builds a CAGRA graph on the GPU with cuVS
   (`cuvs.neighbors.cagra.build`) — the ~10x-faster-than-CPU construction the
   2026 "To GPU or Not to GPU" study reports under unified memory (no PCIe tax).
3. Exports the finished graph to hnswlib on-disk HNSW format
   (`cuvs.neighbors.cagra.save` with the hnswlib-compatible serializer / the
   CAGRA->HNSW export path), writing a file the fork's EXISTING `hnsw` access
   method loads UNCHANGED (ADR-0004). The CPU iterator + NEON kernel search it at
   query time exactly as if hnswlib had built it on CPU.

ZERO SERVING-PATH GPU FOOTPRINT (operator's hard constraint)
------------------------------------------------------------
The GPU is touched ONLY in this process. The output is a CPU-loadable HNSW file.
This process exits before the engine serves; no CUDA/GPU state survives into query
time. See docs/gpu_index_build_v0.1.0.md for the full analysis + the format pin.

WHY NO TR-1 CONCERN
-------------------
This is an OFFLINE build, OUTSIDE the Volcano iterator — there is no
Open/Next/Close surface here, so it cannot introduce a blocking operator. TR-1
governs the query-time iterator, which this step does not touch.

The recall A/B (CAGRA-built vs CPU-built index, recall@10 within tolerance through
the SAME iterator) and the build-time delta are GX10-MEASURED and recorded in the
design note — NOT claimed off-target.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.real_corpus import load_vectors  # noqa: E402

# The hnswlib on-disk format the fork's `hnsw` AM loads. Pinned here so a future
# rebase that moves the fork's hnswlib version forces a re-validation of the cuVS
# export compatibility (Step 3 / the design note's maintenance note).
HNSWLIB_FORMAT_PIN = "hnswlib>=0.8 (PG13.4 fork `hnsw` AM; ADR-0004)"


def build_cagra_export_hnsw(
    vectors: np.ndarray,
    out_path: Path,
    *,
    m: int = 32,
    ef_construction: int = 200,
    metric: str = "l2",
) -> dict:
    """Build a CAGRA graph on the GPU and export it to hnswlib HNSW format.

    GX10-ONLY. cuVS is imported lazily here so this module imports without CUDA;
    OFF-CUDA this raises a clear RuntimeError (the shell wrapper guards before we
    ever get here on a non-CUDA box). Returns a small stats dict for the design
    note (build wall-clock, params, output path, format pin).
    """
    try:
        # Lazy, GX10-only imports. Absent off-CUDA — intentional.
        from cuvs.neighbors import cagra, hnsw  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - GX10-only path
        raise RuntimeError(
            "cuVS is not installed — the GPU index build is GX10-ONLY (NVIDIA "
            "cuVS + CUDA, sm_121). On a non-CUDA box, build HNSW on CPU instead "
            "(CREATE INDEX ... USING hnsw), which is bit-identical. "
            f"Target format: {HNSWLIB_FORMAT_PIN}."
        ) from exc

    data = np.ascontiguousarray(vectors, dtype=np.float32)
    n, dim = data.shape

    # CAGRA build params. intermediate_graph_degree / graph_degree map to the
    # HNSW M on export; ef_construction governs the search-list during build.
    build_params = cagra.IndexParams(  # pragma: no cover - GX10-only path
        metric=metric,
        graph_degree=m,
        intermediate_graph_degree=max(m * 2, m + 1),
    )
    t0 = time.perf_counter()
    index = cagra.build(build_params, data)  # pragma: no cover - GX10-only path
    build_s = time.perf_counter() - t0

    # Export CAGRA -> hnswlib on-disk format. The fork's `hnsw` AM loads this file
    # UNCHANGED; the CPU iterator searches it identically to a CPU-built index.
    # API reconciled to cuVS 26.06 and VERIFIED on the GB10 (sm_121, CUDA 13,
    # 2026-06-29): from_cagra(IndexParams, index) -> save(path, index). The earlier
    # from_cagra(index)/save(..., ef_construction=) form was wrong (from_cagra needs
    # the params object). ef_construction is a CPU search-list param applied at query
    # time via the `hnsw` AM reloptions (plan 006/DEV-1286), not at export; it is
    # carried in the stats dict as build metadata only.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # hierarchy="cpu" is REQUIRED (measured on the GB10, cuVS 26.06, 2026-06-29): the export
    # builds the navigable HNSW upper layers from the CAGRA base graph. The cuVS DEFAULT is
    # hierarchy="gpu", which on a clustered 100k×128 corpus searched at ef=200 gives recall@10
    # ~0.17 (and "none" ~0.06) — a near-useless index — vs ~0.83 with hierarchy="cpu". For the
    # highest recall, cuVS's GPU-accelerated ACE build (`hnsw.build(IndexParams(ace_params=
    # AceParams()), X)`) reached ~0.9998 on the same corpus; it is the recall-optimal alternative
    # to a raw CAGRA export. See docs/gpu_index_build_v0.1.0.md §5 for the A/B.
    hnsw_index = hnsw.from_cagra(  # pragma: no cover - GX10-only path
        hnsw.IndexParams(hierarchy="cpu"), index
    )
    hnsw.save(str(out_path), hnsw_index)  # pragma: no cover - GX10-only path

    return {
        "n": int(n),
        "dim": int(dim),
        "m": m,
        "ef_construction": ef_construction,
        "metric": metric,
        "build_seconds": build_s,
        "out": str(out_path),
        "hnswlib_format_pin": HNSWLIB_FORMAT_PIN,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--vectors", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--m", type=int, default=32)
    p.add_argument("--ef-construction", type=int, default=200)
    p.add_argument("--metric", default="l2")
    p.add_argument("--hdf5-dataset", default="train")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args(argv)

    vectors = load_vectors(
        args.vectors, hdf5_dataset=args.hdf5_dataset, limit=args.limit or None
    )
    stats = build_cagra_export_hnsw(
        vectors,
        args.out,
        m=args.m,
        ef_construction=args.ef_construction,
        metric=args.metric,
    )
    print(
        f"[gpu_build_index] CAGRA build: {stats['n']} x {stats['dim']} "
        f"m={stats['m']} ef_c={stats['ef_construction']} -> {stats['out']} "
        f"in {stats['build_seconds']:.1f}s (GX10-measured)"
    )
    print(f"[gpu_build_index] hnswlib format pin: {stats['hnswlib_format_pin']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - GX10-only entrypoint
    raise SystemExit(main())

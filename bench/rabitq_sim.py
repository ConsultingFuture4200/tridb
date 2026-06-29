"""RaBitQ / Extended-RaBitQ quantization recall simulator (Plan 008, Step 1).

WHY THIS EXISTS
---------------
Plan 008 proposes RaBitQ quantization as the **128 GB launch-headline lever**:
8-32x smaller vector footprint at near-equal recall lets a far larger corpus fit
in the GX10's unified memory, and the quantized distance estimate carries an
*unbiased* error with a published bound that can later strengthen the VBASE
early-termination guarantee (plan 007). Before any C goes into the fork, the
recall/footprint trade has to be *measured*, not assumed. This module does that
in **pure numpy**, on this x86 standin, with no engine: it quantizes a corpus to
B-bit RaBitQ codes, ranks by the quantized distance estimator, and measures
recall@10 against the full-precision L2 ranking, plus the empirical distance
error against the RaBitQ theoretical bound.

This is the de-risking step. The actual in-engine quantized storage (RaBitQ codes
alongside HNSW node vectors, fed into the plan-007 termination math) is an
explicitly DEFERRED follow-on (Step 4 of the plan) — NOT implemented here.

THE METHOD (Extended RaBitQ, SIGMOD'24 / VLDB'25)
-------------------------------------------------
RaBitQ (Gao & Long, SIGMOD 2024, "RaBitQ: Quantizing High-Dimensional Vectors
with a Theoretical Error Bound for Approximate Nearest Neighbor Search") and its
Extended-RaBitQ successor (Gao, Long et al., 2024/2025, arXiv:2409.09913,
multi-bit generalization) quantize a vector by:

  1. **Center** every vector at the corpus centroid c (RaBitQ bounds are stated
     for the residual o - c; distances are reconstructed by adding c back).
  2. **Rotate** the residual by a random orthonormal matrix P (a Johnson-
     Lindenstrauss-style transform). Rotation makes the per-coordinate sign /
     code behave like a random projection, which is what gives the *unbiased*
     estimator and the concentration (error-bound) result. We realize P as the Q
     factor of a QR decomposition of a seeded gaussian matrix (a proper Haar-ish
     orthonormal matrix); the seed makes the whole pipeline deterministic.
  3. **Encode** the rotated residual. For the 1-bit base method the code is the
     sign vector and the reconstruction is +/- (1/sqrt(d)) per coordinate scaled
     by a single per-vector factor. Extended RaBitQ generalizes this to B bits
     per dimension: a per-vector scalar `scale` plus a symmetric uniform
     B-bit grid on the normalized rotated residual. More bits -> finer grid ->
     smaller reconstruction error -> higher recall, at B/32 of the float32
     footprint per dimension.
  4. **Estimate distance** from the code: reconstruct an approximate residual
     `r_hat = scale * P @ code_levels` and use `||q - (c + r_hat)||^2`. Because
     the rotation is orthonormal and the grid is symmetric, the estimator is
     (approximately) unbiased and its error concentrates per the RaBitQ bound.

THEORETICAL ERROR BOUND (what the unit test pins)
-------------------------------------------------
RaBitQ's headline guarantee is on the *reconstruction of the unit residual*: for
the normalized rotated residual u (||u|| = 1) quantized to the B-bit grid, the
squared reconstruction error ||u - u_hat||^2 is bounded. For a symmetric uniform
B-bit grid on [-1, 1] the worst-case per-coordinate quantization step is
`step = 2 / (2^B - 1)` and the worst-case squared error per coordinate is
`(step/2)^2`, so across d coordinates the *normalized* reconstruction error is
bounded by `d * (step/2)^2 / ||rotated_residual||^2`-style terms. We assert the
empirical normalized reconstruction error stays at or below the closed-form grid
bound (a deterministic, dataset-independent ceiling), which is the host-checkable
core of the RaBitQ guarantee. We do NOT re-derive the probabilistic JL constant;
the deterministic uniform-grid ceiling is the conservative envelope and is what a
reviewer can check without the engine.

WHAT IS MEASURABLE HERE vs GATED
--------------------------------
Recall@10 of the quantized estimator vs full-precision L2, and the empirical
error vs the grid bound, are computable RIGHT NOW (pure numpy, no engine). The
in-engine storage + the GX10 CAGRA build A/B are GX10-gated and live in
scripts/gpu_build_index.* and docs/gpu_index_build_v0.1.0.md. This module never
emits a latency number and never claims one.

If no real dataset (.npy/.fvecs/.hdf5) is passed via --dataset, the simulator
runs on a SYNTHETIC clustered corpus and labels the output accordingly — the
real-dataset recall numbers are data-gated until a dataset is supplied.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Reuse the real-dataset loaders so a RaBitQ run consumes the SAME .npy/.fvecs/
# .hdf5 inputs the live benchmark does (one loader surface, no drift).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.real_corpus import load_vectors  # noqa: E402


# --------------------------------------------------------------------------- #
# Rotation (random orthonormal, deterministic from a seed)
# --------------------------------------------------------------------------- #


def random_rotation(dim: int, seed: int = 42) -> np.ndarray:
    """A deterministic random orthonormal (dim, dim) matrix P.

    Realized as the Q factor of a QR decomposition of a seeded gaussian matrix,
    with a sign correction on the diagonal of R so the result is a proper,
    reproducible orthonormal transform (a JL-style rotation, the heart of why the
    RaBitQ estimator is unbiased). Deterministic given `seed`.
    """
    rng = np.random.default_rng(seed)
    a = rng.standard_normal((dim, dim))
    q, r = np.linalg.qr(a)
    # Fix the sign ambiguity of QR so the rotation is deterministic across BLAS.
    q = q * np.sign(np.diag(r))
    return np.ascontiguousarray(q, dtype=np.float64)


# --------------------------------------------------------------------------- #
# Extended-RaBitQ quantizer
# --------------------------------------------------------------------------- #


@dataclass
class RaBitQCode:
    """A quantized corpus.

    `levels` are integer grid indices in [0, 2^bits - 1] of shape (n, dim) for the
    rotated, centered residual; `scale` is the per-vector reconstruction scalar of
    shape (n,); `centroid`/`rotation`/`bits` are the shared decode parameters.
    """

    levels: np.ndarray  # (n, dim) int, grid indices
    scale: np.ndarray  # (n,) float, per-vector reconstruction scalar
    centroid: np.ndarray  # (dim,) float
    rotation: np.ndarray  # (dim, dim) float, orthonormal P
    bits: int

    @property
    def n(self) -> int:
        return self.levels.shape[0]

    @property
    def dim(self) -> int:
        return self.levels.shape[1]

    def footprint_bits_per_vector(self) -> float:
        """Code footprint in bits per stored vector: bits/dim for the codes plus
        the one float32 per-vector `scale`. The shared centroid + rotation are
        amortized across the whole corpus (O(dim^2) once), so they are excluded
        from the per-vector figure — exactly the storage that scales with n."""
        return self.bits * self.dim + 32  # codes + one float32 scale

    @staticmethod
    def fp32_bits_per_vector(dim: int) -> float:
        return 32.0 * dim


def _grid_levels(bits: int) -> int:
    """Number of symmetric uniform levels for `bits` bits per dimension."""
    return (1 << bits) - 1  # 2^bits - 1 intervals -> 2^bits codes, symmetric


def quantize(
    vectors: np.ndarray,
    bits: int,
    *,
    seed: int = 42,
    rotation: np.ndarray | None = None,
) -> RaBitQCode:
    """Extended-RaBitQ quantize `vectors` (n, dim) to `bits` bits/dimension.

    Steps (see module docstring): center at the corpus centroid, rotate by a
    deterministic orthonormal P, then encode the rotated residual on a symmetric
    uniform B-bit grid scaled by a per-vector factor so the grid spans the
    vector's own dynamic range (this is the Extended-RaBitQ per-vector scalar,
    the generalization of the 1-bit method's single norm factor).
    """
    if bits < 1:
        raise ValueError(f"bits must be >= 1, got {bits}")
    x = np.ascontiguousarray(vectors, dtype=np.float64)
    n, dim = x.shape
    centroid = x.mean(axis=0)
    p = random_rotation(dim, seed=seed) if rotation is None else rotation
    # rotated residuals: r = P^T (x - c)  (P orthonormal -> ||r|| == ||x - c||)
    resid = (x - centroid) @ p
    # per-vector scale: the max abs component sets the grid span so every code is
    # used; a tiny epsilon avoids a zero scale on an all-zero residual.
    scale = np.maximum(np.abs(resid).max(axis=1), 1e-12)
    g = _grid_levels(bits)
    # normalize to [-1, 1], map to integer grid [0, g], symmetric round
    norm = resid / scale[:, None]
    levels = np.rint((norm + 1.0) * 0.5 * g).astype(np.int64)
    levels = np.clip(levels, 0, g)
    return RaBitQCode(
        levels=levels,
        scale=scale,
        centroid=centroid,
        rotation=p,
        bits=bits,
    )


def reconstruct(code: RaBitQCode) -> np.ndarray:
    """Reconstruct approximate full-precision vectors (n, dim) from the code.

    Inverse of :func:`quantize`: integer levels -> [-1, 1] -> * per-vector scale
    -> rotate back by P -> add the centroid. Used by the distance estimator and
    by the reconstruction-error bound check.
    """
    g = _grid_levels(code.bits)
    norm = (code.levels.astype(np.float64) / g) * 2.0 - 1.0
    resid_hat = norm * code.scale[:, None]
    # rotate back: P is orthonormal so P @ r = (r @ P^T); we rotated by @ P, undo with @ P^T
    return resid_hat @ code.rotation.T + code.centroid


# --------------------------------------------------------------------------- #
# Distance estimator + recall
# --------------------------------------------------------------------------- #


def estimated_l2_sq(code: RaBitQCode, query: np.ndarray) -> np.ndarray:
    """Estimated squared L2 from every coded vector to `query` (dim,).

    Reconstructs the approximate corpus vectors and computes exact L2 to the
    (full-precision) query — the standard RaBitQ "estimate distance from the
    code" step. Monotone with L2 (no sqrt), matching the engine's `<->` ordering.
    """
    recon = reconstruct(code)
    diff = recon - np.asarray(query, dtype=np.float64)
    return np.einsum("ij,ij->i", diff, diff)


def exact_l2_sq(vectors: np.ndarray, query: np.ndarray) -> np.ndarray:
    diff = np.asarray(vectors, dtype=np.float64) - np.asarray(query, dtype=np.float64)
    return np.einsum("ij,ij->i", diff, diff)


def recall_at_k(
    vectors: np.ndarray,
    code: RaBitQCode,
    queries: np.ndarray,
    k: int = 10,
    *,
    rerank: int | None = None,
) -> float:
    """Mean recall@k of the RaBitQ-estimated ranking vs the full-precision L2
    ranking, over all `queries` (q, dim).

    Two modes, both reported by the CLI because they answer different questions:

    * **raw estimator** (``rerank=None``): top-k purely by the quantized distance
      estimate. This measures the *intrinsic resolution of the code* — the
      quantity that feeds the plan-007 early-termination bound (the code alone has
      to be good enough to order candidates). It is the conservative number.

    * **rerank shortlist** (``rerank=R``, R >= k): take the top-R candidates by the
      cheap quantized estimate, then re-rank ONLY those R by exact full-precision
      L2 and return their top-k. This is the standard ANN deployment of RaBitQ
      (the published recall numbers are reported this way) and the relevant
      *end-to-end* recall: the quantized code is a cheap pre-filter, full precision
      decides the final order. NOTE (ADR-0006): in the fork this rerank must stay
      INSIDE the index scan on the authoritative in-scan distance — never a SQL
      re-rank. Here it is a host-side recall simulation, not the engine path.
    """
    hits = 0
    total = 0
    for q in np.asarray(queries, dtype=np.float64):
        true_topk = set(np.argsort(exact_l2_sq(vectors, q))[:k].tolist())
        est = estimated_l2_sq(code, q)
        if rerank is None:
            got = set(np.argsort(est)[:k].tolist())
        else:
            shortlist = np.argsort(est)[: max(rerank, k)]
            exact_on_short = exact_l2_sq(vectors[shortlist], q)
            got = set(shortlist[np.argsort(exact_on_short)[:k]].tolist())
        hits += len(true_topk & got)
        total += len(true_topk)
    return hits / total if total else 0.0


# --------------------------------------------------------------------------- #
# Reconstruction-error bound (the host-checkable RaBitQ guarantee core)
# --------------------------------------------------------------------------- #


def grid_error_bound(bits: int, dim: int) -> float:
    """Closed-form worst-case NORMALIZED squared reconstruction error for a
    symmetric uniform B-bit grid on the rotated residual.

    For a per-coordinate grid step `step = 2 / (2^bits - 1)` on the [-1, 1]
    normalized residual, the worst-case per-coordinate squared error is
    `(step/2)^2`; across `dim` coordinates the worst-case squared error of the
    normalized residual is `dim * (step/2)^2`. This is the deterministic ceiling
    the empirical error must stay under — the conservative envelope of the RaBitQ
    bound that needs no probabilistic constant.
    """
    g = _grid_levels(bits)
    step = 2.0 / g
    return dim * (step / 2.0) ** 2


def empirical_reconstruction_error(vectors: np.ndarray, code: RaBitQCode) -> float:
    """Mean NORMALIZED squared reconstruction error over the corpus.

    For each vector: ||resid - resid_hat||^2 / scale^2, i.e. the error of the
    normalized rotated residual (the quantity :func:`grid_error_bound` ceils).
    Comparable across datasets of different magnitudes because it is normalized by
    the per-vector scale.
    """
    recon = reconstruct(code)
    resid = np.asarray(vectors, dtype=np.float64) - code.centroid
    resid_hat = recon - code.centroid
    err = np.einsum("ij,ij->i", resid - resid_hat, resid - resid_hat)
    return float(np.mean(err / (code.scale**2)))


# --------------------------------------------------------------------------- #
# Synthetic clustered corpus (when no real dataset is supplied)
# --------------------------------------------------------------------------- #


def synthetic_corpus(
    n: int, dim: int, *, clusters: int = 16, seed: int = 7
) -> tuple[np.ndarray, np.ndarray]:
    """A clustered (topically structured) corpus + a query set near cluster
    centers. Clustered (not uniform random) so recall@k is a meaningful number —
    a uniform random corpus has no near-neighbour structure for quantization to
    preserve or lose. Returns (vectors (n, dim), queries (clusters, dim))."""
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((clusters, dim)) * 5.0
    assign = rng.integers(0, clusters, size=n)
    vectors = centers[assign] + rng.standard_normal((n, dim))
    queries = centers + rng.standard_normal((clusters, dim)) * 0.3
    return (
        np.ascontiguousarray(vectors, dtype=np.float64),
        np.ascontiguousarray(queries, dtype=np.float64),
    )


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def run_report(
    vectors: np.ndarray,
    queries: np.ndarray,
    *,
    bit_widths: tuple[int, ...] = (1, 2, 4),
    k: int = 10,
    seed: int = 42,
    rerank: int = 100,
) -> list[dict]:
    """Build the recall@k vs footprint table for each bit-width. Returns a list of
    per-bit-width dicts; the CLI renders it. A single shared rotation (seed) is
    reused across bit-widths so the only varying factor is the grid resolution.
    Reports BOTH the raw-estimator recall (feeds the plan-007 termination bound)
    and the rerank-shortlist recall (the end-to-end ANN deployment number)."""
    n, dim = vectors.shape
    rotation = random_rotation(dim, seed=seed)
    fp32 = RaBitQCode.fp32_bits_per_vector(dim)
    rows = []
    for bits in bit_widths:
        code = quantize(vectors, bits, seed=seed, rotation=rotation)
        recall_raw = recall_at_k(vectors, code, queries, k=k)
        recall_rr = recall_at_k(vectors, code, queries, k=k, rerank=rerank)
        emp_err = empirical_reconstruction_error(vectors, code)
        bound = grid_error_bound(bits, dim)
        fpv = code.footprint_bits_per_vector()
        rows.append(
            {
                "bits": bits,
                "recall_at_k_raw": recall_raw,
                "recall_at_k_rerank": recall_rr,
                "rerank": rerank,
                "footprint_bits_per_vector": fpv,
                "footprint_ratio": fp32 / fpv,
                "empirical_norm_recon_error": emp_err,
                "grid_error_bound": bound,
                "within_bound": emp_err <= bound,
            }
        )
    return rows


def _print_report(rows: list[dict], *, n: int, dim: int, k: int, source: str) -> None:
    rr = rows[0]["rerank"] if rows else 0
    print(f"[rabitq_sim] corpus: {n} vectors x {dim} dim  ({source})")
    print(f"[rabitq_sim] full-precision float32 footprint: {32 * dim} bits/vector")
    print(
        f"[rabitq_sim] raw = top-k by quantized estimate only (feeds plan-007 "
        f"term bound); rerank = top-{rr} shortlist re-scored full-precision "
        f"(end-to-end ANN recall)"
    )
    print(
        f"[rabitq_sim] {'bits':>4} | {'raw@' + str(k):>8} | {'rr@' + str(k):>8} | "
        f"{'bits/vec':>9} | {'vs fp32':>8} | {'emp.err':>9} | "
        f"{'bound':>9} | within"
    )
    print("[rabitq_sim] " + "-" * 78)
    for r in rows:
        print(
            f"[rabitq_sim] {r['bits']:>4} | {r['recall_at_k_raw']:>8.3f} | "
            f"{r['recall_at_k_rerank']:>8.3f} | "
            f"{r['footprint_bits_per_vector']:>9.0f} | "
            f"{r['footprint_ratio']:>7.1f}x | "
            f"{r['empirical_norm_recon_error']:>9.4f} | "
            f"{r['grid_error_bound']:>9.4f} | "
            f"{'yes' if r['within_bound'] else 'NO'}"
        )
    if source.startswith("synthetic"):
        print(
            "[rabitq_sim] NOTE: SYNTHETIC clustered corpus — the real-dataset "
            "recall numbers are DATA-GATED until --dataset <real .npy/.fvecs/"
            ".hdf5> is supplied. The recall/footprint TRADE-OFF SHAPE is real; "
            "the absolute numbers depend on the dataset."
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--dataset",
        type=Path,
        help="real embedding file (.npy/.fvecs/.hdf5). If omitted, a SYNTHETIC "
        "clustered corpus is used and results are labelled DATA-GATED.",
    )
    p.add_argument(
        "--hdf5-dataset",
        default="train",
        help="dataset name inside a .hdf5 file (ann-benchmarks: 'train')",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="take the first N corpus rows (public sets ship ~1M; keep the host "
        "sim bounded). 0 = all.",
    )
    p.add_argument("--queries", type=int, default=64)
    p.add_argument("--k", type=int, default=10)
    p.add_argument(
        "--bits",
        type=int,
        nargs="+",
        default=[1, 2, 4],
        help="bit-widths to evaluate (default: 1 2 4)",
    )
    p.add_argument(
        "--rerank",
        type=int,
        default=100,
        help="full-precision rerank shortlist size (the top-R by the cheap "
        "quantized estimate are re-scored exactly; standard RaBitQ deployment)",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--n", type=int, default=10000, help="synthetic corpus size")
    p.add_argument("--dim", type=int, default=128, help="synthetic corpus dim")
    args = p.parse_args(argv)

    if args.dataset is not None:
        vectors = load_vectors(
            args.dataset,
            hdf5_dataset=args.hdf5_dataset,
            limit=args.limit or None,
        )
        # Hold out the first `queries` rows as the query set; rank against the rest.
        nq = min(args.queries, vectors.shape[0] // 2)
        queries = vectors[:nq]
        corpus = vectors[nq:]
        source = f"real: {args.dataset.name}"
    else:
        corpus, queries = synthetic_corpus(args.n, args.dim, seed=args.seed)
        source = f"synthetic clustered (n={args.n}, dim={args.dim})"

    rows = run_report(
        corpus,
        queries,
        bit_widths=tuple(args.bits),
        k=args.k,
        seed=args.seed,
        rerank=args.rerank,
    )
    _print_report(rows, n=corpus.shape[0], dim=corpus.shape[1], k=args.k, source=source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

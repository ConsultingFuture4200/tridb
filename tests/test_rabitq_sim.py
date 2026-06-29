"""Unit tests for bench/rabitq_sim.py — the RaBitQ quantization recall simulator
(Plan 008, Step 1).

These check the quantizer's CONTRACT without an engine and without network/Docker,
on small synthetic vectors written in-test:

  * the random rotation is orthonormal and deterministic given a seed,
  * the empirical normalized reconstruction error stays AT OR BELOW the closed-
    form symmetric-uniform-grid bound (the host-checkable core of the RaBitQ
    error guarantee), for every bit-width,
  * reconstruction error decreases monotonically as bit-width grows,
  * recall@k (raw and reranked) is monotonic non-decreasing in bit-width,
  * quantize/reconstruct is deterministic given a fixed rotation seed,
  * the footprint accounting matches bits/dim + the per-vector scale.

The GX10 CAGRA build A/B and any in-engine quantized storage are GX10-gated and
NOT exercised here — only the host-measurable recall/error surface.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from bench import rabitq_sim as rq  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _corpus(n: int = 400, dim: int = 32, seed: int = 3) -> np.ndarray:
    """A small clustered corpus (structured, so recall is meaningful)."""
    vectors, _ = rq.synthetic_corpus(n, dim, clusters=8, seed=seed)
    return vectors


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


def test_rotation_is_orthonormal():
    p = rq.random_rotation(16, seed=1)
    # P^T P == I  (orthonormal)
    assert np.allclose(p.T @ p, np.eye(16), atol=1e-10)


def test_rotation_is_deterministic():
    a = rq.random_rotation(16, seed=7)
    b = rq.random_rotation(16, seed=7)
    assert np.array_equal(a, b)
    c = rq.random_rotation(16, seed=8)
    assert not np.allclose(a, c)


def test_rotation_preserves_norm():
    rng = np.random.default_rng(0)
    p = rq.random_rotation(24, seed=5)
    v = rng.standard_normal(24)
    assert np.isclose(np.linalg.norm(v @ p), np.linalg.norm(v), atol=1e-10)


# --------------------------------------------------------------------------- #
# Reconstruction-error bound (the RaBitQ guarantee core)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bits", [1, 2, 4, 8])
def test_reconstruction_error_within_grid_bound(bits):
    """The empirical normalized squared reconstruction error must stay at or below
    the closed-form symmetric-uniform-grid ceiling, for every bit-width. This is
    the deterministic envelope of the RaBitQ error bound a reviewer can check."""
    vectors = _corpus()
    code = rq.quantize(vectors, bits, seed=42)
    emp = rq.empirical_reconstruction_error(vectors, code)
    bound = rq.grid_error_bound(bits, vectors.shape[1])
    assert emp <= bound, f"{bits}-bit: empirical {emp} exceeds grid bound {bound}"


def test_reconstruction_error_within_bound_on_random_vectors():
    """The bound is dataset-independent — also holds on plain gaussian vectors
    (not just the clustered corpus)."""
    rng = np.random.default_rng(11)
    vectors = rng.standard_normal((300, 48))
    for bits in (1, 2, 4):
        code = rq.quantize(vectors, bits, seed=42)
        emp = rq.empirical_reconstruction_error(vectors, code)
        bound = rq.grid_error_bound(bits, vectors.shape[1])
        assert emp <= bound


def test_reconstruction_error_decreases_with_bits():
    """More bits -> finer grid -> strictly smaller reconstruction error."""
    vectors = _corpus()
    errs = [
        rq.empirical_reconstruction_error(vectors, rq.quantize(vectors, b, seed=42))
        for b in (1, 2, 4, 8)
    ]
    assert all(errs[i] > errs[i + 1] for i in range(len(errs) - 1)), errs


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_quantize_is_deterministic():
    vectors = _corpus()
    a = rq.quantize(vectors, 4, seed=42)
    b = rq.quantize(vectors, 4, seed=42)
    assert np.array_equal(a.levels, b.levels)
    assert np.allclose(a.scale, b.scale)


def test_shared_rotation_passes_through():
    """When a rotation is supplied, quantize uses it verbatim (the report path
    reuses ONE rotation across bit-widths so grid resolution is the only variable)."""
    vectors = _corpus()
    rot = rq.random_rotation(vectors.shape[1], seed=99)
    code = rq.quantize(vectors, 2, rotation=rot)
    assert np.array_equal(code.rotation, rot)


# --------------------------------------------------------------------------- #
# Recall monotonicity + footprint accounting
# --------------------------------------------------------------------------- #


def test_recall_monotonic_in_bits():
    """recall@k (both raw and reranked) is non-decreasing in bit-width."""
    vectors, queries = rq.synthetic_corpus(600, 32, clusters=8, seed=4)
    rows = rq.run_report(vectors, queries, bit_widths=(1, 2, 4, 8), k=10, rerank=100)
    raw = [r["recall_at_k_raw"] for r in rows]
    rr = [r["recall_at_k_rerank"] for r in rows]
    assert all(raw[i] <= raw[i + 1] + 1e-9 for i in range(len(raw) - 1)), raw
    assert all(rr[i] <= rr[i + 1] + 1e-9 for i in range(len(rr) - 1)), rr


def test_rerank_recall_at_least_raw():
    """Full-precision rerank of the shortlist can only help (or tie) vs the raw
    quantized ranking — it re-scores the same candidates with exact distance."""
    vectors, queries = rq.synthetic_corpus(600, 32, clusters=8, seed=4)
    code = rq.quantize(vectors, 2, seed=42)
    raw = rq.recall_at_k(vectors, code, queries, k=10)
    rr = rq.recall_at_k(vectors, code, queries, k=10, rerank=100)
    assert rr >= raw - 1e-9


def test_footprint_accounting():
    """Per-vector footprint = bits*dim (codes) + 32 (one float32 scale); the
    fp32 baseline is 32*dim."""
    vectors = _corpus(dim=64)
    code = rq.quantize(vectors, 4, seed=42)
    assert code.footprint_bits_per_vector() == 4 * 64 + 32
    assert rq.RaBitQCode.fp32_bits_per_vector(64) == 32 * 64


def test_recall_at_k_perfect_with_full_resolution_rerank():
    """A high-bit code with a full-corpus rerank shortlist recovers the exact
    top-k (sanity: the rerank uses exact distance, so a shortlist == corpus is
    exact)."""
    vectors, queries = rq.synthetic_corpus(200, 16, clusters=4, seed=2)
    code = rq.quantize(vectors, 8, seed=42)
    rr = rq.recall_at_k(vectors, code, queries, k=5, rerank=vectors.shape[0])
    assert rr == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# CLI smoke (synthetic path, no dataset)
# --------------------------------------------------------------------------- #


def test_cli_synthetic_runs(capsys):
    rc = rq.main(["--n", "300", "--dim", "16", "--k", "5", "--bits", "2", "4"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rabitq_sim" in out
    assert "DATA-GATED" in out  # synthetic path is labelled honestly

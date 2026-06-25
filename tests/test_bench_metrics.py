"""Unit tests for the SM-1..SM-5 metric derivations + report (de)serialization.

Pure-arithmetic surface: no engine, no DB, no I/O. Each SM is exercised at both
sides of its threshold so a regression in the pass/fail boundary is caught.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.metrics import (  # noqa: E402
    SM1_MIN_REDUCTION,
    SM3_MAX_CORPUS_FRACTION,
    BenchmarkReport,
    QuerySample,
    derive_metrics,
    sm1_intermediate_reduction,
    sm2_latency_win_fraction,
    sm3_corpus_examined,
    sm4_answer_parity,
    sm5_txn_atomicity,
)


def _tri(qid, *, latency, peak, examined, size=100, chunks=None, atomic=True, k=5):
    return QuerySample(
        qid=qid,
        system="tridb",
        k=k,
        latency_ms=latency,
        peak_intermediate_rows=peak,
        corpus_examined=examined,
        corpus_size=size,
        result_chunks=chunks or [],
        txn_atomic=atomic,
    )


def _base(qid, *, latency, peak, chunks=None, size=100, k=5):
    return QuerySample(
        qid=qid,
        system="baseline",
        k=k,
        latency_ms=latency,
        peak_intermediate_rows=peak,
        corpus_examined=size,
        corpus_size=size,
        result_chunks=chunks or [],
    )


# --------------------------------------------------------------------------- #
# SM-1: intermediate-result reduction
# --------------------------------------------------------------------------- #


def test_sm1_passes_at_or_above_5x():
    tri = [_tri(0, latency=1, peak=10, examined=5)]
    base = [_base(0, latency=5, peak=50)]  # exactly 5x
    r = sm1_intermediate_reduction(tri, base)
    assert r.value == 5.0
    assert r.passed
    assert r.target == SM1_MIN_REDUCTION


def test_sm1_fails_below_5x():
    tri = [_tri(0, latency=1, peak=20, examined=5)]
    base = [_base(0, latency=5, peak=50)]  # 2.5x
    r = sm1_intermediate_reduction(tri, base)
    assert not r.passed
    assert r.value == 2.5


def test_sm1_zero_tridb_intermediate_is_guarded():
    tri = [_tri(0, latency=1, peak=0, examined=0)]
    base = [_base(0, latency=5, peak=50)]
    r = sm1_intermediate_reduction(tri, base)
    assert r.value == 0.0  # guarded, does not raise


# --------------------------------------------------------------------------- #
# SM-2: latency-win fraction
# --------------------------------------------------------------------------- #


def test_sm2_passes_at_80pct():
    tri = [_tri(i, latency=1, peak=1, examined=1) for i in range(10)]
    base = [_base(i, latency=(0 if i < 2 else 5), peak=1) for i in range(10)]
    # TriDB loses on qid 0,1 (base latency 0) -> wins 8/10 = 0.8
    r = sm2_latency_win_fraction(tri, base)
    assert r.value == 0.8
    assert r.passed


def test_sm2_fails_below_80pct():
    tri = [_tri(i, latency=1, peak=1, examined=1) for i in range(10)]
    base = [_base(i, latency=(0 if i < 3 else 5), peak=1) for i in range(10)]
    r = sm2_latency_win_fraction(tri, base)  # 7/10
    assert r.value == 0.7
    assert not r.passed


# --------------------------------------------------------------------------- #
# SM-3: corpus examined (TriDB only, worst case)
# --------------------------------------------------------------------------- #


def test_sm3_passes_under_25pct():
    tri = [_tri(0, latency=1, peak=1, examined=10, size=100)]  # 10%
    r = sm3_corpus_examined(tri)
    assert r.value == 0.1
    assert r.passed
    assert r.target == SM3_MAX_CORPUS_FRACTION


def test_sm3_fails_at_or_above_25pct():
    tri = [_tri(0, latency=1, peak=1, examined=25, size=100)]  # exactly 25% -> fail (<)
    r = sm3_corpus_examined(tri)
    assert not r.passed


def test_sm3_uses_worst_case_query():
    tri = [
        _tri(0, latency=1, peak=1, examined=5, size=100),
        _tri(1, latency=1, peak=1, examined=40, size=100),  # blows the budget
    ]
    r = sm3_corpus_examined(tri)
    assert r.value == 0.4
    assert not r.passed


# --------------------------------------------------------------------------- #
# SM-4: answer-set parity
# --------------------------------------------------------------------------- #


def test_sm4_full_parity():
    tri = [_tri(0, latency=1, peak=1, examined=1, chunks=["a", "b"])]
    base = [_base(0, latency=2, peak=1, chunks=["b", "a"])]  # order-insensitive
    r = sm4_answer_parity(tri, base)
    assert r.value == 1.0
    assert r.passed


def test_sm4_partial_parity_fails():
    tri = [_tri(0, latency=1, peak=1, examined=1, chunks=["a", "b"])]
    base = [_base(0, latency=2, peak=1, chunks=["a", "c"])]  # jaccard 1/3
    r = sm4_answer_parity(tri, base)
    assert round(r.value, 3) == 0.333
    assert not r.passed


def test_sm4_both_empty_is_full_parity():
    tri = [_tri(0, latency=1, peak=1, examined=1, chunks=[])]
    base = [_base(0, latency=2, peak=1, chunks=[])]
    r = sm4_answer_parity(tri, base)
    assert r.value == 1.0
    assert r.passed


# --------------------------------------------------------------------------- #
# SM-5: transaction atomicity
# --------------------------------------------------------------------------- #


def test_sm5_all_atomic_passes():
    tri = [_tri(i, latency=1, peak=1, examined=1, atomic=True) for i in range(5)]
    r = sm5_txn_atomicity(tri)
    assert r.value == 1.0
    assert r.passed


def test_sm5_one_nonatomic_fails():
    tri = [_tri(i, latency=1, peak=1, examined=1, atomic=(i != 2)) for i in range(5)]
    r = sm5_txn_atomicity(tri)
    assert r.value == 0.8
    assert not r.passed


def test_sm5_empty_fails():
    r = sm5_txn_atomicity([])
    assert not r.passed


# --------------------------------------------------------------------------- #
# Aggregate + JSON round-trip
# --------------------------------------------------------------------------- #


def test_derive_metrics_returns_all_five_in_order():
    tri = [_tri(0, latency=1, peak=5, examined=5, chunks=["a"])]
    base = [_base(0, latency=5, peak=50, chunks=["a"])]
    metrics = derive_metrics(tri, base)
    assert [m.sm for m in metrics] == ["SM-1", "SM-2", "SM-3", "SM-4", "SM-5"]


def test_benchmark_report_json_round_trip():
    tri = [_tri(0, latency=1, peak=5, examined=5, chunks=["a"])]
    base = [_base(0, latency=5, peak=50, chunks=["a"])]
    report = BenchmarkReport(
        k=5,
        corpus_size=100,
        num_queries=1,
        engine_mode="stub",
        tridb_samples=tri,
        baseline_samples=base,
        metrics=derive_metrics(tri, base),
    )
    text = report.to_json()
    restored = BenchmarkReport.from_json(text)
    assert restored.k == report.k
    assert restored.engine_mode == "stub"
    assert restored.all_passed == report.all_passed
    assert [m.sm for m in restored.metrics] == ["SM-1", "SM-2", "SM-3", "SM-4", "SM-5"]
    assert restored.tridb_samples[0].result_chunks == ["a"]

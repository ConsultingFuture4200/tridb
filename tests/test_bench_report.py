"""Unit tests for the HTML report renderer (DEV-1173)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.metrics import BenchmarkReport, QuerySample, derive_metrics  # noqa: E402
from bench.report import render_report  # noqa: E402


def _report(*, passing: bool, engine_mode: str = "stub") -> BenchmarkReport:
    if passing:
        tri = [
            QuerySample(
                0,
                "tridb",
                5,
                latency_ms=1.0,
                peak_intermediate_rows=5,
                corpus_examined=5,
                corpus_size=100,
                result_chunks=["a"],
            ),
        ]
        base = [
            QuerySample(
                0,
                "baseline",
                5,
                latency_ms=9.0,
                peak_intermediate_rows=50,
                corpus_examined=100,
                corpus_size=100,
                result_chunks=["a"],
            ),
        ]
    else:
        # SM-4 fails (different answer sets) and SM-1 fails (no reduction).
        tri = [
            QuerySample(
                0,
                "tridb",
                5,
                latency_ms=9.0,
                peak_intermediate_rows=50,
                corpus_examined=90,
                corpus_size=100,
                result_chunks=["x"],
            ),
        ]
        base = [
            QuerySample(
                0,
                "baseline",
                5,
                latency_ms=1.0,
                peak_intermediate_rows=50,
                corpus_examined=100,
                corpus_size=100,
                result_chunks=["a"],
            ),
        ]
    return BenchmarkReport(
        k=5,
        corpus_size=100,
        num_queries=1,
        engine_mode=engine_mode,
        tridb_samples=tri,
        baseline_samples=base,
        metrics=derive_metrics(tri, base),
    )


def test_render_passing_report_is_self_contained_html():
    html = render_report(_report(passing=True))
    assert html.startswith("<!doctype html>")
    assert "TriDB Benchmark Report" in html
    # No external assets / no JS (read-once requirement).
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html
    # All five SMs appear in the scoreboard.
    for sm in ("SM-1", "SM-2", "SM-3", "SM-4", "SM-5"):
        assert sm in html
    assert "ALL METRICS PASS" in html


def test_render_failing_report_shows_fail_and_targeted_recs():
    html = render_report(_report(passing=False))
    assert "ONE OR MORE METRICS FAIL" in html
    assert "FAIL" in html
    # SM-4 failure recommendation must surface (correctness regression).
    assert "answer parity" in html.lower()


def test_render_escapes_chunk_text():
    tri = [
        QuerySample(
            0,
            "tridb",
            5,
            latency_ms=1.0,
            peak_intermediate_rows=5,
            corpus_examined=5,
            corpus_size=100,
            result_chunks=["<script>x"],
        ),
    ]
    base = [
        QuerySample(
            0,
            "baseline",
            5,
            latency_ms=9.0,
            peak_intermediate_rows=50,
            corpus_examined=100,
            corpus_size=100,
            result_chunks=["<script>x"],
        ),
    ]
    report = BenchmarkReport(
        k=5,
        corpus_size=100,
        num_queries=1,
        engine_mode="stub",
        tridb_samples=tri,
        baseline_samples=base,
        metrics=derive_metrics(tri, base),
    )
    html = render_report(report)
    # The renderer never emits a raw <script>; the only thing resembling chunk
    # text would be escaped. Confirm no executable script tag slipped in.
    assert "<script>x" not in html


def test_stub_mode_emits_caveat():
    html = render_report(_report(passing=True, engine_mode="stub"))
    assert "stub" in html
    assert "engine-gated" in html.lower()

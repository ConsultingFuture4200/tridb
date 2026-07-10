"""Characterization tests for the wiki head-to-head publication gate (DEV-1354).

`publication_gate` is the hard gate that refuses a public 'Yx faster' headline until
every reviewer blocker reconciles. It has ZERO tests/ coverage yet CI only runs
`pytest tests/` — a silent gate regression would only surface on the next Spark
re-run. These pin the current gate semantics: one healthy pass case (== []) plus one
case per blocker branch asserting a stable substring. No DB, no network — the module
reads env vars at import time only.

Substrings (not full strings) are asserted so minor wording changes don't break the
suite; if a gate is intentionally changed, update the matching assertion in the same PR."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wiki_h2h import publication_gate  # noqa: E402


def _healthy_meta() -> dict:
    """oracle_meta with every gate cleared: matched edge counts, all HNSW builds
    healthy, a cap comfortably above the operating point's examined."""
    return {
        "engine_edges": "1000",
        "neo4j_edges": "1000",
        "hnsw_healthy_builds": "3",
        "hnsw_total_builds": "3",
        "tjs_max_examined": "5000",
    }


def _tp() -> tuple[str, dict]:
    # TriDB matched operating point: recall matched to baseline, examined > 0 and < cap.
    return ("tridb", {"recall_at_k": 0.95, "median_examined": 90})


def _bp() -> tuple[str, dict]:
    return ("baseline", {"recall_at_k": 0.95, "median_examined": 500})


def _pass_env(monkeypatch) -> None:
    """Put the env-var gates into their passing state and neutralize overrides so the
    only thing a test varies is the input it is exercising."""
    monkeypatch.setenv("WH_BOUNDARY_PARITY", "1")
    monkeypatch.delenv("WH_MIN_HEALTHY_BUILDS", raising=False)


def test_healthy_case_is_publishable(monkeypatch):
    _pass_env(monkeypatch)
    assert publication_gate(_tp(), _bp(), _healthy_meta()) == []


def test_graph_edge_counts_unknown_blocks(monkeypatch):
    _pass_env(monkeypatch)
    meta = _healthy_meta()
    del meta["engine_edges"]
    blockers = publication_gate(_tp(), _bp(), meta)
    assert any("graph-set" in b for b in blockers)


def test_graph_edge_mismatch_blocks(monkeypatch):
    _pass_env(monkeypatch)
    meta = _healthy_meta()
    meta["neo4j_edges"] = "2000"  # engine 1000 != oracle 2000
    blockers = publication_gate(_tp(), _bp(), meta)
    assert any("graph-set MISMATCH" in b for b in blockers)


def test_timer_boundary_parity_unset_blocks(monkeypatch):
    _pass_env(monkeypatch)
    monkeypatch.delenv("WH_BOUNDARY_PARITY", raising=False)
    blockers = publication_gate(_tp(), _bp(), _healthy_meta())
    assert any("timer boundary" in b for b in blockers)


def test_hnsw_build_health_undeclared_blocks(monkeypatch):
    _pass_env(monkeypatch)
    meta = _healthy_meta()
    del meta["hnsw_healthy_builds"]
    blockers = publication_gate(_tp(), _bp(), meta)
    assert any("HNSW build" in b for b in blockers)


def test_hnsw_build_non_reproducible_blocks(monkeypatch):
    _pass_env(monkeypatch)
    meta = _healthy_meta()
    meta["hnsw_healthy_builds"] = "2"  # 2/3 healthy, need all >= 3
    blockers = publication_gate(_tp(), _bp(), meta)
    assert any("HNSW build" in b for b in blockers)


def test_no_matched_operating_point_blocks(monkeypatch):
    _pass_env(monkeypatch)
    blockers = publication_gate(None, _bp(), _healthy_meta())
    assert any("no matched operating point" in b for b in blockers)


def test_recall_not_matched_blocks(monkeypatch):
    _pass_env(monkeypatch)
    tp = ("tridb", {"recall_at_k": 0.95, "median_examined": 90})
    bp = ("baseline", {"recall_at_k": 0.80, "median_examined": 500})  # |Δ|=0.15 > eps
    blockers = publication_gate(tp, bp, _healthy_meta())
    assert any("recall NOT matched" in b for b in blockers)


def test_examined_zero_is_seqscan_blocks(monkeypatch):
    _pass_env(monkeypatch)
    tp = ("tridb", {"recall_at_k": 0.95, "median_examined": 0})
    blockers = publication_gate(tp, _bp(), _healthy_meta())
    assert any("did NOT use the HNSW index" in b for b in blockers)


def test_examined_at_cap_is_censored_blocks(monkeypatch):
    _pass_env(monkeypatch)
    tp = (
        "tridb",
        {"recall_at_k": 0.95, "median_examined": 5000},
    )  # == tjs_max_examined
    blockers = publication_gate(tp, _bp(), _healthy_meta())
    assert any("CENSORED" in b for b in blockers)

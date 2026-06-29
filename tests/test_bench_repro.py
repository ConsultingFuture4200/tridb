"""Tests for the one-command public-dataset repro orchestrator (make bench-repro).

No network, no engine, no large dataset: a tiny synthetic HotpotQA-shaped manifest
exercises the host-side recall assembly and the rendered table; the SIFT section is
checked for its honest gated/missing behaviour (a missing file must report MISSING,
never fabricate a number).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import bench_repro  # noqa: E402


def _tiny_manifest(tmp_path: Path) -> Path:
    """A minimal manifest in the shape bench.graphrag_report.load_slice expects."""
    rng = np.random.default_rng(0)
    n, d = 6, 8
    corpus = rng.standard_normal((n, d)).astype(np.float32)
    corpus /= np.linalg.norm(corpus, axis=1, keepdims=True)
    queries = corpus[:2].copy()
    cpath = tmp_path / "corpus.npy"
    qpath = tmp_path / "query.npy"
    np.save(cpath, corpus)
    np.save(qpath, queries)
    manifest = {
        "corpus_emb_path": str(cpath),
        "query_emb_path": str(qpath),
        "k": 5,
        "_edges": [[0, 3], [1, 4]],  # bridges from the top vector hits
        "paragraphs": [
            {"id": i, "title": f"T{i}", "text": f"text {i}"} for i in range(n)
        ],
        "questions": [
            {
                "qid": 0,
                "question": "q0?",
                "answer": "a0",
                "type": "bridge",
                "gold_ids": [0, 3],
                "gold_titles": ["T0", "T3"],
            },
            {
                "qid": 1,
                "question": "q1?",
                "answer": "a1",
                "type": "comparison",
                "gold_ids": [1],
                "gold_titles": ["T1"],
            },
        ],
    }
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    return mpath


def test_run_hotpot_structure(tmp_path):
    mpath = _tiny_manifest(tmp_path)
    hot = bench_repro.run_hotpot(mpath, ks=[2, 3, 5], reader_k=5)
    assert hot["n_questions"] == 2
    assert hot["n_paragraphs"] == 6
    assert hot["headline_group"] == "bridge"
    # the sweep is keyed by retriever -> k -> group -> metric
    sw = hot["sweep"]
    assert {"vector_only", "graph_inject", "graph_rerank"} <= set(sw)
    for k in hot["ks"]:
        assert "joint" in sw["vector_only"][k]["bridge"]
        assert 0.0 <= sw["vector_only"][k]["bridge"]["joint"] <= 1.0


def test_render_md_contains_honesty_split(tmp_path):
    mpath = _tiny_manifest(tmp_path)
    hot = bench_repro.run_hotpot(mpath, ks=[2, 3, 5], reader_k=5)
    sift = {
        "dataset": "sift-128-euclidean",
        "dim": 128,
        "distance": "euclidean",
        "pinned_sha256": "deadbeef",
        "checksum_verified": True,
        "state": "PINNED + VERIFIED; exact oracle built host-side",
        "oracle_selfcheck_recall": 1.0,
        "live_recall_at_k": "GX10/engine-gated (needs live tjs() transcript)",
        "latency": "GX10/engine-gated",
    }
    md = bench_repro.render_md(hot, sift, reader_k=5)
    # the rendered table must surface the honest split, never a fabricated latency
    assert "Honesty split" in md
    assert "GX10/engine-gated" in md
    assert "graph_inject joint" in md  # the real-recall table is present
    assert "sift-128-euclidean" in md


def test_sift_missing_file_reports_missing(tmp_path):
    """A missing dataset must report MISSING, not fabricate a recall/latency."""
    out = bench_repro.run_sift(
        tmp_path / "nope.hdf5", limit=100, queries=2, k=5, seed=42
    )
    assert out["present"] is False
    assert "MISSING" in out["state"]
    assert "oracle_selfcheck_recall" not in out
    assert out["checksum_verified"] is False

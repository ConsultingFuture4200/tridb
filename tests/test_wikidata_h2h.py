"""Host tests for Harness B — Wikidata fused KBQA h2h (plan 060).

Covers the host-runnable layer: dense id mapping, typed out-reach, the filter-first candidate
set + exact oracle over a tiny synthetic slice, query sampling, and the REUSED publication_gate
(that it blocks a deliberately mismatched-graph meta and clears when parity holds). The live
tridb-emit / baseline legs are GX10/Spark-gated and not exercised here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_h2h import (  # noqa: E402
    WCfg,
    candidates,
    compute_oracle,
    load_emb,
    load_dense_map,
    load_manifest,
    load_types,
    load_typed_adj,
    publication_gate,
    render_report,
    sample_queries,
    typed_reach,
)

# dense: Q10->0 Q20->1 Q30->2 Q99->3 Q88->4 Q1->5 Q77->6 (emission order below)
ENTITIES = [
    {"id": 10, "label": "Star Alpha", "description": ""},
    {"id": 20, "label": "Star Beta", "description": ""},
    {"id": 30, "label": "Planet Gamma", "description": ""},
    {"id": 99, "label": "star", "description": "stellar type"},
    {"id": 88, "label": "planet", "description": "planetary type"},
    {"id": 1, "label": "Galaxy X", "description": ""},
    {"id": 77, "label": "galaxy", "description": "galaxy type"},
]
EDGES = [(1, 50, 10), (1, 50, 20), (1, 50, 30)]  # X --P50--> {Star,Star,Planet}
CLAIMS = [
    {"id": 10, "P31": [99]},
    {"id": 20, "P31": [99]},
    {"id": 30, "P31": [88]},
    {"id": 99, "P31": []},
    {"id": 88, "P31": []},
    {"id": 1, "P31": [77]},
    {"id": 77, "P31": []},
]
# 4-dim embeddings, dense-indexed; Star Alpha(0) closest to Galaxy X(5), then Star Beta(1)
EMB = np.array(
    [
        [1.0, 0.0, 0.0, 0.0],  # 0 Star Alpha
        [0.9, 0.1, 0.0, 0.0],  # 1 Star Beta
        [0.0, 1.0, 0.0, 0.0],  # 2 Planet
        [0.0, 0.0, 1.0, 0.0],  # 3 star type
        [0.0, 0.0, 0.0, 1.0],  # 4 planet type
        [1.0, 0.05, 0.0, 0.0],  # 5 Galaxy X (anchor)
        [0.0, 0.0, 0.5, 0.5],  # 6 galaxy type
    ],
    dtype=np.float32,
)


def _build_slice(tmp_path: Path) -> WCfg:
    (tmp_path / "entities-00000.jsonl").write_text(
        "\n".join(json.dumps(e) for e in ENTITIES)
    )
    (tmp_path / "edges-00000.tsv").write_text(
        "\n".join(f"{s}\t{p}\t{d}" for s, p, d in EDGES)
    )
    (tmp_path / "claims-00000.jsonl").write_text(
        "\n".join(json.dumps(c) for c in CLAIMS)
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "shards": {
                    "entities": {"files": [{"path": "entities-00000.jsonl"}]},
                    "edges": {"files": [{"path": "edges-00000.tsv"}]},
                    "claims": {"files": [{"path": "claims-00000.jsonl"}]},
                }
            }
        )
    )
    emb_dir = tmp_path / "emb"
    emb_dir.mkdir()
    np.save(emb_dir / "dense_id_aligned.npy", EMB)
    return WCfg(slice_dir=tmp_path, emb_path=emb_dir / "dense_id_aligned.npy", dim=4)


def test_dense_map_is_emission_order(tmp_path):
    cfg = _build_slice(tmp_path)
    qmap, dense_to_qid = load_dense_map(cfg, load_manifest(cfg))
    assert qmap == {10: 0, 20: 1, 30: 2, 99: 3, 88: 4, 1: 5, 77: 6}
    assert dense_to_qid[5] == 1  # dense 5 is Q1 (Galaxy X)


def test_typed_adj_and_types_remapped_to_dense(tmp_path):
    cfg = _build_slice(tmp_path)
    manifest = load_manifest(cfg)
    qmap, _ = load_dense_map(cfg, manifest)
    adj = load_typed_adj(cfg, manifest, qmap)
    types = load_types(cfg, manifest, qmap)
    assert adj[5] == [(50, 0), (50, 1), (50, 2)]  # Q1 -> dense 0,1,2 via P50
    assert types[0] == {3} and types[2] == {4}  # star / planet types, dense


def test_typed_reach_out_direction_only(tmp_path):
    cfg = _build_slice(tmp_path)
    manifest = load_manifest(cfg)
    qmap, _ = load_dense_map(cfg, manifest)
    adj = load_typed_adj(cfg, manifest, qmap)
    assert typed_reach(adj, 5, 50, hops=1) == {0, 1, 2}
    assert typed_reach(adj, 5, 999, hops=1) == set()  # no such property
    assert (
        typed_reach(adj, 0, 50, hops=1) == set()
    )  # a leaf has no out-edges (not backlinks)


def test_candidates_filter_first(tmp_path):
    cfg = _build_slice(tmp_path)
    manifest = load_manifest(cfg)
    qmap, _ = load_dense_map(cfg, manifest)
    adj = load_typed_adj(cfg, manifest, qmap)
    types = load_types(cfg, manifest, qmap)
    # X=dense5 --P50--> {0,1,2}, filter to type 3 (star) -> {0,1}
    assert candidates(adj, types, 5, 50, 3, hops=1) == [0, 1]
    assert candidates(adj, types, 5, 50, 4, hops=1) == [2]  # planet type
    assert candidates(adj, types, 5, 50, None, hops=1) == [0, 1, 2]  # no type filter
    # p_id=None (any property) still reaches all out-neighbours
    assert typed_reach(adj, 5, None, hops=1) == {0, 1, 2}


def test_oracle_exact_rank_by_similarity(tmp_path):
    cfg = _build_slice(tmp_path)
    manifest = load_manifest(cfg)
    qmap, dense_to_qid = load_dense_map(cfg, manifest)
    adj = load_typed_adj(cfg, manifest, qmap)
    types = load_types(cfg, manifest, qmap)
    emb = load_emb(cfg, len(dense_to_qid))
    q = [{"x": 5, "p": 50, "t": 3}]
    oracle = compute_oracle(emb, adj, types, q, k=10, hops=1)
    # both star-type members survive the filter; Star Alpha(0) is nearer Galaxy X than Beta(1)
    assert oracle[0] == [0, 1]


def test_sample_queries_wellformed(tmp_path):
    cfg = _build_slice(tmp_path)
    manifest = load_manifest(cfg)
    qmap, _ = load_dense_map(cfg, manifest)
    adj = load_typed_adj(cfg, manifest, qmap)
    types = load_types(cfg, manifest, qmap)
    qs = sample_queries(adj, types, q=5, seed=1, hops=1, min_candidates=2)
    assert qs == [
        {"x": 5, "p": 50, "t": 3}
    ]  # the only anchor with a >=2 same-type reach


# --------------------------------------------------------------------------- #
# the REUSED honesty gate (plan 060: publication_gate unchanged)
# --------------------------------------------------------------------------- #
def _matched_point():
    tp = (
        "m16h2t128",
        {"recall_at_k": 0.95, "median_latency_ms": 4.7, "median_examined": 120},
    )
    bp = ("neo_pg_milvus", {"recall_at_k": 0.95, "median_latency_ms": 22.0})
    return tp, bp


def test_gate_blocks_mismatched_graph():
    tp, bp = _matched_point()
    meta = {
        "engine_edges": 1000,
        "neo4j_edges": 1200,  # deliberate mismatch
        "hnsw_healthy_builds": 3,
        "hnsw_total_builds": 3,
        "tjs_max_examined": 4000,
    }
    blockers = publication_gate(tp, bp, meta)
    assert any("graph-set MISMATCH" in b for b in blockers)


def test_gate_clears_when_parity_holds(monkeypatch):
    monkeypatch.setenv("WH_BOUNDARY_PARITY", "1")
    monkeypatch.setenv("WH_MIN_HEALTHY_BUILDS", "3")
    tp, bp = _matched_point()
    meta = {
        "engine_edges": 1000,
        "neo4j_edges": 1000,  # matched topology
        "hnsw_healthy_builds": 3,
        "hnsw_total_builds": 3,
        "tjs_max_examined": 4000,
    }
    assert publication_gate(tp, bp, meta) == []


def test_gate_blocks_unmatched_recall(monkeypatch):
    monkeypatch.setenv("WH_BOUNDARY_PARITY", "1")
    monkeypatch.setenv("WH_MIN_HEALTHY_BUILDS", "3")
    tp = (
        "m16h2t128",
        {"recall_at_k": 0.98, "median_latency_ms": 4.7, "median_examined": 120},
    )
    bp = ("neo_pg_milvus", {"recall_at_k": 0.80, "median_latency_ms": 22.0})
    meta = {
        "engine_edges": 1000,
        "neo4j_edges": 1000,
        "hnsw_healthy_builds": 3,
        "hnsw_total_builds": 3,
        "tjs_max_examined": 4000,
    }
    assert any("recall NOT matched" in b for b in publication_gate(tp, bp, meta))


def test_render_report_blocks_then_headlines(monkeypatch):
    tridb = {
        "m16h2t128": {
            "recall_at_k": 0.95,
            "median_latency_ms": 4.7,
            "median_examined": 120,
        }
    }
    baseline = {"neo_pg_milvus": {"recall_at_k": 0.95, "median_latency_ms": 22.0}}
    bad_meta = {
        "engine_edges": 1000,
        "neo4j_edges": 1200,
        "hnsw_healthy_builds": 3,
        "hnsw_total_builds": 3,
        "tjs_max_examined": 4000,
    }
    md, blockers = render_report(tridb, baseline, bad_meta, target=0.90)
    assert blockers and "COMPARISON INVALID" in md and "speedup" not in md

    monkeypatch.setenv("WH_BOUNDARY_PARITY", "1")
    monkeypatch.setenv("WH_MIN_HEALTHY_BUILDS", "3")
    good_meta = {**bad_meta, "neo4j_edges": 1000}
    md2, blockers2 = render_report(tridb, baseline, good_meta, target=0.90)
    assert blockers2 == [] and "speedup: 4.68×" in md2


def test_typed_reach_excludes_src_on_cycle():
    """A 2-hop cycle over a symmetric property (x -P-> a -P-> x) must not put the
    anchor in its own reach — the documented contract, matched by the engine BFS
    (gph_traverse_bfs excludes the seed). Caught live on the 1M slice (P47)."""
    adj = {0: [(1, 1)], 1: [(1, 0), (1, 2)]}
    assert typed_reach(adj, 0, 1, 2) == {1, 2}


def test_oracle_tie_break_is_deterministic_id_ascending():
    """At equal distance to the anchor, compute_oracle must pin ties by id ascending
    (np.lexsort), so the ranking is reproducible run-to-run and matches the engine's
    `ORDER BY <->, e.id`. Candidates 0 and 1 sit at the SAME cosine to X; 2 is farther."""
    # emb indexed by dense id. X=dense 5. 0 and 1 identical (tie); 2 orthogonal (farther).
    emb = np.array(
        [
            [1.0, 0.0, 0.0],  # 0  tie with 1
            [1.0, 0.0, 0.0],  # 1  tie with 0
            [0.0, 1.0, 0.0],  # 2  farther
            [0.0, 0.0, 0.0],  # 3  unused
            [0.0, 0.0, 0.0],  # 4  unused
            [1.0, 0.0, 0.0],  # 5  = anchor X
        ],
        dtype=np.float64,
    )
    adj = {5: [(50, 0), (50, 1), (50, 2)]}  # (property, dst): X --P50--> {0,1,2}
    types = {0: {3}, 1: {3}, 2: {3}}
    q = [{"x": 5, "p": 50, "t": 3}]
    first = compute_oracle(emb, adj, types, q, k=10, hops=1)
    second = compute_oracle(emb, adj, types, q, k=10, hops=1)
    assert first == second  # reproducible across calls
    assert first[0] == [0, 1, 2]  # tie (0,1) broken id-ascending, then farther 2

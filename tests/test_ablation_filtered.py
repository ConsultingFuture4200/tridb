"""Tests for the filtered-search + tri-modal fusion ablation harnesses (Plan 015)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.ablation_report import (  # noqa: E402
    Slice,
    _relational_mask,
    recall_at_k,
    retrieve_fusion,
    retrieve_fusion_hardfilter,
    retrieve_vector,
)
from bench.filtered_report import grade, parse
from tools.filtered_corpus import exact_filtered_topk


# --------------------------------------------------------------------------- #
# Filtered-search oracle + report parser
# --------------------------------------------------------------------------- #
def test_exact_filtered_topk_respects_selectivity_and_distance():
    base = np.array([[0.0], [1.0], [2.0], [3.0]], dtype=np.float32)
    labels = np.array([0, 50, 99, 10], dtype=np.int64)
    q = np.array([0.0], dtype=np.float32)
    # thresh=50 -> only ids {1,2} pass; nearest to q=0 is id1 (dist 1) then id2 (dist 2)
    assert exact_filtered_topk(base, labels, q, thresh=50, k=10) == [1, 2]
    # thresh=0 -> all pass; nearest order 0,1,2,3
    assert exact_filtered_topk(base, labels, q, thresh=0, k=2) == [0, 1]


def test_filtered_report_parse_and_grade():
    raw = (
        "#FILT QSTART qid=0 sel=1 run=0 tag=WARM\nTime: 9.0 ms\n 5\n 7\n"
        "#FILT QEND qid=0 sel=1 run=0\n"
        "#FILT QSTART qid=0 sel=1 run=1 tag=RUN\n 5\n 7\nTime: 2.0 ms\n"
        "#FILT QEND qid=0 sel=1 run=1\n#FILT DONE\n"
    )
    p = parse(raw)
    assert p[(0, 1)]["ids"] == [5, 7]
    assert p[(0, 1)]["times"] == [2.0]
    g = grade(p, {"k": 10, "oracle": {"0:1": [5, 7, 9]}})
    assert abs(g[1]["recall_at_k"] - 2 / 3) < 1e-9  # got 5,7 of truth 5,7,9


# --------------------------------------------------------------------------- #
# Ablation: relational mask + fusion mechanism
# --------------------------------------------------------------------------- #
def _toy_ablation() -> Slice:
    # doc0 ~ query; doc1 is the 2nd-hop gold (opposite the query, vector misses it)
    # but is graph-connected to doc0 and shares the relational constraint.
    emb = np.array([[1.0, 0.0], [-1.0, 0.0], [0.9, 0.1], [0.8, 0.2]], dtype=np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    docs = [
        {"id": 0, "title": "a", "category": "tech", "source": "X", "ym": 202301},
        {"id": 1, "title": "b", "category": "tech", "source": "X", "ym": 202301},
        {"id": 2, "title": "c", "category": "sport", "source": "Y", "ym": 202301},
        {"id": 3, "title": "d", "category": "sport", "source": "Y", "ym": 202301},
    ]
    q = {
        "qid": 0,
        "query": "q",
        "answer": "",
        "question_type": "inference_query",
        "gold_ids": [0, 1],
        "rel_categories": ["tech"],
        "rel_sources": ["X"],
        "rel_ym_min": 202301,
        "rel_ym_max": 202301,
    }
    return Slice(
        corpus_emb=emb,
        query_emb=np.array([[1.0, 0.0]], dtype=np.float32),
        docs=docs,
        questions=[q],
        out_adj={0: [1]},  # graph bridge 0 -> 1
        title_key={"a": 0, "b": 1, "c": 2, "d": 3},
        k=3,
    )


def test_relational_mask_matches_constraint():
    sl = _toy_ablation()
    mask = _relational_mask(sl, sl.questions[0])
    assert mask.tolist() == [True, True, False, False]  # only tech/X docs


def test_fusion_recovers_graph_relational_bridge_vector_misses():
    sl = _toy_ablation()
    gold = sl.questions[0]["gold_ids"]
    vec = recall_at_k(retrieve_vector(sl, 0, 3), gold, 3)
    fus = recall_at_k(retrieve_fusion(sl, 0, 3), gold, 3)
    assert 1 not in set(
        retrieve_vector(sl, 0, 3)
    )  # vector misses the opposite-pole gold
    assert 1 in set(retrieve_fusion(sl, 0, 3))  # fusion injects the gated graph bridge
    assert fus > vec


def test_query_parsed_constraint_no_gold_leakage():
    from tools.multihoprag_corpus import _parse_query_constraint

    sources = ["The Verge", "TechCrunch"]
    cats = ["technology", "sports"]
    # source + year named in the query -> parsed constraint (deployable, no gold)
    qc, qs, ymin, ymax = _parse_query_constraint(
        "According to The Verge in 2023, what happened?", sources, cats
    )
    assert qs == ["The Verge"] and ymin == 202301 and ymax == 202312
    # no relational cue -> empty constraint (relational leg is a no-op)
    qc2, qs2, ymin2, ymax2 = _parse_query_constraint("Who won?", sources, cats)
    assert qc2 == [] and qs2 == [] and ymin2 == 0


def test_fusion_hardfilter_caps_when_constraint_excludes_gold():
    sl = _toy_ablation()
    # make the gold bridge (doc1) fail the relational constraint -> hard filter drops it
    sl.docs[1]["category"] = "sport"
    sl.docs[1]["source"] = "Y"
    got = set(retrieve_fusion_hardfilter(sl, 0, 3))
    assert 1 not in got  # hard pre-filter cannot recover an excluded gold

"""Tests for the GraphRAG QA-accuracy benchmark (Plan 015) — no network, no engine.

Covers: the real mention-graph builder, gold resolution, the EM/F1 + evidence
metrics, the vector-only vs graph-constrained retrievers (the thesis mechanism),
and the manifest/SQL drop-in contract (reuses tools.bench_corpus.build_sql).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.graphrag_report import (  # noqa: E402
    Slice,
    em_score,
    evidence_scores,
    f1_score,
    reader_scores,
    retrieve_graph,
    retrieve_graph_inject,
    retrieve_vector,
)
from tools.build_wiki_graph import build_graph  # noqa: E402
from tools.hotpot_corpus import build_manifest, emit_bench_sql  # noqa: E402


# --------------------------------------------------------------------------- #
# Phase 2 — real mention graph
# --------------------------------------------------------------------------- #
def _questions():
    return [
        {
            "id": "q1",
            "question": "Who directed Film X?",
            "answer": "Jane Doe",
            "type": "bridge",
            "level": "hard",
            "supporting_facts": [["Film X", 0], ["Jane Doe", 0]],
            "context": [
                ["Film X", ["Film X is a 2020 movie directed by Jane Doe.", "A hit."]],
                ["Jane Doe", ["Jane Doe is an American director.", "She makes films."]],
                ["Unrelated Topic", ["Something entirely about cats and dogs."]],
            ],
        }
    ]


def test_mention_edges_are_embedding_independent_and_directed():
    g = build_graph(_questions())
    assert len(g.paragraphs) == 3
    t2id = g.title_to_id
    film = t2id["film x"]
    jane = t2id["jane doe"]
    # "Film X" body mentions "Jane Doe" -> directed edge film -> jane.
    assert (film, jane) in g.edges
    # "Jane Doe" body does NOT mention "Film X"; no reverse edge.
    assert (jane, film) not in g.edges
    # distractor participates in no edge
    unrel = t2id["unrelated topic"]
    assert all(unrel not in (s, d) for s, d in g.edges)


def test_gold_resolution():
    g = build_graph(_questions())
    q = g.questions[0]
    assert q["n_gold"] == 2 and q["n_gold_resolved"] == 2
    assert set(q["gold_ids"]) == {g.title_to_id["film x"], g.title_to_id["jane doe"]}


# --------------------------------------------------------------------------- #
# Phase 4 — metrics
# --------------------------------------------------------------------------- #
def test_em_and_f1_hotpot_normalization():
    assert em_score("The Beatles", "beatles") == 1.0  # article + case stripped
    assert em_score("yes", "no") == 0.0
    assert f1_score("New York City", "New York") > 0.6
    assert f1_score("", "") == 1.0


def test_evidence_scores_joint_requires_all_gold():
    assert evidence_scores([1, 2, 3], [1, 2])["joint"] == 1.0
    assert evidence_scores([1, 9, 8], [1, 2])["joint"] == 0.0
    assert evidence_scores([1, 9, 8], [1, 2])["recall"] == 0.5


# --------------------------------------------------------------------------- #
# Phase 4 — the thesis mechanism: graph reaches a 2nd-hop gold the vector misses
# --------------------------------------------------------------------------- #
def _toy_slice() -> Slice:
    # para0 ~ query; para3 is OPPOSITE the query (vector misses it) but is reached
    # from para0 via a mention edge -> graph-constrained must recover it.
    emb = np.array([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [-1.0, 0.0]], dtype=np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    q = np.array([[1.0, 0.0]], dtype=np.float32)
    return Slice(
        corpus_emb=emb,
        query_emb=q,
        paragraphs=[{"id": i, "title": f"p{i}", "text": ""} for i in range(4)],
        questions=[
            {
                "qid": 0,
                "question": "?",
                "answer": "a",
                "type": "bridge",
                "gold_ids": [0, 3],
                "gold_titles": ["p0", "p3"],
            }
        ],
        out_adj={0: [3]},
        k=2,
    )


def test_graph_inject_recovers_second_hop_vector_misses():
    """The REAL mechanism: inject the graph-reachable bridge into the context even
    though it has the WORST query similarity — vector-only can never surface it."""
    sl = _toy_slice()
    gold = set(sl.questions[0]["gold_ids"])
    vec = set(retrieve_vector(sl, 0, k=2))
    inj = set(retrieve_graph_inject(sl, 0, k=2, seeds=1, hops=1))
    assert 3 not in vec  # opposite-pole gold is invisible to pure vector search
    assert 3 in inj  # injected via the mention edge regardless of similarity
    assert (
        evidence_scores(list(inj), list(gold))["recall"]
        > evidence_scores(list(vec), list(gold))["recall"]
    )


def test_graph_rerank_cannot_promote_a_low_similarity_bridge():
    """The naive ablation: with vector-similar candidates filling k, gating +
    re-ranking by QUERY cosine still buries the low-similarity bridge."""
    sl = _toy_slice()
    extra = np.array([[0.95, 0.05], [0.85, 0.15]], dtype=np.float32)
    extra /= np.linalg.norm(extra, axis=1, keepdims=True)
    sl.corpus_emb = np.vstack([sl.corpus_emb, extra])
    sl.paragraphs += [
        {"id": 4, "title": "p4", "text": ""},
        {"id": 5, "title": "p5", "text": ""},
    ]
    sl.out_adj = {0: [4, 5, 3]}  # bridge 3 competes with vector-similar 4,5
    rer = set(retrieve_graph(sl, 0, k=2, seeds=1, hops=1))  # alias -> rerank
    assert 3 not in rer  # query-cosine re-rank keeps 4/5 over the bridge


# --------------------------------------------------------------------------- #
# Reader-failure accounting (advisor plan 014): a failed reader call is tallied
# and EXCLUDED from the EM/F1 denominator, not scored 0.
# --------------------------------------------------------------------------- #
def _two_question_slice() -> Slice:
    emb = np.array([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2]], dtype=np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    q = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    return Slice(
        corpus_emb=emb,
        query_emb=q,
        paragraphs=[{"id": i, "title": f"p{i}", "text": ""} for i in range(3)],
        questions=[
            {
                "qid": 0,
                "question": "?",
                "answer": "a",
                "type": "bridge",
                "gold_ids": [0],
            },
            {
                "qid": 1,
                "question": "?",
                "answer": "a",
                "type": "bridge",
                "gold_ids": [0],
            },
        ],
        out_adj={0: [1]},
        k=2,
    )


class _RaiseOnceReader:
    name = "raise-once"

    def __init__(self):
        self.calls = 0

    def answer(self, question, contexts):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated reader timeout")
        return "a"  # matches both questions' gold answer -> EM 1.0


def test_reader_failure_tallied_and_excluded_from_denominator():
    sl = _two_question_slice()
    rd = reader_scores(sl, _RaiseOnceReader(), k=2, only=["vector_only"])
    # one of the two reader calls failed:
    assert rd["reader_failures"] == 1
    # denominator shrank to the 1 successful question, which scored EM 1.0.
    # (had the failure been scored 0 instead of excluded, the mean would be 0.5.)
    assert rd["vector_only"]["answer_em"] == 1.0
    assert rd["n_reader_questions"] == 2


# --------------------------------------------------------------------------- #
# Phase 3 — manifest + SQL drop-in contract (reuses bench_corpus.build_sql)
# --------------------------------------------------------------------------- #
def test_manifest_and_bench_sql_dropin():
    g = build_graph(_questions())
    n = len(g.paragraphs)
    corpus_emb = np.zeros((n, 768), dtype=np.float32)
    corpus_emb[:, 0] = np.arange(n)  # distinct rows so argmax seed is deterministic
    query_emb = np.zeros((1, 768), dtype=np.float32)
    query_emb[0, 0] = 1.0
    m = build_manifest(
        g,
        k=5,
        embed_model="test",
        corpus_emb_path="x.npy",
        query_emb_path="y.npy",
        source_slice="test",
    )
    # real_corpus-compatible public fields
    for field in ("entities", "dim", "edges", "k", "_edges", "questions"):
        assert field in m
    assert m["dim"] == 768 and m["entities"] == n
    sql = emit_bench_sql(m, corpus_emb, query_emb, k=5)
    assert "CREATE EXTENSION graph_store;" in sql
    assert "SELECT graph_store.add_edge(" in sql
    assert "tjs(" in sql

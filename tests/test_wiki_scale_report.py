"""End-to-end smoke for the full-wiki HotpotQA harness — no network, no fastembed.

Synthesizes a tiny wiki_extract-shaped manifest + a HotpotQA slice whose gold titles
resolve into it, feeds PRECOMPUTED embeddings (so the slow BGE encode is not needed),
and drives build_slice -> sweep -> emit_engine_sql. Proves the harness reads the
manifest, resolves gold into the real-wiki id space, grades over the WHOLE corpus, and
emits the GX10-gated tjs_open SQL. The retrievers/grading themselves are covered by
tests/test_graphrag_report; this test covers the full-wiki wiring around them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wiki_scale_report import (  # noqa: E402
    build_slice,
    emit_engine_sql,
    sweep,
)


def _write_wiki_manifest(d: Path) -> None:
    """A 3-article corpus: ids 0,1,2; edge 0->1; no redirects/categories."""
    d.mkdir(parents=True, exist_ok=True)
    articles = [
        {
            "id": 0,
            "title": "Scott Derrickson",
            "text": "American film director.",
            "ts": "",
        },
        {"id": 1, "title": "Ed Wood", "text": "A 1994 biographical film.", "ts": ""},
        {
            "id": 2,
            "title": "Distractor Article",
            "text": "Unrelated filler text.",
            "ts": "",
        },
    ]
    (d / "articles-00000.jsonl").write_text(
        "\n".join(json.dumps(a, ensure_ascii=False) for a in articles) + "\n"
    )
    (d / "edges-00000.tsv").write_text("0\t1\n")  # Scott Derrickson -> Ed Wood
    (d / "categories-00000.tsv").write_text("")
    (d / "redirects.tsv").write_text("")
    manifest = {
        "source": "mediawiki-pages-articles",
        "counts": {"articles": 3, "edges": 1, "categories": 0, "redirects": 0},
        "shards": {
            "articles": {
                "schema": "jsonl",
                "files": [{"path": "articles-00000.jsonl", "rows": 3}],
            },
            "edges": {
                "schema": "tsv",
                "files": [{"path": "edges-00000.tsv", "rows": 1}],
            },
            "categories": {
                "schema": "tsv",
                "files": [{"path": "categories-00000.tsv", "rows": 0}],
            },
            "redirects": {
                "schema": "tsv",
                "files": [{"path": "redirects.tsv", "rows": 0}],
            },
        },
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _write_hotpot_slice(p: Path) -> None:
    questions = [
        {  # both gold titles present -> fully resolved
            "id": "q_full",
            "question": "Were Scott Derrickson and Ed Wood of the same nationality?",
            "answer": "yes",
            "type": "bridge",
            "level": "hard",
            "supporting_facts": [["Scott Derrickson", 0], ["Ed Wood", 0]],
        },
        {  # a gold title absent from the corpus -> NOT fully resolved (skipped)
            "id": "q_partial",
            "question": "?",
            "answer": "no",
            "type": "comparison",
            "level": "easy",
            "supporting_facts": [["Ed Wood", 0], ["Missing Person", 0]],
        },
    ]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"questions": questions}, ensure_ascii=False))


def _write_embeddings(dirp: Path) -> tuple[Path, Path]:
    # 4-d normalized vectors; query nearest to article 0 (the vector seed), article 1
    # reachable only via the 0->1 edge (the bridge graph_inject injects).
    corpus = np.array(
        [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.8, 0.6, 0.0, 0.0]],
        dtype=np.float32,
    )
    corpus /= np.linalg.norm(corpus, axis=1, keepdims=True)
    # query_emb has one row PER SLICE QUESTION (original order), per the harness contract.
    query = np.array([[1.0, 0.05, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
    query /= np.linalg.norm(query, axis=1, keepdims=True)
    cp, qp = dirp / "corpus.npy", dirp / "query.npy"
    np.save(cp, corpus)
    np.save(qp, query)
    return cp, qp


def test_wiki_scale_end_to_end(tmp_path):
    wiki = tmp_path / "wiki"
    _write_wiki_manifest(wiki)
    slice_path = tmp_path / "dev_slice.json"
    _write_hotpot_slice(slice_path)
    cp, qp = _write_embeddings(tmp_path)

    sl, cov = build_slice(wiki, slice_path, k=3, corpus_emb_path=cp, query_emb_path=qp)

    # corpus + coverage
    assert sl.n == 3
    assert cov["n_questions"] == 2
    assert cov["n_fully_resolved"] == 1  # only q_full has both gold in the corpus
    # gradeable subset: the fully-resolved question, gold resolved to wiki ids {0,1}
    assert len(sl.questions) == 1
    q = sl.questions[0]
    assert sorted(q["gold_ids"]) == [0, 1]
    assert q["qid"] == 0
    # graph leg loaded (0 -> 1)
    assert sl.out_adj.get(0) == [1]

    # grading runs and returns well-formed recall in [0,1]
    sw = sweep(sl, [2, 3])
    for name in ("vector_only", "graph_inject"):
        for k in (2, 3):
            j = sw[name][k]["all"]["joint"]
            r = sw[name][k]["all"]["recall"]
            assert 0.0 <= j <= 1.0 and 0.0 <= r <= 1.0
    # graph_inject reaches the bridge (article 1) by k=3, so joint recall is perfect;
    # this is exactly the multi-hop mechanism the full-wiki test targets.
    assert sw["graph_inject"][3]["all"]["joint"] == 1.0

    # GX10-gated SQL emission produces the operator call for the one gradeable query
    out_sql = tmp_path / "tjsopen.sql"
    emit_engine_sql(sl, out_sql, k=3, seeds=5, hops=2, term_cond=0)
    sql = out_sql.read_text()
    assert "tjs_open(" in sql
    assert "CREATE EXTENSION graph_store_am;" in sql
    assert sql.count("IDS_BEGIN") == 1  # one gradeable question

"""Unit tests for the benchmark harness: corpus loading, the stub driver, the
in-process baseline model, end-to-end SM derivation, and engine-gating.

Uses a tiny hand-built corpus (deterministic) so the canonical-query semantics
and the SM thresholds are checkable by hand. Also drives the real seed generator
once to confirm the harness reads the on-disk seed format.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.driver import (  # noqa: E402
    Corpus,
    LiveDriver,
    StubDriver,
    make_driver,
)
from bench.harness import (  # noqa: E402
    baseline_query_inprocess,
    baseline_query_live,
    load_corpus,
    run_benchmark,
)


def _toy_corpus() -> Corpus:
    # 5 entities. q_emb is closest to entity 0, then 1, 2, 3, 4 by construction
    # (1-D embeddings make distance ordering obvious).
    entities = {
        0: {"timestamp": 100, "chunk": "c0", "embedding": [0.0]},
        1: {"timestamp": 100, "chunk": "c1", "embedding": [1.0]},
        2: {"timestamp": 100, "chunk": "c2", "embedding": [2.0]},
        3: {"timestamp": 999, "chunk": "c3", "embedding": [3.0]},  # out of time range
        4: {"timestamp": 100, "chunk": "c4", "embedding": [4.0]},
    }
    # src 0 -> dst 1, dst 3(filtered out); src 1 -> dst 2; src 2 -> dst 4
    edges = [(0, 1), (0, 3), (1, 2), (2, 4)]
    queries = [
        {"qid": 0, "embedding": [0.0], "selected_time_range": list(range(90, 110))},
    ]
    return Corpus(entities=entities, edges=edges, queries=queries)


# --------------------------------------------------------------------------- #
# Stub driver: canonical-query semantics + bounded cost
# --------------------------------------------------------------------------- #


def test_stub_driver_canonical_semantics():
    corpus = _toy_corpus()
    s = StubDriver().run_query(corpus.queries[0], k=2, corpus=corpus)
    # Sources ranked by distance to q_emb=[0]: 0,1,2,3,4.
    # src 0 -> dst 1 (ts in range) qualifies; dst 3 filtered out.
    # next src 1 -> dst 2 qualifies. k=2 reached -> stop.
    assert s.result_chunks == ["c1", "c2"]
    assert s.system == "tridb"
    assert s.txn_atomic is True
    # Early termination: did NOT walk all 5 sources.
    assert s.corpus_examined < corpus.size
    assert s.corpus_size == corpus.size


def test_stub_driver_respects_time_filter():
    corpus = _toy_corpus()
    # Time range that excludes ts=100 entirely -> only entity 3 (ts 999) could
    # qualify but no narrow range here; pick one matching only ts 999.
    q = {"qid": 9, "embedding": [3.0], "selected_time_range": [999]}
    s = StubDriver().run_query(q, k=5, corpus=corpus)
    # Only dst with ts==999 is entity 3, reachable from src 0.
    assert s.result_chunks == ["c3"]


def test_stub_driver_corpus_fraction_under_budget():
    corpus = _toy_corpus()
    s = StubDriver().run_query(corpus.queries[0], k=2, corpus=corpus)
    assert s.corpus_fraction() < 0.25 or s.corpus_examined <= 2


# --------------------------------------------------------------------------- #
# In-process baseline: same answer set, larger intermediates
# --------------------------------------------------------------------------- #


def test_baseline_matches_stub_answer_set():
    corpus = _toy_corpus()
    tri = StubDriver().run_query(corpus.queries[0], k=2, corpus=corpus)
    base = baseline_query_inprocess(corpus.queries[0], k=2, corpus=corpus)
    assert set(tri.result_chunks) == set(base.result_chunks)
    assert base.system == "baseline"


def test_baseline_materializes_more_than_tridb():
    corpus = _toy_corpus()
    tri = StubDriver().run_query(corpus.queries[0], k=2, corpus=corpus)
    base = baseline_query_inprocess(corpus.queries[0], k=2, corpus=corpus)
    # The baseline examines the whole corpus on the ANN leg; TriDB does not.
    assert base.corpus_examined == corpus.size
    assert tri.corpus_examined < base.corpus_examined


def test_baseline_live_is_gated():
    corpus = _toy_corpus()
    with pytest.raises(NotImplementedError):
        baseline_query_live(corpus.queries[0], k=2, corpus=corpus)


# --------------------------------------------------------------------------- #
# Driver factory + live gating
# --------------------------------------------------------------------------- #


def test_make_driver_stub():
    assert isinstance(make_driver("stub"), StubDriver)


def test_make_driver_live_is_gated_on_run():
    d = make_driver("live")
    assert isinstance(d, LiveDriver)
    corpus = _toy_corpus()
    with pytest.raises(NotImplementedError):
        d.run_query(corpus.queries[0], k=2, corpus=corpus)


def test_make_driver_rejects_unknown_mode():
    with pytest.raises(ValueError):
        make_driver("bogus")


# --------------------------------------------------------------------------- #
# End-to-end benchmark
# --------------------------------------------------------------------------- #


def test_run_benchmark_end_to_end():
    corpus = _toy_corpus()
    report = run_benchmark(corpus, k=2, driver=StubDriver())
    assert report.engine_mode == "stub"
    assert report.num_queries == 1
    assert len(report.metrics) == 5
    # SM-4 parity must be perfect: stub and baseline agree on the answer set.
    sm4 = next(m for m in report.metrics if m.sm == "SM-4")
    assert sm4.passed
    # SM-5 atomicity always holds for the stub.
    sm5 = next(m for m in report.metrics if m.sm == "SM-5")
    assert sm5.passed


# --------------------------------------------------------------------------- #
# Corpus loading from the real seed format
# --------------------------------------------------------------------------- #


def test_load_corpus_reads_seed_format(tmp_path):
    seed = tmp_path / "seed"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "seed_corpus.py"),
            "--entities",
            "40",
            "--dim",
            "8",
            "--edges-per-node",
            "4",
            "--seed",
            "42",
            "--out",
            str(seed),
        ],
        check=True,
        capture_output=True,
    )
    corpus = load_corpus(seed)
    assert corpus.size == 40
    assert len(corpus.queries) == 10
    assert len(corpus.entities[0]["embedding"]) == 8
    assert corpus.edges  # non-empty

    # Harness end-to-end on a real seed corpus: every SM is derivable + parity holds.
    report = run_benchmark(corpus, k=5, driver=StubDriver())
    assert len(report.metrics) == 5
    sm4 = next(m for m in report.metrics if m.sm == "SM-4")
    assert sm4.passed  # stub answer set == baseline answer set on the seed corpus

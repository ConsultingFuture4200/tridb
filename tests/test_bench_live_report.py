"""Unit tests for bench/live_report.py — the parser that turns a LIVE TriDB run's
#BENCH output + corpus manifest into the bench JSON schema.

These run anywhere (no engine, no Docker): they feed a hand-written #BENCH
transcript and a tiny manifest through the parser/derivation and check the SM
wiring. The LIVE engine numbers themselves are produced by scripts/bench_live.sh
against tridb/msvbase:dev; here we only verify the off-engine glue (parse,
corpus rebuild, baseline model, SM derivation, SM-2 honesty override).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from bench.live_report import (  # noqa: E402
    baseline_query_canonical,
    build_report,
    parse_bench_output,
    rebuild_corpus,
)


def _manifest(tmp_seed=7) -> dict:
    # tiny deterministic corpus: 6 entities, dim 2, one hub (0) -> {1,2,3,4}.
    return {
        "entities": 6,
        "dim": 2,
        "hubs": 1,
        "fanout": 4,
        "num_queries": 1,
        "k": 3,
        "edges": 4,
        "seed": tmp_seed,
        "time_min": 100,
        "time_max": 200,
        "window": 101,
        "queries": [
            {
                "qid": 0,
                "src": 0,
                "embedding": [1.0, 0.0],
                "window": list(range(100, 201)),
            }
        ],
        "hub_dsts": {"0": [1, 2, 3, 4]},
    }


def test_parse_bench_output_basic():
    text = (
        "#BENCH QSTART qid=0 src=0 k=3\n"
        " #BENCH TRIDB_RESULT qid=0 ids=2,4,1\n"
        " #BENCH TRIDB_EXAMINED qid=0 examined=11\n"
        "#BENCH EXPLAIN_BEGIN qid=0\n"
        " Function Scan on tjs t  (cost=0.00..10.00 rows=1000 width=8)"
        " (actual time=1.0..1.0 rows=3 loops=1)\n"
        " Execution Time: 2.345 ms\n"
        "#BENCH EXPLAIN_END qid=0\n"
        " #BENCH ORACLE qid=0 ids=2,4,1\n"
        " #BENCH ORACLE_COUNTS qid=0 reached=4 filtered=3\n"
        "#BENCH QEND qid=0\n"
        "#BENCH DONE\n"
    )
    obs = parse_bench_output(text)
    assert obs[0]["tridb_ids"] == [2, 4, 1]
    assert obs[0]["examined"] == 11
    assert obs[0]["oracle_ids"] == [2, 4, 1]
    assert obs[0]["reached"] == 4
    assert obs[0]["filtered"] == 3
    assert obs[0]["exec_ms"] == pytest.approx(2.345)


def test_parse_empty_result_ids():
    text = "#BENCH TRIDB_RESULT qid=0 ids=\n#BENCH TRIDB_EXAMINED qid=0 examined=0\n"
    obs = parse_bench_output(text)
    assert obs[0]["tridb_ids"] == []


def test_rebuild_corpus_deterministic():
    m = _manifest()
    c1 = rebuild_corpus(m, m["seed"])
    c2 = rebuild_corpus(m, m["seed"])
    assert c1.size == 6
    # same seed -> identical embeddings/timestamps
    assert c1.entities[0]["embedding"] == c2.entities[0]["embedding"]
    assert c1.entities[3]["timestamp"] == c2.entities[3]["timestamp"]
    # edges come from the manifest hub_dsts
    assert sorted(c1.edges) == [(0, 1), (0, 2), (0, 3), (0, 4)]


def test_baseline_canonical_peak_and_pin():
    m = _manifest()
    corpus = rebuild_corpus(m, m["seed"])
    q = m["queries"][0]
    s = baseline_query_canonical(q, m["k"], corpus, src=0)
    # baseline examines the whole corpus (un-pushed ANN) and holds a peak >= k.
    assert s.corpus_examined == corpus.size
    assert s.peak_intermediate_rows >= m["k"]
    # only dst reachable from src 0 can appear in the result.
    reachable_chunks = {f"chunk {d}" for d in (1, 2, 3, 4)}
    assert set(s.result_chunks).issubset(reachable_chunks)


def test_build_report_live_smoke():
    m = _manifest()
    # craft a transcript whose TriDB result EQUALS the exact oracle (parity 100%).
    # use the rebuilt corpus to compute the true top-k so the test is self-consistent.
    corpus = rebuild_corpus(m, m["seed"])
    q = m["queries"][0]
    # exact: reachable-from-0, all in-window, ordered by L2 to [1,0], top-3
    from bench.driver import _l2_sq

    order = sorted(
        (1, 2, 3, 4),
        key=lambda d: _l2_sq(corpus.entities[d]["embedding"], q["embedding"]),
    )
    top = order[: m["k"]]
    ids = ",".join(str(x) for x in top)
    text = (
        f"#BENCH QSTART qid=0 src=0 k=3\n"
        f"#BENCH TRIDB_RESULT qid=0 ids={ids}\n"
        f"#BENCH TRIDB_EXAMINED qid=0 examined=5\n"
        f"#BENCH EXPLAIN_BEGIN qid=0\n Execution Time: 0.5 ms\n#BENCH EXPLAIN_END qid=0\n"
        f"#BENCH ORACLE qid=0 ids={ids}\n"
        f"#BENCH ORACLE_COUNTS qid=0 reached=4 filtered=4\n"
        f"#BENCH QEND qid=0\n#BENCH DONE\n"
    )
    report = build_report(text, m, m["seed"])
    assert report.engine_mode == "live"
    by = {x.sm: x for x in report.metrics}
    # parity exact -> SM-4 passes at 100%
    assert by["SM-4"].passed
    assert by["SM-4"].value == pytest.approx(1.0)
    # SM-3: examined 5 / 6 ... that is > 25%, so SM-3 may fail on this tiny toy;
    # we only assert it carries the LIVE examined count surface, not a verdict.
    assert report.tridb_samples[0].corpus_examined == 5
    # SM-1: baseline peak (>= k) vs TriDB peak (k) -> reduction reported
    assert by["SM-1"].value >= 1.0
    # SM-2 honesty override: passes (not a fail), unit marks TriDB-side only
    assert by["SM-2"].passed
    assert "TriDB-side" in by["SM-2"].unit
    # SM-5 atomic
    assert by["SM-5"].passed


def test_build_report_incomplete_raises():
    m = _manifest()
    with pytest.raises(SystemExit):
        build_report("#BENCH TRIDB_RESULT qid=0 ids=1\n", m, m["seed"])  # no DONE

"""Unit tests for bench/sm2_compare.py — the SM-2 head-to-head comparison.

Pure parsing + arithmetic; no live systems, no engine. Guards the SM-2 fraction,
the latency-ratio math, and the answer-parity computation.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import sm2_compare  # noqa: E402


TRIDB_TRANSCRIPT = """\
#SM2 QSTART qid=0 src=3 k=5
Time: 1.200 ms
Time: 1.000 ms
Time: 1.100 ms
#SM2 RESULT qid=0 ids=10,11,12
#SM2 QEND qid=0
#SM2 QSTART qid=1 src=7 k=5
Time: 5.000 ms
Time: 5.500 ms
Time: 5.200 ms
#SM2 RESULT qid=1 ids=20,21
#SM2 QEND qid=1
#SM2 DONE
"""


def test_parse_tridb_groups_times_by_qid():
    obs = sm2_compare.parse_tridb(TRIDB_TRANSCRIPT)
    assert obs[0]["samples_ms"] == [1.2, 1.0, 1.1]
    assert obs[0]["result_ids"] == [10, 11, 12]
    assert obs[1]["samples_ms"] == [5.0, 5.5, 5.2]
    assert obs[1]["result_ids"] == [20, 21]
    assert obs[0]["src"] == 3 and obs[1]["src"] == 7


def _baseline(q0_ms, q1_ms, q0_ids, q1_ids):
    return {
        "runs": 3,
        "queries": [
            {
                "qid": 0,
                "latency_total_ms": q0_ms,
                "latency_samples_ms": [q0_ms],
                "result_ids": q0_ids,
                "graph_reached_dst": 100,
                "vector_candidates": 160,
                "relational_filtered": 30,
                "merged_candidates": 12,
                "final_results": len(q0_ids),
            },
            {
                "qid": 1,
                "latency_total_ms": q1_ms,
                "latency_samples_ms": [q1_ms],
                "result_ids": q1_ids,
                "graph_reached_dst": 90,
                "vector_candidates": 160,
                "relational_filtered": 20,
                "merged_candidates": 8,
                "final_results": len(q1_ids),
            },
        ],
    }


def _manifest():
    return {
        "k": 5,
        "seed": 42,
        "entities": 2000,
        "edges": 1799,
        "queries": [{"qid": 0, "src": 3}, {"qid": 1, "src": 7}],
    }


def test_sm2_fraction_and_ratios():
    obs = sm2_compare.parse_tridb(TRIDB_TRANSCRIPT)
    # TriDB medians: q0=1.1ms, q1=5.2ms. Baseline slower on both -> SM-2 = 1.0
    baseline = _baseline(3.0, 10.4, [10, 11, 12], [20, 21])
    res = sm2_compare.compare(obs, baseline, _manifest())
    assert res["num_queries"] == 2
    assert res["tridb_wins"] == 2
    assert res["sm2_fraction"] == 1.0
    assert res["sm2_passed"] is True
    # ratio q0 = 3.0/1.1 ≈ 2.727 ; q1 = 10.4/5.2 = 2.0 ; median ≈ 2.36
    assert res["median_ratio_baseline_over_tridb"] > 2.0
    # exact answer parity both queries
    assert res["answer_parity_exact_set"] == "2/2"
    assert res["answer_mean_jaccard"] == 1.0


def test_sm2_fraction_partial_and_parity_divergence():
    obs = sm2_compare.parse_tridb(TRIDB_TRANSCRIPT)
    # baseline FASTER on q1 (TriDB loses q1) -> SM-2 = 0.5
    # q1 answer diverges (baseline returns 20,99) -> Jaccard < 1
    baseline = _baseline(3.0, 2.0, [10, 11, 12], [20, 99])
    res = sm2_compare.compare(obs, baseline, _manifest())
    assert res["tridb_wins"] == 1
    assert res["sm2_fraction"] == 0.5
    assert res["sm2_passed"] is False
    assert res["answer_parity_exact_set"] == "1/2"
    # q1 jaccard: {20,21} vs {20,99} -> 1/3 (rounded to 4 dp in the result)
    q1 = next(q for q in res["queries"] if q["qid"] == 1)
    assert abs(q1["answer_jaccard"] - (1 / 3)) < 1e-3
    assert q1["answer_exact_set_match"] is False


def test_render_md_contains_headline_and_tables():
    obs = sm2_compare.parse_tridb(TRIDB_TRANSCRIPT)
    baseline = _baseline(3.0, 10.4, [10, 11, 12], [20, 21])
    res = sm2_compare.compare(obs, baseline, _manifest())
    md = sm2_compare.render_md(res)
    assert "SM-2" in md
    assert "Per-query latency" in md
    assert "intermediate-result sizes" in md

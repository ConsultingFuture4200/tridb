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
        "term_cond": 10000,
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
    # plan 012: the operating point is recorded in the result (and the JSON it serializes to)
    assert res["term_cond"] == 10000


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
    # plan 012: the markdown names the operating point so latency/recall claims are tied to it
    assert "term_cond=10000" in md


def test_pct_interpolation_and_tail_guard():
    # p50 always available; single sample returns itself.
    assert sm2_compare._pct([5.0], 50.0) == 5.0
    # linear interpolation on a known set (0..100 step 10, 11 samples >= tail min).
    s = [float(x) for x in range(0, 101, 10)]  # 11 samples
    assert sm2_compare._pct(s, 50.0) == 50.0
    # p95/p99 require >= _MIN_SAMPLES_FOR_TAIL samples; below that -> None.
    assert sm2_compare._pct([1.0, 2.0, 3.0], 95.0) is None
    assert sm2_compare._pct([1.0, 2.0, 3.0], 50.0) == 2.0
    big = [float(x) for x in range(100)]  # 100 samples
    assert sm2_compare._pct(big, 95.0) is not None
    assert sm2_compare._pct([], 50.0) is None


def test_compare_emits_percentiles_and_qps_null_guarded_for_small_n():
    # 3 samples/side -> p50 present, p95/p99 null (below the tail threshold).
    manifest = {"k": 5, "seed": 42, "queries": [{"qid": 0, "src": 3}]}
    tridb_obs = {
        0: {"src": 3, "k": 5, "samples_ms": [1.2, 1.0, 1.1], "result_ids": [10]}
    }
    baseline = {
        "runs": 3,
        "queries": [
            {
                "qid": 0,
                "latency_total_ms": 5.0,
                "latency_samples_ms": [5.0, 5.5, 5.2],
                "result_ids": [10],
            }
        ],
    }
    res = sm2_compare.compare(tridb_obs, baseline, manifest)
    pq = res["queries"][0]
    assert pq["tridb_p95_ms"] is None and pq["baseline_p95_ms"] is None
    assert res["p95_ratio_baseline_over_tridb"] is None
    assert res["qps_singleclient_tridb"] is not None  # median-based, always available
    assert res["tail_latency_note"] is not None  # explains the nulls

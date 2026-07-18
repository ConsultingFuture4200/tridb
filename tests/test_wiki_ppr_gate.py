"""Host tests for the wiki-scale PPR gate (advisor plan 096) -- no docker, no network.

Covers every pure piece the plan requires unit coverage for: slice-edge filtering
(shard math + dedup + self-loop drop), deterministic query/gold sampling (proven by
running the sampler twice), held-out-edge exclusion in BOTH directions (including the
case where a genuine reverse hyperlink coincides with a held-out pair), the recall
reducer (query-id self-exclusion + evidence_scores), and the transcript parser/grader.

A tiny hand-checkable synthetic graph anchors the sampling/exclusion tests: node 0 has
exactly 8 distinct out-links (1..8), a duplicate line and a self-loop that must be
dropped, and out-of-slice ids that must be filtered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wiki_ppr_gate import (  # noqa: E402
    adjacency_from_edges,
    build_load_edges,
    edge_shard_paths,
    exclude_holdouts,
    grade,
    grade_point,
    load_slice_adjacency,
    mean_gold_reachable_fraction,
    parse_edge_lines,
    parse_log,
    reachable_within_hops,
    sample_queries_and_holdouts,
)


# --------------------------------------------------------------------------- #
# fixture: node 0 -> {1..8} distinct (dup line + self-loop dropped), node 1 -> {0, 2},
# out-of-slice src (9) and dst (20) dropped for n=9.
# --------------------------------------------------------------------------- #
FIXTURE_TSV = (
    "0\t1\n0\t2\n0\t3\n0\t4\n0\t5\n0\t6\n0\t7\n0\t8\n"
    "0\t1\n"  # duplicate -> deduped
    "0\t0\n"  # self-loop -> dropped
    "1\t0\n1\t2\n"
    "9\t0\n"  # src out of slice (n=9) -> dropped
    "2\t20\n"  # dst out of slice -> dropped
)


def _fixture_paths(tmp_path: Path) -> list[Path]:
    p = tmp_path / "edges-00000.tsv"
    p.write_text(FIXTURE_TSV)
    return [p]


# --------------------------------------------------------------------------- #
# edge filtering
# --------------------------------------------------------------------------- #
def test_parse_edge_lines_dedups_drops_selfloop_and_out_of_slice(tmp_path):
    adj = parse_edge_lines(_fixture_paths(tmp_path), n=9)
    assert adj == {0: [1, 2, 3, 4, 5, 6, 7, 8], 1: [0, 2]}


def test_edge_shard_paths_shard_index_math(tmp_path):
    manifest = {
        "shard_size": 100,
        "shards": {
            "articles": {"files": [{"path": "articles-00000.jsonl", "rows": 100}]},
            "edges": {
                "files": [
                    {"path": "edges-00000.tsv"},
                    {"path": "edges-00001.tsv"},
                    {"path": "edges-00002.tsv"},
                ]
            },
        },
    }
    # n=150 spans shard 0 and shard 1 (ids 100-199) but not shard 2.
    paths = edge_shard_paths(manifest, tmp_path, n=150)
    assert [p.name for p in paths] == ["edges-00000.tsv", "edges-00001.tsv"]
    # n=50 stays within shard 0 only.
    paths = edge_shard_paths(manifest, tmp_path, n=50)
    assert [p.name for p in paths] == ["edges-00000.tsv"]


def test_load_slice_adjacency_reads_manifest(tmp_path):
    (tmp_path / "edges-00000.tsv").write_text(FIXTURE_TSV)
    manifest = {
        "shard_size": 100,
        "shards": {
            "articles": {"files": [{"path": "articles-00000.jsonl", "rows": 100}]},
            "edges": {"files": [{"path": "edges-00000.tsv"}]},
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    adj = load_slice_adjacency(tmp_path, n=9)
    assert adj == {0: [1, 2, 3, 4, 5, 6, 7, 8], 1: [0, 2]}


# --------------------------------------------------------------------------- #
# deterministic sampling (hand-verified against `random.Random(1)` directly)
# --------------------------------------------------------------------------- #
_ADJ = {0: [1, 2, 3, 4, 5, 6, 7, 8], 1: [0, 2]}


def test_sample_queries_and_holdouts_deterministic_and_hand_checked():
    a = sample_queries_and_holdouts(_ADJ, q=1, seed=1, hold_out=3, min_outdegree=8)
    b = sample_queries_and_holdouts(_ADJ, q=1, seed=1, hold_out=3, min_outdegree=8)
    assert a == b  # same seed, same output, run twice
    # hand-verified: random.Random(1).sample([0], 1) == [0]; continuing the same RNG
    # stream, .sample([1,2,3,4,5,6,7,8], 3) == [2, 1, 3] -> sorted [1, 2, 3].
    assert a == [{"qid": 0, "gold": [1, 2, 3]}]


def test_sample_queries_and_holdouts_rejects_low_outdegree_candidates():
    # node 1 has only 2 distinct out-links; min_outdegree=8 excludes it, leaving only
    # node 0 as a candidate -- asking for q=2 must fail loudly, not silently pad.
    try:
        sample_queries_and_holdouts(_ADJ, q=2, seed=1, hold_out=3, min_outdegree=8)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "candidates" in str(e)


# --------------------------------------------------------------------------- #
# load-graph construction + held-out exclusion (both directions, including the case
# where a genuine reverse hyperlink coincides with a held-out pair).
# --------------------------------------------------------------------------- #
def test_build_load_edges_symmetrizes():
    edges = build_load_edges({0: [1, 2]})
    assert edges == {(0, 1), (1, 0), (0, 2), (2, 0)}


def test_exclude_holdouts_removes_both_directions_including_genuine_reverse():
    # node 1 has a REAL hyperlink back to node 0 -- its symmetrized copy is the same
    # directed pair (1,0) that holding out (qid=0, gold=1) must remove.
    adj = {0: [1, 2], 1: [0, 3]}
    edges = build_load_edges(adj)
    assert edges == {(0, 1), (1, 0), (0, 2), (2, 0), (1, 3), (3, 1)}
    kept = exclude_holdouts(edges, [{"qid": 0, "gold": [1]}])
    assert kept == {(0, 2), (2, 0), (1, 3), (3, 1)}


def test_adjacency_from_edges():
    assert adjacency_from_edges({(0, 2), (2, 0), (1, 3), (3, 1)}) == {
        0: [2],
        2: [0],
        1: [3],
        3: [1],
    }


# --------------------------------------------------------------------------- #
# reachability diagnostic (mode-independent context metric)
# --------------------------------------------------------------------------- #
_CHAIN = {0: [1, 2], 1: [3], 2: [4]}


def test_reachable_within_hops():
    assert reachable_within_hops(_CHAIN, 0, hops=1) == {1, 2}
    assert reachable_within_hops(_CHAIN, 0, hops=2) == {1, 2, 3, 4}


def test_mean_gold_reachable_fraction():
    samples = [{"qid": 0, "gold": [1, 4]}]
    assert mean_gold_reachable_fraction(_CHAIN, samples, hops=1) == 0.5
    assert mean_gold_reachable_fraction(_CHAIN, samples, hops=2) == 1.0


# --------------------------------------------------------------------------- #
# grading (query-id self-exclusion + the shared evidence_scores recall reducer)
# --------------------------------------------------------------------------- #
def test_grade_point_drops_query_id_before_scoring():
    sc = grade_point([0, 1, 2], qid=0, gold=[1, 2])
    assert sc["recall"] == 1.0  # both gold ids present once id 0 is dropped
    sc_miss = grade_point([0, 9], qid=0, gold=[1, 2])
    assert sc_miss["recall"] == 0.0


def test_grade_point_noop_when_query_id_absent():
    sc = grade_point([1, 2, 3], qid=0, gold=[1, 2])
    assert sc["recall"] == 1.0


# --------------------------------------------------------------------------- #
# transcript parse + grade
# --------------------------------------------------------------------------- #
def test_parse_log_and_grade(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text(
        "\\timing on\n"
        "#R mode=membership k=10 tc=8 bud=2048 qid=0 ids=1,2,3\n"
        "Time: 12.500 ms\n"
        "\\timing off\n"
        "#C mode=membership k=10 tc=8 bud=2048 qid=0 examined=5 censored=false term=term_cond\n"
        "\\timing on\n"
        "#R mode=ppr k=10 tc=8 bud=2048 qid=0 ids=1,9\n"
        "Time: 20.000 ms\n"
        "\\timing off\n"
        "#C mode=ppr k=10 tc=8 bud=2048 qid=0 examined=7 censored=true term=stream_end_unknown\n"
        "#WPG PROBE fwd_absent=true rev_absent=false\n"
    )
    points, probe = parse_log(log)
    # Postgres renders boolean::text as the full word ('true'/'false'), not 't'/'f' --
    # a mixed true/false fixture catches a single-char-vs-word regression either way.
    assert probe == {"fwd_absent": True, "rev_absent": False}
    assert points[("membership", 10, 8, 2048, 0)]["latency_ms"] == 12.5
    assert points[("ppr", 10, 8, 2048, 0)]["term"] == "stream_end_unknown"

    meta = {
        "modes": ["membership", "ppr"],
        "ks": [10],
        "term_conds": [8],
        "budgets": [2048],
        "m_seeds": 8,
        "hops": 2,
        "n": 9,
        "q": 1,
        "gold_reachable_within_hops_mean": 1.0,
        "n_loaded_edges": 4,
        "samples": [{"qid": 0, "gold": [1, 2]}],
    }
    res = grade(meta, points)
    rows = {(r["mode"]): r for r in res["rows"]}
    assert rows["membership"]["n"] == 1
    assert rows["membership"]["recall"] == 1.0  # {1,2,3} -> hits both gold ids
    assert rows["membership"]["censored_fraction"] == 0.0
    assert rows["membership"]["latency_ms_mean"] == 12.5
    assert rows["ppr"]["recall"] == 0.5  # {1,9} -> only 1 of 2 gold ids
    assert rows["ppr"]["censored_fraction"] == 1.0
    assert rows["ppr"]["stream_end_unknown_fraction"] == 1.0


def test_grade_reports_missing_points_as_dropped_not_padded(tmp_path, capsys):
    meta = {
        "modes": ["membership"],
        "ks": [10],
        "term_conds": [8],
        "budgets": [2048],
        "m_seeds": 8,
        "hops": 2,
        "n": 9,
        "q": 1,
        "gold_reachable_within_hops_mean": 1.0,
        "n_loaded_edges": 4,
        "samples": [{"qid": 0, "gold": [1, 2]}],
    }
    res = grade(meta, points={})
    row = res["rows"][0]
    assert row["n"] == 0
    assert row["recall"] != row["recall"]  # NaN, not a fabricated 0.0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err

"""Unit tests for the baseline harness's app-side merge + leg-shaping functions.

These are the SM-1 measurement surface (intermediate-set sizes + final top-k). The DB
clients are mocked — no live Neo4j/Milvus/Postgres, and the driver wheels need not be
installed (the harness imports them lazily inside each function).
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "baseline"))

import harness  # noqa: E402


def _metrics():
    return harness.QueryMetrics(qid=0, k=2)


# --------------------------------------------------------------------------- #
# merge — the baseline's defining cost
# --------------------------------------------------------------------------- #


def test_merge_happy_path_orders_by_distance():
    m = _metrics()
    pairs = [(1, 10), (2, 20)]
    vector_hits = [(1, 0.5), (2, 0.9)]  # src 1 closer than src 2
    kept_dst = {10: "chunk-10", 20: "chunk-20"}
    out = harness.merge(pairs, vector_hits, kept_dst, k=2, m=m)
    assert out == ["chunk-10", "chunk-20"]  # ascending distance
    assert m.merged_candidates == 2
    assert m.final_results == 2


def test_merge_empty_inputs():
    m = _metrics()
    assert harness.merge([], [], {}, k=5, m=m) == []
    assert m.merged_candidates == 0
    assert m.final_results == 0


def test_merge_requires_both_src_distance_and_surviving_dst():
    m = _metrics()
    # pair (1,10): src 1 has a distance AND dst 10 survived -> qualifies
    # pair (2,20): dst 20 NOT in kept_dst -> dropped
    # pair (3,30): src 3 NOT in vector_hits -> dropped
    pairs = [(1, 10), (2, 20), (3, 30)]
    vector_hits = [(1, 0.5), (2, 0.9)]
    kept_dst = {10: "chunk-10", 30: "chunk-30"}
    out = harness.merge(pairs, vector_hits, kept_dst, k=5, m=m)
    assert out == ["chunk-10"]
    assert m.merged_candidates == 1


def test_merge_dedups_dst_preserving_best_distance():
    m = _metrics()
    # same dst reached via two srcs; closest src wins ordering, dst emitted once
    pairs = [(1, 10), (2, 10)]
    vector_hits = [(1, 0.5), (2, 0.1)]
    kept_dst = {10: "chunk-10"}
    out = harness.merge(pairs, vector_hits, kept_dst, k=5, m=m)
    assert out == ["chunk-10"]
    assert m.merged_candidates == 2  # both pairs qualified before dedup
    assert m.final_results == 1


def test_merge_caps_at_k():
    m = _metrics()
    pairs = [(i, 100 + i) for i in range(10)]
    vector_hits = [(i, float(i)) for i in range(10)]
    kept_dst = {100 + i: f"chunk-{i}" for i in range(10)}
    out = harness.merge(pairs, vector_hits, kept_dst, k=3, m=m)
    assert len(out) == 3
    assert out == ["chunk-0", "chunk-1", "chunk-2"]  # smallest distances first
    assert m.final_results == 3


# --------------------------------------------------------------------------- #
# graph_expand (Neo4j) — mocked driver
# --------------------------------------------------------------------------- #


def test_graph_expand_shapes_pairs_and_metrics():
    m = _metrics()
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value = [
        {"src": 1, "dst": 2},
        {"src": 1, "dst": 3},
        {"src": 5, "dst": 2},
    ]
    pairs = harness.graph_expand(driver, [1, 5], m)
    assert pairs == [(1, 2), (1, 3), (5, 2)]
    assert m.graph_pairs == 3
    assert m.graph_distinct_src == 2
    assert m.graph_distinct_dst == 2


# --------------------------------------------------------------------------- #
# relational_filter (Postgres) — mocked cursor
# --------------------------------------------------------------------------- #


def test_relational_filter_keeps_matching_rows():
    m = _metrics()
    pg = MagicMock()
    cur = pg.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [(2, "chunk-2"), (3, "chunk-3")]
    kept = harness.relational_filter(pg, [2, 3, 4], [10, 11, 12], m)
    assert kept == {2: "chunk-2", 3: "chunk-3"}
    assert m.relational_filtered == 2


def test_relational_filter_short_circuits_on_empty_inputs():
    m = _metrics()
    pg = MagicMock()
    assert harness.relational_filter(pg, [], [1, 2], m) == {}
    assert harness.relational_filter(pg, [1, 2], [], m) == {}
    assert m.relational_filtered == 0
    pg.cursor.assert_not_called()  # no DB round-trip when nothing to filter


# --------------------------------------------------------------------------- #
# vector_topk (Milvus) — fake pymilvus module
# --------------------------------------------------------------------------- #


def test_vector_topk_shapes_hits_and_metrics():
    m = _metrics()
    conn = harness.Conn()
    hits_in = [SimpleNamespace(id=1, distance=0.5), SimpleNamespace(id=2, distance=0.9)]

    class FakeCollection:
        def __init__(self, name, using=None):
            self.name = name

        def search(self, **kwargs):
            return [hits_in]

    fake = types.ModuleType("pymilvus")
    fake.Collection = FakeCollection
    with patch.dict(sys.modules, {"pymilvus": fake}):
        hits = harness.vector_topk("alias", [0.1, 0.2, 0.3, 0.4], k=2, m=m, conn=conn)
    assert hits == [(1, 0.5), (2, 0.9)]
    assert m.vector_candidates == 2

"""Unit tests for tools/bench_corpus.py — the LIVE-benchmark SQL generator.

These check the generated SQL's structure and the manifest invariants WITHOUT an
engine (the live run itself is scripts/bench_live.sh against tridb/msvbase:dev).
Two regression guards matter most:

  * the corpus is built BEFORE the HNSW index (fork limitation), and
  * the oracle does NOT use `array_agg(... ORDER BY d2 ...)` — re-referencing the
    correlated-subquery d2 column inside an aggregate ORDER BY makes the MSVBASE
    fork return a WRONG ordering (a real bug found + fixed during DEV-1172).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("numpy")

from tools.bench_corpus import build  # noqa: E402


class _Args:
    entities = 60
    dim = 4
    hubs = 3
    fanout = 12
    queries = 4
    k = 5
    window = 50
    time_min = 19000
    time_max = 20000
    query_jitter = 0.3
    seed = 5


def test_manifest_invariants():
    sql, manifest = build(_Args())
    assert manifest["entities"] == 60
    assert manifest["num_queries"] == 4
    assert manifest["k"] == 5
    assert len(manifest["queries"]) == 4
    # each query pins a hub that exists in hub_dsts
    for q in manifest["queries"]:
        assert str(q["src"]) in manifest["hub_dsts"]
        assert len(q["embedding"]) == manifest["dim"]
        assert len(q["window"]) == manifest["window"]
    # the manifest carries everything rebuild_corpus needs
    for key in ("seed", "time_min", "time_max", "dim", "hub_dsts"):
        assert key in manifest


def test_corpus_built_before_index():
    sql, _ = build(_Args())
    insert_at = sql.index("INSERT INTO entities")
    index_at = sql.index("CREATE INDEX entities_hnsw")
    assert insert_at < index_at, (
        "rows must be inserted before the HNSW index (fork limit)"
    )


def test_two_phase_oracle_before_tjs():
    sql, _ = build(_Args())
    phase_a = sql.index("PHASE A")
    phase_b = sql.index("PHASE B")
    first_oracle = sql.index("#BENCH ORACLE qid=")
    first_tjs = sql.index("FROM tjs(")
    assert phase_a < phase_b
    # all oracle queries (PHASE A) precede the first tjs() call (PHASE B): the
    # oracle is a clean-backend seqscan, run before any tjs scan can corrupt it.
    assert first_oracle < first_tjs


def test_oracle_avoids_array_agg_orderby_d2_bug():
    sql, _ = build(_Args())
    # the buggy pattern must NOT appear anywhere in the generated oracle.
    assert "array_agg(id ORDER BY d2" not in sql
    # the safe pattern (rank via row_number, aggregate ordered by the integer rn).
    assert "row_number() OVER (ORDER BY d2" in sql
    assert "string_agg(id::text, ',' ORDER BY rn)" in sql


def test_canonical_arg_shape_in_tjs_calls():
    sql, manifest = build(_Args())
    # each query emits one tjs() call with the canonical arg order:
    #   tjs('entities', k, 0, <src>::bigint, 'id, chunk', 'ts IN (...)', 'embedding <-> ...')
    # one tjs() in the live result statement + one in the EXPLAIN ANALYZE, per query.
    assert sql.count("FROM tjs('entities',") == 2 * manifest["num_queries"]
    # canonical arg order: term_cond=0, then <src>::bigint, then attr/filter/orderby.
    assert f"tjs('entities', {manifest['k']}, 0, " in sql
    assert "::bigint, 'id, chunk'," in sql
    assert "embedding <->" in sql

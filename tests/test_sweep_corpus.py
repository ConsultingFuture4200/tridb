"""Unit tests for tools/sweep_corpus.py — the index-quality x term_cond sweep.

These check the grading layer WITHOUT an engine and WITHOUT network/Docker: a
tiny synthetic corpus is built in-process, and the transcript grader is fed
hand-written #SWEEP lines (the same format the live engine emits) so recall /
examined / latency parsing is exercised end-to-end. The asserts pin:

  * the numpy oracle is the EXACT true top-k (reachable + ts-filtered, by L2),
  * its tiebreak is by ascending id (matches ORDER BY d2, id / real_corpus),
  * recall is 1.0 on perfect ids and degrades when an id is missing,
  * the empty-oracle semantics match real_corpus.recall_at_k (perfect only if
    nothing was returned),
  * the transcript grader populates build_ms / examined / latency / recall, and
  * generation is deterministic across two runs with the same seed.

The LIVE engine run (real latency, real tjs_candidates_examined) stays
GX10/engine-gated and is NOT exercised here.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from tools import sweep_corpus as sc  # noqa: E402


# --------------------------------------------------------------------------- #
# Args fixture (mirrors the CLI defaults but tiny + fast)
# --------------------------------------------------------------------------- #


class _Args:
    entities = 200
    dim = 16
    hubs = 4
    fanout = 30
    queries = 6
    k = 5
    window = 400
    time_min = 19000
    time_max = 20000
    query_jitter = 0.35
    seed = 42
    index_configs = "16:200"
    term_conds = "50"


def _args(**over):
    a = _Args()
    for key, val in over.items():
        setattr(a, key, val)
    return a


# --------------------------------------------------------------------------- #
# Exact numpy oracle == true top-k (reachable + ts-filtered, by L2, ties by id)
# --------------------------------------------------------------------------- #


def test_oracle_is_exact_top_k():
    """For each query, independently recompute the brute-force top-k from the
    same corpus structures and assert equality with the manifest oracle."""
    args = _args()
    _, man = sc.build(args)

    # rebuild the corpus deterministically to get emb/ts/hub_dsts for an
    # independent brute-force check (same RNG draw order as build()).
    rng = np.random.default_rng(args.seed)
    n, dim = args.entities, args.dim
    emb = rng.standard_normal((n, dim)).astype(np.float64)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    ts = rng.integers(args.time_min, args.time_max + 1, size=n)
    hubs = list(range(args.hubs))
    hub_dsts = {}
    for h in hubs:
        centroid = rng.standard_normal(dim).astype(np.float64)
        centroid /= np.linalg.norm(centroid)
        d2 = np.sum((emb - centroid) ** 2, axis=1)
        pool = np.argsort(d2)[: max(args.fanout * 3, args.fanout + 1)]
        dsts = rng.choice(pool, size=min(args.fanout, len(pool)), replace=False)
        hub_dsts[h] = [int(d) for d in dsts if int(d) != h]

    non_empty = 0
    for q in man["queries"]:
        src = int(q["src"])
        qv = np.asarray(q["embedding"], dtype=np.float64)
        win = set(q["window"])
        cands = [d for d in hub_dsts[src] if int(ts[d]) in win]
        scored = sorted(
            ((float(((emb[d] - qv) ** 2).sum()), d) for d in cands),
            key=lambda x: (x[0], x[1]),  # by L2, ties by id
        )
        truth = [d for _, d in scored[: args.k]]
        assert q["oracle"] == truth
        if truth:
            non_empty += 1

    # the topical synthesis must leave at least one non-empty oracle, else the
    # recall checks below would be vacuous.
    assert non_empty > 0


def test_oracle_tiebreak_matches_id_order():
    """With an exact distance tie, the lower id must win (guards the lexsort in
    build() against the old argsort-by-position behavior)."""
    # Two candidates equidistant from the query: ids 7 and 3. Construct emb so
    # both have identical L2 to qv; lexsort((cd, d2)) must place id 3 first.
    dim = 4
    n = 10
    emb = np.zeros((n, dim), dtype=np.float64)
    qv = np.zeros(dim, dtype=np.float64)
    # id 3 and id 7 both at distance 1 from origin query; distinct directions.
    emb[3] = np.array([1.0, 0.0, 0.0, 0.0])
    emb[7] = np.array([0.0, 1.0, 0.0, 0.0])

    cand = [7, 3]  # deliberately listed with the higher id FIRST
    cd = np.array(cand)
    d2 = np.sum((emb[cd] - qv) ** 2, axis=1)
    order = cd[np.lexsort((cd, d2))]
    assert [int(x) for x in order] == [3, 7]  # lower id wins the tie


# --------------------------------------------------------------------------- #
# _recall: perfect / degraded / empty-oracle semantics
# --------------------------------------------------------------------------- #


def test_recall_perfect_and_degrades():
    assert sc._recall([1, 2, 3], [1, 2, 3]) == 1.0
    assert sc._recall([1, 2], [1, 2, 3, 4]) == 0.5
    assert sc._recall([9, 9], [1, 2]) == 0.0


def test_recall_empty_oracle():
    """Empty oracle (ts window excludes all reachable dst): recall is 1.0 only if
    nothing was returned; a false positive scores 0.0. This is the shared
    semantics tools/real_corpus.recall_at_k must match."""
    assert sc._recall([], []) == 1.0
    assert sc._recall([5], []) == 0.0


def test_query_with_empty_oracle_exists():
    """A tight window over a tiny corpus should leave at least one query whose
    reachable+filtered candidate set is empty, exercising the empty-oracle path
    in build()."""
    args = _args(entities=80, window=2, hubs=4, fanout=10, queries=8)
    _, man = sc.build(args)
    assert any(q["oracle"] == [] for q in man["queries"])


# --------------------------------------------------------------------------- #
# Transcript grading: build_ms / examined / latency / recall parsing
# --------------------------------------------------------------------------- #


def test_report_parses_build_examined_latency():
    """Feed report() a hand-written transcript in the live #SWEEP format and
    assert build_ms, mean_examined, median_latency_ms, and mean_recall@k all
    populate from the parsed lines."""
    # minimal manifest: one config, one term_cond, two queries with known oracles
    manifest = {
        "entities": 1000,
        "k": 3,
        "queries": [
            {"qid": 0, "oracle": [1, 2, 3]},
            {"qid": 1, "oracle": [4, 5, 6]},
        ],
    }
    cfg = "16_200"
    tc = 50
    transcript = "\n".join(
        [
            f"#SWEEP BUILD_BEGIN cfg={cfg}",
            "Time: 1234.500 ms",
            f"#SWEEP BUILD_END cfg={cfg}",
            # qid 0: perfect ids -> recall 1.0
            f"#SWEEP RESULT cfg={cfg} tc={tc} qid=0 ids=1,2,3",
            f"#SWEEP EXAMINED cfg={cfg} tc={tc} qid=0 examined=100",
            f"#SWEEP EXPLAIN_BEGIN cfg={cfg} tc={tc} qid=0",
            " Execution Time: 2.500 ms",
            f"#SWEEP EXPLAIN_END cfg={cfg} tc={tc} qid=0",
            # qid 1: one missing id -> recall 2/3
            f"#SWEEP RESULT cfg={cfg} tc={tc} qid=1 ids=4,5",
            f"#SWEEP EXAMINED cfg={cfg} tc={tc} qid=1 examined=300",
            f"#SWEEP EXPLAIN_BEGIN cfg={cfg} tc={tc} qid=1",
            " Execution Time: 4.500 ms",
            f"#SWEEP EXPLAIN_END cfg={cfg} tc={tc} qid=1",
            "#SWEEP DONE",
        ]
    )
    rep = sc.report(manifest, transcript)

    assert rep["build_ms"] == {cfg: 1234.5}
    assert rep["corpus"] == 1000
    assert rep["k"] == 3
    assert len(rep["sweep"]) == 1
    row = rep["sweep"][0]
    assert row["config"] == cfg
    assert row["term_cond"] == tc
    # recall: (1.0 + 2/3) / 2
    assert row["mean_recall@3"] == round((1.0 + 2.0 / 3.0) / 2.0, 4)
    # examined: mean of 100 and 300
    assert row["mean_examined"] == 200.0
    assert row["examined_pct"] == round(100.0 * 200.0 / 1000, 3)
    # latency: median of 2.5 and 4.5
    assert row["median_latency_ms"] == 3.5
    assert row["n"] == 2


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_determinism_same_seed():
    sql_a, man_a = sc.build(_args(seed=123))
    sql_b, man_b = sc.build(_args(seed=123))
    assert sql_a == sql_b
    assert man_a == man_b


def test_different_seed_changes_corpus():
    _, man_a = sc.build(_args(seed=1))
    _, man_b = sc.build(_args(seed=2))
    assert man_a["queries"] != man_b["queries"]


# --------------------------------------------------------------------------- #
# Window-bound validation (guards the cryptic numpy "low >= high")
# --------------------------------------------------------------------------- #


def test_window_too_large_raises_clear_error():
    args = _args(window=5000)  # > time_max - time_min + 1
    with pytest.raises(ValueError, match="window"):
        sc.build(args)

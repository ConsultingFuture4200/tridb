"""Unit tests for tools/real_corpus.py — the REAL-dataset benchmark harness.

These check the real-dataset path WITHOUT an engine and WITHOUT network/Docker:
a tiny synthetic `.npy` (and `.fvecs`) fixture is written to a tmp path inside the
test, loaded, turned into a topical-graph corpus + exact oracle, and emitted as the
canonical #BENCH SQL + manifest. The asserts pin the contract that makes this a
true drop-in for the synthetic tools/bench_corpus.py path:

  * the manifest's PUBLIC schema matches bench_corpus.py's manifest keys,
  * the numpy oracle is the EXACT true top-k (reachable + ts-filtered, by L2),
  * recall@k == 1.0 when results == oracle and degrades correctly otherwise,
  * the emitted SQL carries the same #BENCH / tjs markers, and
  * generation is deterministic across two runs with the same seed.

The LIVE engine run (latency, tjs_candidates_examined) stays GX10/engine-gated and
is NOT exercised here — only the correctness/recall surface, which is measurable
today on this x86 box.
"""

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from tools import real_corpus as rc  # noqa: E402
from tools.bench_corpus import build as bench_build  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures (written in-test; no network, no Docker)
# --------------------------------------------------------------------------- #


def _make_npy(tmp_path: Path, n: int = 200, dim: int = 16) -> Path:
    """A tiny deterministic float32 (n, dim) array on disk — stands in for a real
    embedding dataset (e.g. an ann-benchmarks train matrix)."""
    rng = np.random.default_rng(7)
    arr = rng.standard_normal((n, dim)).astype(np.float32)
    p = tmp_path / "vecs.npy"
    np.save(p, arr)
    return p


def _make_fvecs(tmp_path: Path, arr: np.ndarray) -> Path:
    """Write `arr` to a SIFT-style .fvecs file (int32 dim header + float32 row)."""
    p = tmp_path / "vecs.fvecs"
    with open(p, "wb") as f:
        for row in arr:
            f.write(struct.pack("<i", row.shape[0]))
            f.write(np.asarray(row, dtype=np.float32).tobytes())
    return p


def _synth(emb, **over):
    kw = dict(hubs=4, fanout=20, queries=6, k=5, window=400, seed=42)
    kw.update(over)
    return rc.synthesize_corpus(emb, **kw)


# --------------------------------------------------------------------------- #
# Loaders -> float64 (n, dim)
# --------------------------------------------------------------------------- #


def test_load_npy_returns_float64(tmp_path):
    p = _make_npy(tmp_path)
    arr = rc.load_vectors(p)
    assert arr.shape == (200, 16)
    assert arr.dtype == np.float64


def test_load_fvecs_round_trip(tmp_path):
    src = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
    p = _make_fvecs(tmp_path, src)
    arr = rc.load_fvecs(p)
    assert arr.shape == (2, 3)
    assert arr.dtype == np.float64
    assert np.allclose(arr, src.astype(np.float64))


def test_load_fvecs_via_dispatch(tmp_path):
    rng = np.random.default_rng(1)
    src = rng.standard_normal((10, 8)).astype(np.float32)
    p = _make_fvecs(tmp_path, src)
    arr = rc.load_vectors(p)
    assert arr.shape == (10, 8)
    assert np.allclose(arr, src.astype(np.float32).astype(np.float64))


def test_load_unsupported_extension(tmp_path):
    p = tmp_path / "vecs.txt"
    p.write_text("nope")
    with pytest.raises(ValueError):
        rc.load_vectors(p)


def test_hdf5_degrades_gracefully_without_h5py(tmp_path, monkeypatch):
    """If h5py is absent the .hdf5 loader must raise a clear, actionable error —
    not an ImportError at module import time (h5py is a soft, lazy dependency)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "h5py":
            raise ImportError("no h5py")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="h5py"):
        rc.load_hdf5(tmp_path / "x.hdf5")


# --------------------------------------------------------------------------- #
# Manifest schema parity with the synthetic path (drop-in contract)
# --------------------------------------------------------------------------- #


def test_manifest_public_schema_matches_bench_corpus(tmp_path):
    """Every PUBLIC manifest key the synthetic tools/bench_corpus.py emits must be
    present (same name + meaning) in the real-dataset manifest, so the downstream
    consumer (bench/live_report.py) treats them identically."""

    class _Args:
        entities = 200
        dim = 16
        hubs = 4
        fanout = 20
        queries = 6
        k = 5
        window = 400
        time_min = rc.DEFAULT_TIME_MIN
        time_max = rc.DEFAULT_TIME_MAX
        query_jitter = 0.35
        seed = 42

    _, synthetic = bench_build(_Args())
    synthetic_public = {k for k in synthetic if not k.startswith("_")}

    emb = rc.load_vectors(_make_npy(tmp_path))
    real = _synth(emb)

    for key in synthetic_public:
        assert key in real, f"real manifest missing public key {key!r}"
    # query rows have the same shape (qid / src / embedding / window)
    for q in real["queries"]:
        assert set(q) == {"qid", "src", "embedding", "window"}
        assert str(q["src"]) in real["hub_dsts"]
        assert len(q["embedding"]) == real["dim"]
        assert len(q["window"]) == real["window"]


def test_manifest_carries_real_dataset_oracle_and_entities(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    man = _synth(emb)
    # real vectors can't be RNG-regenerated, so the manifest MUST carry them.
    assert man["source"] == "real-dataset"
    assert len(man["_entities"]) == man["entities"]
    assert "oracle" in man
    assert set(man["oracle"]) == {str(q["qid"]) for q in man["queries"]}


# --------------------------------------------------------------------------- #
# Exact numpy oracle == true top-k
# --------------------------------------------------------------------------- #


def test_oracle_is_exact_true_top_k(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    man = _synth(emb)
    ts = {eid: t for eid, t, _ in man["_entities"]}

    non_empty = 0
    for q in man["queries"]:
        qid = str(q["qid"])
        src = int(q["src"])
        qv = np.asarray(q["embedding"], dtype=np.float64)
        win = set(q["window"])

        # independent brute-force ground truth from the manifest's own structures
        reach = man["hub_dsts"][str(src)]
        cands = [d for d in reach if ts[d] in win]
        scored = sorted(
            ((float(((emb[d] - qv) ** 2).sum()), d) for d in cands),
            key=lambda x: (x[0], x[1]),
        )
        truth = [d for _, d in scored[: man["k"]]]
        assert man["oracle"][qid] == truth
        if truth:
            non_empty += 1

    # the topical synthesis + wide window must leave at least one query with a
    # non-empty oracle, else the recall test below is vacuous.
    assert non_empty > 0


# --------------------------------------------------------------------------- #
# recall@k: perfect when results == oracle, degrades correctly otherwise
# --------------------------------------------------------------------------- #


def test_recall_perfect_when_results_equal_oracle(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    man = _synth(emb)
    results = {int(qid): list(ids) for qid, ids in man["oracle"].items()}
    rep = rc.report_recall(man, results)
    assert rep["mean_recall"] == 1.0
    # pure-oracle self-check mode is also 1.0
    assert rc.report_recall(man, None)["mean_recall"] == 1.0


def test_recall_degrades_when_results_wrong(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    man = _synth(emb)

    # Drop one true id per non-empty query -> recall must fall below 1.0 on those.
    results = {}
    has_non_empty = False
    for qid, ids in man["oracle"].items():
        ids = list(ids)
        if ids:
            has_non_empty = True
            results[int(qid)] = ids[1:]  # miss the top hit
        else:
            results[int(qid)] = ids
    assert has_non_empty
    rep = rc.report_recall(man, results)
    assert rep["mean_recall"] < 1.0

    # Empty results -> recall is 0.0 on every non-empty query (empty oracle == 1.0).
    empty = {int(qid): [] for qid in man["oracle"]}
    rep_empty = rc.report_recall(man, empty)
    assert rep_empty["mean_recall"] < 1.0


def test_recall_at_k_unit():
    assert rc.recall_at_k([1, 2, 3], [1, 2, 3], 3) == 1.0
    assert rc.recall_at_k([1, 2], [1, 2, 3, 4], 4) == 0.5
    assert rc.recall_at_k([9, 9], [1, 2], 2) == 0.0
    # empty oracle + nothing returned -> defined as full recall (nothing to find)
    assert rc.recall_at_k([], [], 5) == 1.0


def test_recall_empty_oracle_returns_zero_on_false_positive():
    """Regression: an empty oracle scores 1.0 ONLY if nothing was returned. A
    false positive against empty truth must score 0.0, matching
    tools/sweep_corpus._recall (the shared grading semantics)."""
    assert rc.recall_at_k([1, 2], []) == 0.0
    assert rc.recall_at_k([], []) == 1.0
    # the k-truncated path agrees with the no-k path
    assert rc.recall_at_k([1, 2], [], 5) == 0.0
    assert rc.recall_at_k([], [], 5) == 1.0


# --------------------------------------------------------------------------- #
# Emitted SQL carries the canonical #BENCH / tjs markers (drop-in format)
# --------------------------------------------------------------------------- #


def test_emit_sql_has_bench_and_tjs_markers(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    man = _synth(emb)
    sql = rc.emit(man)

    assert sql.startswith("-- AUTO-GENERATED by tools/real_corpus.py")
    # corpus built BEFORE the index (fork limitation), same as the synthetic path
    assert sql.index("INSERT INTO entities") < sql.index("CREATE INDEX entities_hnsw")
    # the canonical #BENCH markers the downstream consumer scrapes
    for marker in (
        "#BENCH ORACLE qid=",
        "#BENCH ORACLE_COUNTS qid=",
        "#BENCH TRIDB_RESULT qid=",
        "#BENCH TRIDB_EXAMINED qid=",
        "#BENCH DONE",
    ):
        assert marker in sql, marker
    # one tjs() in the result stmt + one in the EXPLAIN ANALYZE, per query
    assert sql.count("FROM tjs('entities',") == 2 * man["num_queries"]
    # the safe oracle ordering (no array_agg-ORDER-BY-on-d2 fork bug)
    assert "array_agg(id ORDER BY d2" not in sql
    assert "row_number() OVER (ORDER BY d2" in sql


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_determinism_same_seed(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    a = _synth(emb, seed=123)
    b = _synth(emb, seed=123)
    assert a["queries"] == b["queries"]
    assert a["hub_dsts"] == b["hub_dsts"]
    assert a["oracle"] == b["oracle"]
    assert rc.emit(a) == rc.emit(b)


def test_different_seed_changes_corpus(tmp_path):
    emb = rc.load_vectors(_make_npy(tmp_path))
    a = _synth(emb, seed=1)
    b = _synth(emb, seed=2)
    # the graph/query draws differ (vectors are fixed, but sampling/jitter differ)
    assert a["queries"] != b["queries"] or a["hub_dsts"] != b["hub_dsts"]


# --------------------------------------------------------------------------- #
# Results-file parsing (JSON + #BENCH transcript) for --report-recall
# --------------------------------------------------------------------------- #


def test_parse_results_json(tmp_path):
    p = tmp_path / "res.json"
    p.write_text('{"0": [1, 2, 3], "1": [4, 5]}')
    assert rc.parse_results_file(p) == {0: [1, 2, 3], 1: [4, 5]}


def test_parse_results_bench_transcript(tmp_path):
    p = tmp_path / "raw.txt"
    p.write_text(
        "noise\n"
        "#BENCH TRIDB_RESULT qid=0 ids=10,20,30\n"
        "#BENCH TRIDB_RESULT qid=1 ids=\n"
        "more noise\n"
    )
    assert rc.parse_results_file(p) == {0: [10, 20, 30], 1: []}


# --------------------------------------------------------------------------- #
# CLI smoke (generate + report-recall) — still no engine
# --------------------------------------------------------------------------- #


def test_cli_generate_then_report_recall(tmp_path):
    vecs = _make_npy(tmp_path)
    sql_out = tmp_path / "out.sql"
    man_out = tmp_path / "out.manifest.json"
    rc_code = rc.main(
        [
            "--vectors",
            str(vecs),
            "--hubs",
            "4",
            "--fanout",
            "20",
            "--queries",
            "6",
            "--k",
            "5",
            "--window",
            "400",
            "--seed",
            "42",
            "--sql-out",
            str(sql_out),
            "--manifest-out",
            str(man_out),
        ]
    )
    assert rc_code == 0
    assert sql_out.exists() and man_out.exists()
    assert "#BENCH DONE" in sql_out.read_text()

    # pure-oracle recall report off the written manifest (no engine)
    code = rc.main(["--report-recall", "--manifest", str(man_out)])
    assert code == 0

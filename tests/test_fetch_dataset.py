"""Unit tests for tools/fetch_dataset.py + the bench-public manifest/oracle wiring.

NO NETWORK, NO DOCKER, NO ENGINE. Two surfaces are exercised:

  1. tools/fetch_dataset.py OFFLINE pieces — the pinned-dataset REGISTRY shape and
     the SHA256 checksum/verify helpers (against an in-test file). The actual
     download is network-gated and is never invoked here.

  2. The bench-public wiring — that tools/real_corpus.py's ann-benchmarks .hdf5
     loader + exact oracle + recall grading all work on a TINY synthetic .hdf5
     written in-test. h5py is OPTIONAL (lazy import in real_corpus.load_hdf5 and
     not a hard dep): if it is absent these tests SKIP via pytest.importorskip.

Models tests/test_real_corpus.py (same fixture-in-test, drop-in-contract style).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from tools import fetch_dataset as fd  # noqa: E402
from tools import real_corpus as rc  # noqa: E402


# --------------------------------------------------------------------------- #
# fetch_dataset: pinned-dataset REGISTRY shape
# --------------------------------------------------------------------------- #


def test_default_dataset_is_in_registry():
    assert fd.DEFAULT_DATASET in fd.REGISTRY


def test_default_dataset_meets_headline_requirements():
    """The default must be a RECOGNIZED, L2, dim-768+ set (the GTM headline wants
    768+ real embeddings, and the canonical query ranks by L2 / <->)."""
    ds = fd.REGISTRY[fd.DEFAULT_DATASET]
    assert ds.dim >= 768, "default dataset must be dim 768+ for the headline"
    assert ds.distance == "euclidean", "canonical query is L2 — default must be L2"
    assert ds.url.endswith(".hdf5"), "ann-benchmarks datasets are HDF5"


def test_registry_entries_are_well_formed():
    for name, ds in fd.REGISTRY.items():
        assert ds.name == name
        assert ds.url.startswith("http")
        assert ds.distance in ("euclidean", "angular")
        assert ds.dim > 0
        assert isinstance(ds.sha256, str) and ds.sha256


# --------------------------------------------------------------------------- #
# fetch_dataset: SHA256 checksum / verification helpers (offline)
# --------------------------------------------------------------------------- #


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib

    p = tmp_path / "blob.bin"
    data = b"tridb public dataset bytes"
    p.write_bytes(data)
    assert fd.sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_verify_checksum_passes_on_match(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"abc")
    digest = fd.sha256_file(p)
    # exact + case-insensitive match both pass
    fd.verify_checksum(p, digest)
    fd.verify_checksum(p, digest.upper())


def test_verify_checksum_raises_on_mismatch(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"abc")
    with pytest.raises(ValueError, match="MISMATCH"):
        fd.verify_checksum(p, "0" * 64)


def test_verify_checksum_rejects_pending_sentinel(tmp_path):
    """An unpinned (_PENDING) checksum must never silently pass verification."""
    p = tmp_path / "blob.bin"
    p.write_bytes(b"abc")
    with pytest.raises(ValueError, match="unpinned"):
        fd.verify_checksum(p, fd._PENDING)


def test_fetch_refuses_unpinned_without_escape(monkeypatch, tmp_path):
    """fetch() must NOT download an unpinned dataset without --pin/--allow-unpinned.

    The guard fires BEFORE any network call: we assert by stubbing _download to
    blow up — if the guard works, _download is never reached.
    """

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("download attempted for an unpinned dataset")

    monkeypatch.setattr(fd, "_download", _boom)
    with pytest.raises(SystemExit, match="UNPINNED"):
        fd.fetch(fd.DEFAULT_DATASET, cache=tmp_path)


def test_fetch_unknown_dataset_raises(tmp_path):
    with pytest.raises(KeyError):
        fd.fetch("not-a-real-dataset", cache=tmp_path)


def test_cli_list_runs(capsys):
    assert fd.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert fd.DEFAULT_DATASET in out


# --------------------------------------------------------------------------- #
# bench-public wiring: tiny synthetic ann-benchmarks .hdf5 (lazy h5py, no network)
# --------------------------------------------------------------------------- #


def _make_ann_hdf5(tmp_path: Path, n: int = 120, dim: int = 16) -> Path:
    """Write a tiny ann-benchmarks-shaped .hdf5 with a 'train' (n, dim) matrix.

    Mirrors the shape real_corpus.load_hdf5 reads (the ann-benchmarks 'train'
    dataset). h5py is optional; the caller importorskips it.
    """
    h5py = pytest.importorskip("h5py")
    rng = np.random.default_rng(11)
    train = rng.standard_normal((n, dim)).astype(np.float32)
    p = tmp_path / "tiny-ann.hdf5"
    with h5py.File(p, "w") as f:
        f.create_dataset("train", data=train)
    return p


def test_hdf5_loader_returns_right_shape(tmp_path):
    p = _make_ann_hdf5(tmp_path, n=120, dim=16)
    arr = rc.load_vectors(p, hdf5_dataset="train")
    assert arr.shape == (120, 16)
    assert arr.dtype == np.float64


def test_hdf5_limit_takes_prefix(tmp_path):
    """The bench-public --limit slices the FIRST N rows (bounded live smoke over
    a deterministic prefix of a large public set)."""
    p = _make_ann_hdf5(tmp_path, n=120, dim=16)
    full = rc.load_vectors(p, hdf5_dataset="train")
    sliced = rc.load_vectors(p, hdf5_dataset="train", limit=40)
    assert sliced.shape == (40, 16)
    assert np.allclose(sliced, full[:40])
    # limit larger than the corpus is a no-op (not an error)
    assert rc.load_vectors(p, hdf5_dataset="train", limit=10_000).shape == (120, 16)


def test_bench_public_oracle_is_exact_brute_force_top_k(tmp_path):
    """The bench-public oracle (over the .hdf5 corpus) must equal an independent
    brute-force reachable+filtered top-k — this is the recall reference the live
    engine is graded against."""
    emb = rc.load_vectors(_make_ann_hdf5(tmp_path), hdf5_dataset="train")
    man = rc.synthesize_corpus(
        emb, hubs=4, fanout=20, queries=6, k=5, window=400, seed=42
    )
    ts = {eid: t for eid, t, _ in man["_entities"]}

    non_empty = 0
    for q in man["queries"]:
        qid, src = str(q["qid"]), int(q["src"])
        qv = np.asarray(q["embedding"], dtype=np.float64)
        win = set(q["window"])
        reach = man["hub_dsts"][str(src)]
        cands = [d for d in reach if ts[d] in win]
        truth = [
            d
            for _, d in sorted(
                ((float(((emb[d] - qv) ** 2).sum()), d) for d in cands),
                key=lambda x: (x[0], x[1]),
            )[: man["k"]]
        ]
        assert man["oracle"][qid] == truth
        non_empty += bool(truth)
    assert non_empty > 0, (
        "fixture must leave a non-empty oracle or the grade is vacuous"
    )


def test_bench_public_recall_grading_end_to_end(tmp_path):
    """report_recall must score 1.0 when the (simulated) engine result == oracle,
    and degrade when it misses — the exact grade scripts/bench_public.sh applies
    to the live #BENCH transcript."""
    emb = rc.load_vectors(_make_ann_hdf5(tmp_path), hdf5_dataset="train")
    man = rc.synthesize_corpus(
        emb, hubs=4, fanout=20, queries=6, k=5, window=400, seed=42
    )

    perfect = {int(qid): list(ids) for qid, ids in man["oracle"].items()}
    assert rc.report_recall(man, perfect)["mean_recall"] == 1.0
    # pure-oracle self-check (no engine) is the plumbing sanity baseline
    assert rc.report_recall(man, None)["mean_recall"] == 1.0

    # drop the top hit on every non-empty query -> mean recall must fall below 1.0
    missed = {}
    had_non_empty = False
    for qid, ids in man["oracle"].items():
        ids = list(ids)
        if ids:
            had_non_empty = True
            missed[int(qid)] = ids[1:]
        else:
            missed[int(qid)] = ids
    assert had_non_empty
    assert rc.report_recall(man, missed)["mean_recall"] < 1.0


def test_bench_public_emits_canonical_bench_sql(tmp_path):
    """The .hdf5 path emits the SAME canonical #BENCH SQL the live engine consumes
    (drop-in with the synthetic path), so scripts/bench_public.sh feeds the engine
    an identical surface."""
    emb = rc.load_vectors(_make_ann_hdf5(tmp_path), hdf5_dataset="train")
    man = rc.synthesize_corpus(
        emb, hubs=4, fanout=20, queries=6, k=5, window=400, seed=42
    )
    sql = rc.emit(man)
    for marker in (
        "#BENCH ORACLE qid=",
        "#BENCH TRIDB_RESULT qid=",
        "#BENCH DONE",
    ):
        assert marker in sql, marker
    # one tjs() in the result stmt + one in the EXPLAIN ANALYZE, per query
    assert sql.count("FROM tjs('entities',") == 2 * man["num_queries"]

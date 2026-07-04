"""Batch-slicing tests for baseline/sm2.py's one-time loaders.

The SM-2 baseline loads the corpus into Milvus/Neo4j/Postgres in fixed-size
batches (1000 / 10_000 / 500). A slicing bug (overlap, omission, off-by-one at
the batch boundary) would silently corrupt the baseline side of the SM-2
head-to-head, so pin the reassembly at the boundary sizes with fake clients —
no live systems needed (heavy clients import lazily / inside the loaders).
"""

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# baseline/sm2.py resolves `from harness import ...` relative to its own dir
# (the same shim its __main__ uses).
sys.path.insert(0, str(ROOT / "baseline"))

import baseline.sm2 as sm2  # noqa: E402

MILVUS_BATCH = 1000
NEO4J_BATCH = 10_000
POSTGRES_BATCH = 500


def _corpus(n: int) -> dict:
    return {
        "dim": 4,
        "entities": {
            i: {
                "timestamp": 100 + i,
                "chunk": f"chunk {i}",
                "embedding": [float(i), 0.0, 0.0, 0.0],
            }
            for i in range(n)
        },
        "edges": [],
        "queries": [],
    }


def _sizes(batch: int) -> list[int]:
    return [0, 1, batch, batch + 1, 2 * batch + 3]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeCollection:
    created: list["_FakeCollection"] = []

    def __init__(self, name, schema=None, using=None):
        self.name = name
        self.inserts: list[list] = []
        _FakeCollection.created.append(self)

    def insert(self, data):
        self.inserts.append(data)

    def flush(self):
        pass

    def create_index(self, field, params):
        pass

    def load(self):
        pass


def _fake_pymilvus() -> types.ModuleType:
    mod = types.ModuleType("pymilvus")
    mod.Collection = _FakeCollection
    mod.CollectionSchema = lambda fields: fields
    mod.FieldSchema = lambda *a, **kw: (a, kw)
    mod.DataType = types.SimpleNamespace(INT64="INT64", FLOAT_VECTOR="FLOAT_VECTOR")
    mod.utility = types.SimpleNamespace(
        has_collection=lambda name: False,
        drop_collection=lambda name: None,
    )
    return mod


class _FakeNeo4jSession:
    def __init__(self, store: dict):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **kwargs):
        if "rows" in kwargs:
            self._store["node_batches"].append(kwargs["rows"])
        if "erows" in kwargs:
            self._store["edge_batches"].append(kwargs["erows"])


class _FakeNeo4jDriver:
    def __init__(self, store: dict):
        self._store = store

    def session(self):
        return _FakeNeo4jSession(self._store)

    def close(self):
        pass


class _FakeCopy:
    def __init__(self, store: dict):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self._store["copy_rows"].append(tuple(row))


class _FakePgCursor:
    def __init__(self, store: dict):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, vals=None):
        pass

    def copy(self, sql):
        self._store.setdefault("copy_rows", [])
        return _FakeCopy(self._store)


class _FakePg:
    def __init__(self, store: dict):
        self._store = store

    def cursor(self):
        return _FakePgCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# Slicing tests: captured batches reassemble to exactly the input ids
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n", _sizes(MILVUS_BATCH))
def test_load_milvus_slicing(monkeypatch, n):
    monkeypatch.setitem(sys.modules, "pymilvus", _fake_pymilvus())
    monkeypatch.setattr(_FakeCollection, "created", [])
    corpus = _corpus(n)
    sm2.load_milvus(sm2.Conn(), corpus)
    col = _FakeCollection.created[-1]
    got_ids: list[int] = []
    for ids_batch, vecs_batch in col.inserts:
        assert 1 <= len(ids_batch) <= MILVUS_BATCH
        assert len(ids_batch) == len(vecs_batch)
        got_ids.extend(ids_batch)
    assert got_ids == sorted(corpus["entities"].keys())
    assert len(col.inserts) == -(-n // MILVUS_BATCH)  # ceil(n/batch); 0 -> none


@pytest.mark.parametrize("n", _sizes(NEO4J_BATCH))
def test_load_neo4j_slicing(monkeypatch, n):
    store: dict = {"node_batches": [], "edge_batches": []}
    monkeypatch.setattr(sm2, "connect_neo4j", lambda conn: _FakeNeo4jDriver(store))
    corpus = _corpus(n)
    sm2.load_neo4j(sm2.Conn(), corpus)
    got_ids: list[int] = []
    for batch in store["node_batches"]:
        assert 1 <= len(batch) <= NEO4J_BATCH
        got_ids.extend(r["id"] for r in batch)
    assert sorted(got_ids) == sorted(corpus["entities"].keys())
    assert len(got_ids) == n  # no overlap/duplication


@pytest.mark.parametrize("n", _sizes(POSTGRES_BATCH))
def test_load_postgres_copy_streams_all_ids(monkeypatch, n):
    # baseline/sm2.py now loads via COPY FROM STDIN (plan 035): every entity is
    # streamed once, in id order, as an (id, timestamp, chunk) row — no batching,
    # no omission/duplication.
    store: dict = {"copy_rows": []}
    monkeypatch.setattr(sm2, "connect_postgres", lambda conn: _FakePg(store))
    corpus = _corpus(n)
    sm2.load_postgres(sm2.Conn(), corpus)
    for row in store["copy_rows"]:
        assert len(row) == 3  # (id, timestamp, chunk)
    got_ids = [row[0] for row in store["copy_rows"]]
    assert got_ids == sorted(corpus["entities"].keys())


# --------------------------------------------------------------------------- #
# Import-time env parsing (documents current behavior)
# --------------------------------------------------------------------------- #


def test_fanout_env_parse_error_raises_valueerror(monkeypatch):
    """BASELINE_ANN_FANOUT is parsed at import time; a non-int value fails the
    module load with a ValueError (documented, not swallowed)."""
    monkeypatch.setenv("BASELINE_ANN_FANOUT", "notanint")
    with pytest.raises(ValueError):
        importlib.reload(sm2)
    monkeypatch.delenv("BASELINE_ANN_FANOUT")
    importlib.reload(sm2)  # restore a clean module for other tests
    assert sm2.BASELINE_ANN_FANOUT == 32


def test_baseline_index_config_selection(monkeypatch):
    """BASELINE_INDEX selects IVF_FLAT (default) or HNSW; the config is what gets
    stamped into the run payload (plan 030 step 3 / Fabio review)."""
    import importlib
    import sys

    sys.path.insert(0, str(ROOT / "baseline"))

    monkeypatch.setenv("BASELINE_INDEX", "HNSW")
    monkeypatch.setenv("BASELINE_HNSW_M", "16")
    monkeypatch.setenv("BASELINE_EF", "128")
    import sm2

    importlib.reload(sm2)
    assert sm2.MILVUS_INDEX["index_type"] == "HNSW"
    assert sm2.MILVUS_INDEX["params"] == {"M": 16, "efConstruction": 200}
    assert sm2.MILVUS_SEARCH_PARAM["params"] == {"ef": 128}

    monkeypatch.delenv("BASELINE_INDEX", raising=False)
    importlib.reload(sm2)
    assert sm2.MILVUS_INDEX["index_type"] == "IVF_FLAT"
    assert "nlist" in sm2.MILVUS_INDEX["params"]

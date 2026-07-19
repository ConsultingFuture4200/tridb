"""Tests for the MCP agent-memory server (tools/tridb_mcp.py, advisor plan 098).

Two layers:
  * Unit: MemoryStore logic against a mocked psycopg connection (no docker, no
    network, no mcp package needed) + the MCP tool-surface registration
    (skipped when the `mcp` extra is not installed — see requirements-mcp.txt).
  * Live integration (skipped without docker + the release image): the real
    tool functions against a live tridb/postgres-trimodal container — the
    tjs_ppr_test.sql fixture rebuilt through the MCP surface: 5 memories,
    3 typed connects, and the assertion that fused recall ranks the
    multi-path-reinforced memory ahead of its vector-distance-tied twin
    (the PPR-graded order the ADR-0021 default guarantees).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager

import pytest

from tools.tridb_mcp import DEFAULT_DIM, Embedder, MemoryStore, build_server

# ---------------------------------------------------------------------------
# mocked connection
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Routes execute() by SQL substring to canned rows (each consumed once);
    records every call as (sql, params, in_txn)."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params, self._txn > 0))
        for i, (sub, rows) in enumerate(self.responses):
            if sub in sql:
                self.responses.pop(i)
                return FakeCursor(rows)
        return FakeCursor([])

    _txn = 0

    @contextmanager
    def transaction(self):
        self._txn += 1
        try:
            yield
        finally:
            self._txn -= 1

    def sql_calls(self, sub):
        return [c for c in self.calls if sub in c[0]]


def make_store(responses=(), **kw):
    kw.setdefault("dim", 4)
    return MemoryStore(FakeConn(responses), **kw)


# ---------------------------------------------------------------------------
# store_memory
# ---------------------------------------------------------------------------


def test_store_memory_atomic_dense_id():
    store = make_store([("COALESCE(max(id)", [(0,)]), ("gph_upsert_vertex", [(0,)])])
    out = store.store_memory("hello", kind="fact", embedding=[1, 2, 3, 4])
    assert out == {"id": 0}
    insert = store.conn.sql_calls("INSERT INTO memories")[0]
    upsert = store.conn.sql_calls("gph_upsert_vertex")[0]
    # row + vertex written inside ONE transaction (the one-WAL atomicity claim)
    assert insert[2] and upsert[2]
    assert insert[1] == (0, "fact", "hello", "[1.0,2.0,3.0,4.0]")


def test_store_memory_refuses_vid_drift():
    store = make_store([("COALESCE(max(id)", [(5,)]), ("gph_upsert_vertex", [(7,)])])
    with pytest.raises(RuntimeError, match="dense-id drift"):
        store.store_memory("x", embedding=[0, 0, 0, 0])


def test_store_memory_rejects_wrong_dim():
    store = make_store()
    with pytest.raises(ValueError, match="dims"):
        store.store_memory("x", embedding=[1.0])


def test_store_memory_without_embedder_requires_vector():
    store = make_store()
    with pytest.raises(RuntimeError, match="caller-supplied"):
        store.store_memory("x")


def test_store_memory_uses_embedder_for_bare_text():
    class StubEmbedder:
        def encode(self, text):
            return [0.1, 0.2, 0.3, 0.4]

    store = make_store(
        [("COALESCE(max(id)", [(2,)]), ("gph_upsert_vertex", [(2,)])],
        embedder=StubEmbedder(),
    )
    assert store.store_memory("bare text") == {"id": 2}
    insert = store.conn.sql_calls("INSERT INTO memories")[0]
    assert insert[1][3] == "[0.1,0.2,0.3,0.4]"


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_registers_type_and_inserts_edge():
    store = make_store([("id = ANY", [(0,), (1,)]), ("register_edge_type", [(3,)])])
    out = store.connect(0, 1, "supports")
    assert out == {"edge": {"src": 0, "dst": 1, "rel": "supports", "type_id": 3}}
    edge = store.conn.sql_calls("gph_insert_edge")[0]
    assert edge[1] == (0, 1, 3)
    assert edge[2]  # inside the transaction


def test_connect_unknown_memory_raises():
    store = make_store([("id = ANY", [(0,)])])
    with pytest.raises(ValueError, match="unknown memory id 9"):
        store.connect(0, 9, "supports")


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


def test_recall_validates_mode_and_query():
    store = make_store()
    with pytest.raises(ValueError, match="mode"):
        store.recall(embedding=[0, 0, 0, 0], mode="hybrid")
    with pytest.raises(ValueError, match="text or embedding"):
        store.recall()


def test_recall_vector_mode_plain_hnsw():
    store = make_store(
        [("ORDER BY embedding", [(0, "a", "note", 0.25), (1, "b", "note", 0.5)])]
    )
    out = store.recall(embedding=[0, 0, 0, 0], k=2, mode="vector")
    assert out["mode"] == "vector"
    assert out["graph_censored"] is None
    assert out["termination_reason"] is None
    assert out["results"][0] == {"id": 0, "text": "a", "kind": "note", "score": -0.25}
    assert not store.conn.sql_calls("tjs_open")
    assert not store.conn.sql_calls("SET hnsw.iterative_scan")


def test_recall_fused_mode_tjs_open_with_honesty_metadata():
    store = make_store(
        [
            ("tjs_open('memories'", [(0, "a", "note", 0.01), (2, "c", "fact", 0.5)]),
            ("tjs_open_graph_censored", [(False,)]),
            ("tjs_open_termination_reason", [("term_cond",)]),
        ],
        term_cond=100,
        m_seeds=2,
        hops=3,
    )
    out = store.recall(embedding=[0, 0, 0, 0], k=5)
    # relaxed-order iterative scan MUST be set before the seedless call
    set_idx = store.conn.calls.index(store.conn.sql_calls("SET hnsw")[0])
    open_idx = store.conn.calls.index(store.conn.sql_calls("tjs_open('memories'")[0])
    assert set_idx < open_idx
    params = store.conn.sql_calls("tjs_open('memories'")[0][1]
    assert params[1:5] == (5, 100, 2, 3)  # k, term_cond, m_seeds, hops
    assert params[6] is None  # seedless: src = NULL
    assert out["mode"] == "fused"
    assert out["graph_censored"] is False
    assert out["termination_reason"] == "term_cond"
    assert [r["id"] for r in out["results"]] == [0, 2]


def test_recall_fused_anchor_becomes_filter_first_src():
    store = make_store(
        [
            ("tjs_open('memories'", [(2, "c", "fact", 0.5)]),
            ("tjs_open_graph_censored", [(False,)]),
            ("tjs_open_termination_reason", [("filter_first",)]),
        ]
    )
    out = store.recall(embedding=[0, 0, 0, 0], k=4, anchor_id=7)
    assert store.conn.sql_calls("tjs_open('memories'")[0][1][6] == 7
    assert out["termination_reason"] == "filter_first"


# ---------------------------------------------------------------------------
# neighbors / stats
# ---------------------------------------------------------------------------


def test_neighbors_unknown_rel_raises():
    store = make_store([("edge_type WHERE name", [])])
    with pytest.raises(ValueError, match="unknown edge type"):
        store.neighbors(0, rel="nope")


def test_neighbors_one_hop_typed_traversal():
    store = make_store(
        [
            ("edge_type WHERE name", [(3,)]),
            ("gph_traverse_typed", [(2,), (3,)]),
            ("FROM memories WHERE id = ANY", [(3, "c", "note"), (2, "a", "fact")]),
        ]
    )
    out = store.neighbors(0, rel="supports")
    assert store.conn.sql_calls("gph_traverse_typed")[0][1] == (0, 3)
    # engine emission order preserved, payload joined back
    assert out == [
        {"id": 2, "text": "a", "kind": "fact"},
        {"id": 3, "text": "c", "kind": "note"},
    ]


def test_neighbors_multi_hop_uses_bfs():
    store = make_store(
        [
            ("gph_traverse_bfs", [(2,), (3,)]),
            ("FROM memories WHERE id = ANY", [(2, "a", "fact"), (3, "c", "note")]),
        ]
    )
    out = store.neighbors(0, hops=2)
    assert store.conn.sql_calls("gph_traverse_bfs")[0][1] == (0, 2, 0)
    assert [n["id"] for n in out] == [2, 3]
    assert not store.conn.sql_calls("gph_traverse_typed")


def test_memory_stats_shape():
    store = make_store(
        [
            ("count(*) FROM memories", [(5,)]),
            ("gph_vertex_count", [(5,)]),
            ("gph_visible_edge_count", [(3,)]),
            ("FROM graph_store.edge_type", [("related_to",), ("supports",)]),
            ("FROM pg_extension", [("vector", "0.8.0"), ("tjs_pg", "0.1.0")]),
            ("server_version", [("17.5",)]),
        ]
    )
    out = store.memory_stats()
    assert out["memories"] == 5
    assert out["vertices"] == 5
    assert out["edges"] == 3
    assert out["edge_types"] == ["related_to", "supports"]
    assert out["engine"]["extensions"]["tjs_pg"] == "0.1.0"


# ---------------------------------------------------------------------------
# MCP tool surface (needs the `mcp` extra; requirements-mcp.txt)
# ---------------------------------------------------------------------------


def test_build_server_registers_the_five_tools():
    pytest.importorskip("mcp")
    import anyio

    server = build_server(make_store())
    tools = anyio.run(server.list_tools)
    assert sorted(t.name for t in tools) == [
        "connect",
        "memory_stats",
        "neighbors",
        "recall",
        "store_memory",
    ]


def test_default_embedder_model_is_pinned():
    # No network here — just the pinned identity the docs advertise.
    assert Embedder.__init__.__defaults__ == ("BAAI/bge-small-en-v1.5",)
    assert DEFAULT_DIM == 384


# ---------------------------------------------------------------------------
# live integration (docker + release image)
# ---------------------------------------------------------------------------

RELEASE_IMAGE = os.environ.get("TRIDB_MCP_IMAGE", "tridb/postgres-trimodal:pg17")


def _engine_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", RELEASE_IMAGE], capture_output=True
    )
    return probe.returncode == 0


requires_engine = pytest.mark.skipif(
    not _engine_available(),
    reason=f"docker + release image {RELEASE_IMAGE} required",
)


@pytest.fixture(scope="module")
def live_store():
    psycopg = pytest.importorskip("psycopg")
    name = f"tridb-mcp-it-{uuid.uuid4().hex[:8]}"
    pw = uuid.uuid4().hex
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={pw}",
            "-p",
            "127.0.0.1:0:5432",  # docker picks a free host port
            RELEASE_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    conn = None
    try:
        # two consecutive OK probes 1s apart (initdb's temp server races one)
        ok = 0
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
            )
            ok = ok + 1 if ready.returncode == 0 else 0
            if ok >= 2:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"{RELEASE_IMAGE} not ready after 60s")
        port = (
            subprocess.run(
                ["docker", "port", name, "5432/tcp"],
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.splitlines()[0]
            .rsplit(":", 1)[1]
        )
        conn = psycopg.connect(
            f"postgresql://postgres:{pw}@127.0.0.1:{port}/postgres",
            autocommit=True,
            connect_timeout=10,
        )
        # dim=1 mirrors test/tjs_ppr_test.sql; term_cond=1000 consumes the
        # whole 5-row stream (m_seeds=1, hops=2 — the hand-computed fixture).
        store = MemoryStore(conn, dim=1, term_cond=1000, m_seeds=1, hops=2)
        store.init_schema()
        yield store
    finally:
        if conn is not None:
            conn.close()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@requires_engine
def test_live_end_to_end(live_store):
    """The tjs_ppr_test.sql scenario through the MCP tool functions.

    Corpus: S=0 is the ANN top seed (dist 0.01); B=1, A=2, C=3 are
    vector-distance-TIED (0.5); D=4 is far (10.0). Edges (3 connects):
    S->A, S->C, C->A — A is multi-path-reinforced, B has no links at all.
    Fused recall under the PPR default must rank A ahead of B even though
    vector distance cannot tell them apart; membership/vector order would
    fall back to the ascending-id tie-break (0,1,2,3).
    """
    store = live_store
    texts = {
        0: "S: the seed memory",
        1: "B: vector twin, unconnected",
        2: "A: vector twin, reinforced via two paths",
        3: "C: bridge memory, links to A",
        4: "D: far-away memory",
    }
    for mid, emb in ((0, 0.01), (1, 0.5), (2, 0.5), (3, 0.5), (4, 10.0)):
        got = store.store_memory(texts[mid], kind="note", embedding=[emb])
        assert got == {"id": mid}, f"dense id drift for {texts[mid]}"
    for src, dst in ((0, 2), (0, 3), (3, 2)):
        edge = store.connect(src, dst, "supports")["edge"]
        assert edge["src"] == src and edge["dst"] == dst

    # fused (seedless, PPR default): S, then A (reinforced) ahead of the
    # vector-tied C (one path) and B (no paths). Flip this expectation to
    # [0, 1, 2, 3] and the test MUST fail — that is the negative control.
    fused = store.recall(embedding=[0.0], k=4, mode="fused")
    assert [r["id"] for r in fused["results"]] == [0, 2, 3, 1], fused
    assert fused["graph_censored"] is False
    assert fused["termination_reason"] in ("term_cond", "stream_end_unknown")
    assert fused["results"][0]["score"] == pytest.approx(-0.01)

    # pure vector mode: distance order only (ties unordered), S first, D out.
    vec = store.recall(embedding=[0.0], k=4, mode="vector")
    assert vec["results"][0]["id"] == 0
    assert {r["id"] for r in vec["results"]} == {0, 1, 2, 3}
    assert vec["graph_censored"] is None

    # anchored (filter-first): only the anchor's bounded reach {A, C}.
    anchored = store.recall(embedding=[0.0], k=4, mode="fused", anchor_id=0)
    assert {r["id"] for r in anchored["results"]} == {2, 3}
    assert anchored["termination_reason"] == "filter_first"

    # direct graph reads
    assert [n["id"] for n in store.neighbors(0)] == [2, 3]
    assert [n["id"] for n in store.neighbors(3, rel="supports")] == [2]
    assert {n["id"] for n in store.neighbors(0, hops=2)} == {2, 3}
    with pytest.raises(ValueError, match="unknown edge type"):
        store.neighbors(0, rel="refutes")

    stats = store.memory_stats()
    assert stats["memories"] == 5
    assert stats["vertices"] == 5
    assert stats["edges"] == 3
    assert "supports" in stats["edge_types"]
    assert set(stats["engine"]["extensions"]) == {
        "vector",
        "graph_store_am",
        "tjs_pg",
    }

    # init is idempotent against a populated store
    assert store.init_schema() == {"ok": True, "dim": 1}
    assert store.memory_stats()["memories"] == 5

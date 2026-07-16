"""Host tests for the plan-060 engine loader — no docker, no network, no engine.

Drives the pure logic of tools/wikidata_engine_load over a synthetic 2-shard slice
(with a duplicate entity id, a duplicate manifest shard path, dangling edges and an
out-of-slice P31 type) and verifies: the dense map is IDENTICAL to
bench/wikidata_h2h.load_dense_map (the harness contract), the P31 dense remap, the
dangling-edge drop + parity counts, and the generated SQL surface (table DDL,
gph_upsert_vertex ordinal verify, register_edge_type / typed gph_insert_edge,
HNSW build + health probe, --force reset).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_h2h import (  # noqa: E402
    WCfg,
    load_dense_map,
    load_typed_adj,
    load_types,
)
from tools.wikidata_engine_load import (  # noqa: E402
    build_dense_map,
    entity_copy_row,
    iter_kept_edges,
    iter_load_sql,
    load_p31_dense,
    parse_transcript,
    shard_paths,
    vec_literal,
)

# dense: Q10->0 Q20->1 Q30->2 (shard 0) Q99->3 Q88->4 (shard 1; the DUP Q10 is skipped)
ENTS0 = [
    {"id": 10, "label": "Universe", "description": "everything"},
    {"id": 20, "label": "Galaxy", "description": "bound system"},
    {"id": 30, "label": "Star", "description": "fusor"},
]
ENTS1 = [
    {"id": 99, "label": "class", "description": "type node"},
    {"id": 10, "label": "Universe", "description": "everything"},  # DUPLICATE id
    {"id": 88, "label": "kind", "description": "another type"},
]
# Q555 never appears as an entity -> its edges are DANGLING and must be dropped.
EDGES0 = [
    (10, 31, 99),
    (10, 50, 20),
    (20, 31, 99),
    (10, 361, 555),  # dangling dst
]
EDGES1 = [
    (99, 279, 88),
    (30, 31, 88),
    (555, 31, 10),  # dangling src
]
# kept (dense): (0,31,3) (0,50,1) (1,31,3) (3,279,4) (2,31,4)
KEPT_DENSE = [(0, 31, 3), (0, 50, 1), (1, 31, 3), (3, 279, 4), (2, 31, 4)]
CLAIMS0 = [
    {"id": 10, "P31": [99]},
    {"id": 20, "P31": [99]},
    {"id": 30, "P31": [88, 555]},  # 555 is out-of-slice -> dropped by the remap
]
CLAIMS1 = [
    {"id": 99, "P31": []},
    {"id": 10, "P31": [99]},  # duplicate claims row (identical content)
    {"id": 88, "P31": []},
]
N = 5
DIM = 4


def _write_slice(tmp_path: Path) -> dict:
    (tmp_path / "entities-00000.jsonl").write_text(
        "\n".join(json.dumps(e) for e in ENTS0) + "\n"
    )
    (tmp_path / "entities-00001.jsonl").write_text(
        "\n".join(json.dumps(e) for e in ENTS1) + "\n"
    )
    (tmp_path / "edges-00000.tsv").write_text(
        "\n".join(f"{s}\t{p}\t{d}" for s, p, d in EDGES0) + "\n"
    )
    (tmp_path / "edges-00001.tsv").write_text(
        "\n".join(f"{s}\t{p}\t{d}" for s, p, d in EDGES1) + "\n"
    )
    (tmp_path / "claims-00000.jsonl").write_text(
        "\n".join(json.dumps(c) for c in CLAIMS0) + "\n"
    )
    (tmp_path / "claims-00001.jsonl").write_text(
        "\n".join(json.dumps(c) for c in CLAIMS1) + "\n"
    )
    manifest = {
        "shards": {
            "entities": {
                "files": [
                    {"path": "entities-00000.jsonl"},
                    {"path": "entities-00001.jsonl"},
                    # duplicate descriptor: real manifests can repeat a path; the
                    # loaders must read it once (order-preserving dedup).
                    {"path": "entities-00001.jsonl"},
                ]
            },
            "edges": {
                "files": [
                    {"path": "edges-00000.tsv"},
                    {"path": "edges-00001.tsv"},
                ]
            },
            "claims": {
                "files": [
                    {"path": "claims-00000.jsonl"},
                    {"path": "claims-00001.jsonl"},
                ]
            },
        }
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def _emb() -> np.ndarray:
    # deliberately NOT normalized: the loader must L2-normalize at write
    rng = np.random.default_rng(1354)
    return (rng.normal(size=(N + 1, DIM)) * 3.0).astype(np.float32)


# --------------------------------------------------------------------------- #
# dense-id contract — identical to the harness's loaders
# --------------------------------------------------------------------------- #
def test_dense_map_matches_harness_exactly(tmp_path):
    manifest = _write_slice(tmp_path)
    qmap, dense = build_dense_map(tmp_path, manifest)
    h_qmap, h_dense = load_dense_map(WCfg(slice_dir=tmp_path), manifest)
    assert qmap == h_qmap == {10: 0, 20: 1, 30: 2, 99: 3, 88: 4}
    assert dense == h_dense == [10, 20, 30, 99, 88]  # dup Q10 kept at ordinal 0


def test_shard_paths_dedupe_order_preserving(tmp_path):
    manifest = _write_slice(tmp_path)
    assert shard_paths(manifest, "entities") == [
        "entities-00000.jsonl",
        "entities-00001.jsonl",
    ]


def test_p31_dense_remap_drops_out_of_slice_types(tmp_path):
    manifest = _write_slice(tmp_path)
    qmap, _ = build_dense_map(tmp_path, manifest)
    p31 = load_p31_dense(tmp_path, manifest, qmap)
    assert p31[0] == [3]  # Q10: type Q99 -> dense 3
    assert p31[2] == [4]  # Q30: Q88 -> dense 4; out-of-slice Q555 dropped
    assert p31[3] == [] and p31[4] == []
    # parity with the harness's load_types (which stores only non-empty sets)
    h_types = load_types(WCfg(slice_dir=tmp_path), manifest, qmap)
    for e, ts in h_types.items():
        assert set(p31[e]) == ts


def test_kept_edges_drop_dangling_and_match_typed_adj(tmp_path):
    manifest = _write_slice(tmp_path)
    qmap, _ = build_dense_map(tmp_path, manifest)
    stats: dict = {}
    kept = list(iter_kept_edges(tmp_path, manifest, qmap, stats))
    assert kept == KEPT_DENSE
    assert stats == {"kept": 5, "dropped": 2}  # dangling dst + dangling src
    # parity with the harness's adjacency (flattened)
    adj = load_typed_adj(WCfg(slice_dir=tmp_path), manifest, qmap)
    flat = [(s, p, d) for s, lst in adj.items() for (p, d) in lst]
    assert sorted(flat) == sorted(kept)


# --------------------------------------------------------------------------- #
# generated SQL surface
# --------------------------------------------------------------------------- #
def _script(tmp_path, *, force=False, dialect="fork") -> tuple[str, dict]:
    manifest = _write_slice(tmp_path)
    stats: dict = {}
    sql = "".join(
        iter_load_sql(
            tmp_path,
            manifest,
            _emb(),
            table="entities",
            dim=DIM,
            force=force,
            stats=stats,
            dialect=dialect,
        )
    )
    return sql, stats


def _norm(v):
    vals = [float(x) for x in v]
    n = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / n for x in vals]


@pytest.mark.parametrize("dialect", ["fork", "stock"])
def test_sql_table_and_copy_rows(tmp_path, dialect):
    sql, stats = _script(tmp_path, dialect=dialect)
    assert stats == {
        "entities": 5,
        "edges_kept": 5,
        "edges_dropped_dangling": 2,
        # P31, P50, P279 — P361's only edge was dangling, so no type registers
        "distinct_properties": 3,
    }
    assert "CREATE TABLE entities (" in sql
    assert "id        bigint PRIMARY KEY" in sql
    assert "qid       bigint NOT NULL" in sql
    assert "P31       int[] NOT NULL DEFAULT '{}'" in sql
    assert "COPY entities (id, qid, P31, embedding) FROM stdin;" in sql
    if dialect == "stock":
        # pgvector leg: vector(dim) column + `vector` extension (not vectordb)
        assert f"embedding vector({DIM})" in sql
        assert "CREATE EXTENSION IF NOT EXISTS vector;" in sql
        assert "vectordb" not in sql
        vec_open = "["  # pgvector bracket literal
    else:
        # fork leg: MSVBASE float8[] column + vectordb extension
        assert f"embedding float8[{DIM}]" in sql
        assert "CREATE EXTENSION IF NOT EXISTS vectordb;" in sql
        vec_open = "{"  # Postgres float8[] brace literal
    # row 0: dense 0 / Q10 / P31 {3} / normalized vector
    emb = _emb()
    row0 = entity_copy_row(0, 10, [3], _norm(emb[0]), dialect)
    assert row0 in sql
    assert row0.startswith(f"0\t10\t{{3}}\t{vec_open}")
    assert math.isclose(sum(x * x for x in _norm(emb[0])), 1.0, rel_tol=1e-6)


def test_sql_graph_surface(tmp_path):
    sql, _ = _script(tmp_path)
    # (b) vertex upsert in emission order with vid == ordinal assert
    assert "graph_store.gph_upsert_vertex(r.qid)" in sql
    assert "DENSE-VID CONTRACT BROKEN" in sql
    assert "ORDER BY id" in sql
    # (c) edge-type dictionary + typed insert by verified dense vid
    assert "graph_store.register_edge_type('P' || pid)" in sql
    assert "graph_store.gph_insert_edge(e.src, e.dst, m.type_id)" in sql
    assert "ORDER BY e.src" in sql
    # distinct properties staged: only KEPT edges register a type (31, 50, 279)
    assert "\n31\n" in sql and "\n50\n" in sql and "\n279\n" in sql
    assert "\n361\n" not in sql  # its only edge was dangling
    # kept edges staged in dense space; the dangling rows never appear
    for s, p, d in KEPT_DENSE:
        assert f"{s}\t{p}\t{d}\n" in sql
    # tab-bounded: "555" can legitimately occur inside a float literal's digits
    assert "\t361\t" not in sql
    assert "\t555\n" not in sql and "555\t" not in sql
    # native-count asserts
    assert "gph_edge_count" in sql and "gph_vertex_count" in sql


@pytest.mark.parametrize("dialect", ["fork", "stock"])
def test_sql_hnsw_build_and_health_probe(tmp_path, dialect):
    sql, _ = _script(tmp_path, dialect=dialect)
    if dialect == "stock":
        # pgvector hnsw AM: vector_l2_ops opclass + pinned m / ef_construction
        assert "CREATE INDEX entities_hnsw ON entities USING hnsw " in sql
        assert "(embedding vector_l2_ops) WITH (m = 16, ef_construction = 64);" in sql
    else:
        # fork MSVBASE hnsw AM: dimension / distmethod reloptions
        assert "CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)" in sql
        assert f"WITH (dimension = {DIM}, distmethod = l2_distance);" in sql
    # health probe: top-k on a loaded row's own vector (row 0, normalized)
    probe = vec_literal(_norm(_emb()[0]), dialect)
    assert f"ORDER BY embedding <-> '{probe}' LIMIT {N}" in sql  # k = min(10, N) = 5
    assert "HNSW health probe returned" in sql


def test_sql_force_resets_store_nonforce_does_not(tmp_path):
    forced, _ = _script(tmp_path, force=True)
    assert "DROP TABLE IF EXISTS entities;" in forced
    assert "DROP EXTENSION IF EXISTS graph_store_am CASCADE;" in forced
    plain, _ = _script(tmp_path, force=False)
    assert "DROP EXTENSION" not in plain
    assert "DROP TABLE" not in plain.replace("DROP TABLE edge_stage", "")
    assert "CREATE EXTENSION IF NOT EXISTS graph_store_am;" in plain


def test_sql_rejects_short_embeddings(tmp_path):
    manifest = _write_slice(tmp_path)
    short = _emb()[: N - 1]
    with pytest.raises(SystemExit, match="rows < N"):
        list(iter_load_sql(tmp_path, manifest, short, dim=DIM))


# --------------------------------------------------------------------------- #
# transcript parsing (the load-manifest inputs)
# --------------------------------------------------------------------------- #
def test_parse_transcript():
    text = (
        "NOTICE: #WDL VERTEX_UPSERT verified=5\n"
        "NOTICE: #WDL HNSW_HEALTH rows=5 OK\n"
        "#WDL FINAL entities=5 edges=5 vertices=5\n"
        "#WDL ETYPE P31=2\n"
        "#WDL ETYPE P50=3\n"
        "#WDL ETYPE P279=4\n"
    )
    got = parse_transcript(text)
    assert got["entities"] == got["edges"] == got["vertices"] == 5
    assert got["hnsw_healthy"] is True
    assert got["edge_type_map"] == {"P31": 2, "P50": 3, "P279": 4}
    assert parse_transcript("nothing")["hnsw_healthy"] is False

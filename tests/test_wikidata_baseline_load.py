"""Host tests for the plan-060 baseline loader — no docker, no live stores.

Drives the pure logic of tools/wikidata_baseline_load over the same synthetic
2-shard slice as the engine-loader tests (duplicate entity id, duplicate manifest
shard path, dangling edges, out-of-slice P31 type) and verifies: the pg rows /
Neo4j statements / Milvus id-alignment all live in the harness's dense-id space,
dangling edges are dropped, the per-type relationship Cypher matches the documented
convention, and the edge count is IDENTICAL to the engine loader's (the
engine_edges == neo4j_edges parity the publication gate enforces). Store clients
(pymilvus / neo4j / psycopg) are lazily imported by the loaders and never touched here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_h2h import WCfg, load_dense_map  # noqa: E402
from tools.wikidata_baseline_load import (  # noqa: E402
    CONSTRAINT_CYPHER,
    NODE_CREATE_CYPHER,
    edge_cypher,
    edges_by_pid,
    node_rows,
    pg_create_statements,
    pg_rows,
    pgvector_literal,
)
from tools.wikidata_engine_load import (  # noqa: E402
    build_dense_map,
    iter_kept_edges,
    load_p31_dense,
)

# Same fixture as tests/test_wikidata_engine_load.py: dense Q10->0 Q20->1 Q30->2
# Q99->3 Q88->4; Q555 dangling; duplicate Q10 row and duplicate shard descriptor.
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
EDGES0 = [(10, 31, 99), (10, 50, 20), (20, 31, 99), (10, 361, 555)]
EDGES1 = [(99, 279, 88), (30, 31, 88), (555, 31, 10)]
CLAIMS0 = [
    {"id": 10, "P31": [99]},
    {"id": 20, "P31": [99]},
    {"id": 30, "P31": [88, 555]},
]
CLAIMS1 = [{"id": 99, "P31": []}, {"id": 10, "P31": [99]}, {"id": 88, "P31": []}]


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
                    {"path": "entities-00001.jsonl"},  # duplicate descriptor
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


def test_pg_rows_in_dense_space(tmp_path):
    manifest = _write_slice(tmp_path)
    qmap, dense = build_dense_map(tmp_path, manifest)
    # the baseline loader's map IS the harness's map (shared implementation)
    h_qmap, h_dense = load_dense_map(WCfg(slice_dir=tmp_path), manifest)
    assert (qmap, dense) == (h_qmap, h_dense)
    p31 = load_p31_dense(tmp_path, manifest, qmap)
    rows = list(pg_rows(dense, p31))
    assert rows == [
        (0, 10, [3]),
        (1, 20, [3]),
        (2, 30, [4]),  # out-of-slice type Q555 dropped by the dense remap
        (3, 99, []),
        (4, 88, []),
    ]


def test_pg_create_statements():
    stmts = pg_create_statements("wd_entity", 4)
    # the harness's run_baseline contract: bigint[] p31 probe + pgvector '<=>' rerank
    assert stmts[0] == "CREATE EXTENSION IF NOT EXISTS vector"
    assert "CREATE TABLE wd_entity (" in stmts[1]
    assert "id bigint PRIMARY KEY" in stmts[1]
    assert "qid bigint NOT NULL" in stmts[1]
    assert "p31 bigint[] NOT NULL DEFAULT '{}'" in stmts[1]
    assert "embedding vector(4)" in stmts[1]
    assert stmts[2] == "CREATE INDEX wd_entity_p31_gin ON wd_entity USING gin (p31)"


def test_pgvector_literal():
    assert pgvector_literal([0.5, -1.0]) == "[0.5,-1.0]"


def test_node_rows_dense_string_ids(tmp_path):
    # `id` is a STRING property — run_baseline binds `a.id IN $ids` with str ids
    manifest = _write_slice(tmp_path)
    _, dense = build_dense_map(tmp_path, manifest)
    assert list(node_rows(dense)) == [
        {"id": "0", "qid": 10},
        {"id": "1", "qid": 20},
        {"id": "2", "qid": 30},
        {"id": "3", "qid": 99},
        {"id": "4", "qid": 88},
    ]


def test_edges_by_pid_drops_dangling_and_groups(tmp_path):
    manifest = _write_slice(tmp_path)
    qmap, _ = build_dense_map(tmp_path, manifest)
    groups = edges_by_pid(iter_kept_edges(tmp_path, manifest, qmap))
    assert set(groups) == {31, 50, 279}  # P361's only edge was dangling
    # src/dst stringified to MATCH the string `id` node property
    assert groups[31] == [
        {"src": "0", "dst": "3"},
        {"src": "1", "dst": "3"},
        {"src": "2", "dst": "4"},
    ]
    assert groups[50] == [{"src": "0", "dst": "1"}]
    assert groups[279] == [{"src": "3", "dst": "4"}]
    # no dangling endpoint survives anywhere
    assert all(
        0 <= int(r["src"]) <= 4 and 0 <= int(r["dst"]) <= 4
        for rows in groups.values()
        for r in rows
    )


def test_edge_count_parity_with_engine_loader(tmp_path):
    """The gate's engine_edges == neo4j_edges parity, at the pure-logic level:
    both loaders count edges the same way (BOTH endpoints in-slice, duplicates
    preserved), so what the engine stages equals what Neo4j CREATEs."""
    manifest = _write_slice(tmp_path)
    qmap, _ = build_dense_map(tmp_path, manifest)
    engine_stats: dict = {}
    engine_edges = list(iter_kept_edges(tmp_path, manifest, qmap, engine_stats))
    groups = edges_by_pid(iter_kept_edges(tmp_path, manifest, qmap))
    neo4j_edges = sum(len(rows) for rows in groups.values())
    assert engine_stats["kept"] == len(engine_edges) == neo4j_edges == 5
    assert engine_stats["dropped"] == 2


def test_cypher_convention_per_type_rels_with_p_property():
    cy = edge_cypher(279)
    assert "CREATE (a)-[:P279 {p: 279}]->(b)" in cy
    assert "MATCH (a:Entity {id: r.src}), (b:Entity {id: r.dst})" in cy
    assert "UNWIND $rows AS r" in cy
    assert "CREATE (:Entity {id: r.id, qid: r.qid})" in NODE_CREATE_CYPHER
    assert "REQUIRE e.id IS UNIQUE" in CONSTRAINT_CYPHER

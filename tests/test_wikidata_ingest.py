"""Tests for the plan-060 Wikidata ingest — no network, no large data, no engine.

Drives tools/wikidata_ingest over a tiny in-repo Wikidata JSON-array fixture that
exercises every rule the tri-modal load depends on: item-only filtering (properties
dropped), no-label items dropped from the vector rows, truthy (best-rank) statement
filtering, entity-valued statements -> typed edges, dangling-target edge drops, P31
type + literal claims extraction, both slice modes (prefix / BFS closure), and a
manifest whose counts reconcile with the shard files.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wikidata_ingest import (  # noqa: E402
    SliceSpec,
    _ShardWriter,
    bfs_closure,
    best_rank_statements,
    closure_ids,
    entity_claims,
    entity_edges,
    entity_types,
    ingest,
    parse_entity,
    pid_to_int,
    prefix_ids,
    qid_to_int,
)


def _stmt(prop, target=None, *, rank="normal", time=None, string=None):
    snak = {"snaktype": "value", "property": prop}
    if target is not None:
        snak["datavalue"] = {
            "type": "wikibase-entityid",
            "value": {
                "entity-type": "item",
                "numeric-id": int(target[1:]),
                "id": target,
            },
        }
    elif time is not None:
        snak["datavalue"] = {"type": "time", "value": {"time": time}}
    elif string is not None:
        snak["datavalue"] = {"type": "string", "value": string}
    return {"mainsnak": snak, "type": "statement", "rank": rank}


def _item(qid, label=None, desc=None, claims=None):
    obj = {
        "type": "item",
        "id": qid,
        "labels": {},
        "descriptions": {},
        "claims": claims or {},
    }
    if label is not None:
        obj["labels"]["en"] = {"language": "en", "value": label}
    if desc is not None:
        obj["descriptions"]["en"] = {"language": "en", "value": desc}
    return obj


# Q1 Universe -> P31 Q5 (class), P279 Q2 (galaxy), P361 Q999 (DANGLING; not in dump).
# Q2 Galaxy   -> P31 Q5.
# Q3 Milky Way-> P31 [deprecated Q9, normal Q2] (truthy keeps ONLY Q2), P569 a date literal.
# Q4 Star     -> P31 Q5.
# Q5 class    -> (a bare type node, an edge target).
# Q6          -> NO label/desc (P31 Q5) — a valid edge target but NOT a vector row.
# P100        -> a PROPERTY entity, dropped entirely.
FIXTURE = [
    _item(
        "Q1",
        "Universe",
        "all of space and time",
        {
            "P31": [_stmt("P31", "Q5")],
            "P279": [_stmt("P279", "Q2")],
            "P361": [_stmt("P361", "Q999")],
        },
    ),
    _item(
        "Q2", "Galaxy", "gravitationally bound system", {"P31": [_stmt("P31", "Q5")]}
    ),
    _item(
        "Q3",
        "Milky Way",
        "the galaxy containing the Solar System",
        {
            "P31": [_stmt("P31", "Q9", rank="deprecated"), _stmt("P31", "Q2")],
            "P569": [_stmt("P569", time="+1610-01-07T00:00:00Z")],
        },
    ),
    _item("Q4", "Star", "astronomical object", {"P31": [_stmt("P31", "Q5")]}),
    _item("Q5", "class", "a metaclass", {}),
    _item("Q6", None, None, {"P31": [_stmt("P31", "Q5")]}),
    {
        "type": "property",
        "id": "P100",
        "labels": {"en": {"language": "en", "value": "prop"}},
    },
]


def _write_dump(tmp_path: Path, *, compressed: bool = False) -> Path:
    body = "[\n" + ",\n".join(json.dumps(o) for o in FIXTURE) + "\n]\n"
    suffix = ".json.gz" if compressed else ".json"
    dump = tmp_path / f"wikidata-slice{suffix}"
    if compressed:
        dump.write_bytes(gzip.compress(body.encode("utf-8")))
    else:
        dump.write_text(body, encoding="utf-8")
    return dump


def _load(out: Path):
    manifest = json.loads((out / "manifest.json").read_text())
    entities, edges, claims = [], [], []
    for s in manifest["shards"]["entities"]["files"]:
        entities += [json.loads(x) for x in (out / s["path"]).read_text().splitlines()]
    for s in manifest["shards"]["edges"]["files"]:
        for line in (out / s["path"]).read_text().splitlines():
            src, pid, dst = line.split("\t")
            edges.append((int(src), int(pid), int(dst)))
    for s in manifest["shards"]["claims"]["files"]:
        claims += [json.loads(x) for x in (out / s["path"]).read_text().splitlines()]
    return manifest, entities, edges, claims


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_id_parsing():
    assert qid_to_int("Q42") == 42
    assert qid_to_int("P31") is None
    assert qid_to_int("Q0") is None
    assert qid_to_int("") is None
    assert pid_to_int("P279") == 279
    assert pid_to_int("Q5") is None


def test_best_rank_keeps_only_best_present():
    stmts = [_stmt("P31", "Q9", rank="deprecated"), _stmt("P31", "Q2")]
    kept = best_rank_statements(stmts)
    assert len(kept) == 1
    assert kept[0]["mainsnak"]["datavalue"]["value"]["id"] == "Q2"
    # deprecated-only -> the deprecated statements ARE the best present
    only_dep = [_stmt("P31", "Q9", rank="deprecated")]
    assert len(best_rank_statements(only_dep)) == 1


def test_entity_edges_sorted_deduped_truthy():
    q1 = FIXTURE[0]["claims"]
    assert entity_edges(q1) == [(31, 5), (279, 2), (361, 999)]
    # Q3's deprecated P31->Q9 is truthy-dropped; only ->Q2 survives
    assert entity_edges(FIXTURE[2]["claims"]) == [(31, 2)]


def test_entity_types_and_claims():
    assert entity_types(FIXTURE[0]["claims"]) == [5]
    row = entity_claims(FIXTURE[2]["claims"], literal_props=["P569"])
    assert row["P31"] == [2]
    assert row["P569"] == ["+1610-01-07T00:00:00Z"]
    # a literal prop not requested is absent
    assert "P569" not in entity_claims(FIXTURE[2]["claims"], literal_props=[])


def test_parse_entity_drops_property_and_no_label():
    assert parse_entity(FIXTURE[6]) is None  # P100 property
    assert parse_entity(FIXTURE[5]) is None  # Q6 no label/desc
    ent = parse_entity(FIXTURE[0])
    assert ent is not None and ent.id == 1 and ent.label == "Universe"


def test_bfs_closure_pure():
    adj = {1: [5, 2], 2: [5], 3: [2], 4: [5], 5: []}
    # from Q1 the reachable set is {1,2,5} (Q3/Q4 are disconnected)
    assert bfs_closure([1], adj, target=100) == {1, 2, 5}
    # target cap stops early, deterministically (neighbours consumed in order)
    assert bfs_closure([1], adj, target=2) == {1, 5}


# --------------------------------------------------------------------------- #
# prefix slice — end to end
# --------------------------------------------------------------------------- #
def test_prefix_ids_are_usable_items_only(tmp_path):
    dump = _write_dump(tmp_path)
    kept = prefix_ids(dump, limit=100, lang="en")
    assert kept == {1, 2, 3, 4, 5}  # Q6 (no label) and P100 (property) excluded


def test_prefix_ingest_edges_claims_and_dangling_drop(tmp_path):
    dump = _write_dump(tmp_path)
    out = tmp_path / "corpus"
    ingest(dump, out, SliceSpec(limit=100), literal_props=["P569"])
    manifest, entities, edges, claims = _load(out)
    assert {e["id"] for e in entities} == {1, 2, 3, 4, 5}
    # intra-slice typed edges; the Q1->Q999 dangling statement is dropped
    assert set(edges) == {(1, 31, 5), (1, 279, 2), (2, 31, 5), (3, 31, 2), (4, 31, 5)}
    assert manifest["counts"]["dropped_edges_dangling"] == 1
    # claims carry the type constraint and the requested literal
    q3 = next(c for c in claims if c["id"] == 3)
    assert q3["P31"] == [2] and q3["P569"] == ["+1610-01-07T00:00:00Z"]


def test_manifest_counts_reconcile(tmp_path):
    dump = _write_dump(tmp_path)
    out = tmp_path / "corpus"
    ingest(dump, out, SliceSpec(limit=100))
    manifest, entities, edges, claims = _load(out)
    c = manifest["counts"]
    assert c["entities"] == len(entities) == 5
    assert c["edges"] == len(edges)
    assert c["claims"] == len(claims) == 5
    assert sum(s["rows"] for s in manifest["shards"]["edges"]["files"]) == c["edges"]
    assert manifest["source"] == "wikidata-truthy-json"
    assert manifest["slice"]["mode"] == "prefix"


def test_gzip_streaming(tmp_path):
    dump = _write_dump(tmp_path, compressed=True)
    out = tmp_path / "corpus"
    ingest(dump, out, SliceSpec(limit=100))
    _, entities, edges, _ = _load(out)
    assert len(entities) == 5 and len(edges) == 5


def test_sharding_splits_files(tmp_path):
    dump = _write_dump(tmp_path)
    out = tmp_path / "corpus"
    ingest(dump, out, SliceSpec(limit=100), shard_size=2)
    manifest, entities, _, _ = _load(out)
    files = manifest["shards"]["entities"]["files"]
    assert len(files) == 3  # 5 entities / shard_size 2 -> 2 + 2 + 1
    assert sorted(s["rows"] for s in files) == [1, 2, 2]


# --------------------------------------------------------------------------- #
# BFS slice — connectedness selects the reachable subgraph
# --------------------------------------------------------------------------- #
def test_closure_ids_streaming_selects_connected(tmp_path):
    dump = _write_dump(tmp_path)
    kept, hops = closure_ids(dump, seeds=[1], target=100, lang="en")
    # reachable from Q1: itself, Q5 & Q2 (hop 1), phantom Q999 target kept but never emitted
    assert {1, 2, 5}.issubset(kept)
    assert 3 not in kept and 4 not in kept  # disconnected from the seed
    assert hops >= 1


def test_bfs_ingest_emits_only_connected_entities(tmp_path):
    dump = _write_dump(tmp_path)
    out = tmp_path / "corpus"
    manifest = ingest(dump, out, SliceSpec(seeds=[1], target=100))
    _, entities, edges, _ = _load(out)
    # only the connected, embeddable items are emitted (phantom Q999 yields no row)
    assert {e["id"] for e in entities} == {1, 2, 5}
    assert manifest["slice"]["mode"] == "bfs_closure"
    # the Q1->Q999 edge is dangling within the slice and dropped
    assert (1, 279, 2) in edges
    assert all(dst != 999 for _s, _p, dst in edges)


# --------------------------------------------------------------------------- #
# shard writer monotonic discipline (mirrors wiki_extract)
# --------------------------------------------------------------------------- #
def test_shard_writer_backward_jump_errors_without_truncating(tmp_path):
    out = tmp_path / "corpus"
    out.mkdir()
    w = _ShardWriter(out, shard_size=1)
    w.write(0, {"id": 1}, [], {"id": 1})
    w.write(1, {"id": 2}, [], {"id": 2})
    shard0 = out / "entities-00000.jsonl"
    before = shard0.read_text()
    assert before
    with pytest.raises(ValueError):
        w.write(0, {"id": 3}, [], {"id": 3})
    assert shard0.read_text() == before  # untouched, not truncated

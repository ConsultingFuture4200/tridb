"""Pure-logic tests for the wiki link predictor (DEV-1354).

No embedding, no network, no hnswlib — just the neighbour-minus-existing set
subtraction and the overlap metric on a tiny hand-built fixture. Runs under
`make test`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_linkpredict import (  # noqa: E402
    linked_fraction,
    load_out_edges,
    predicted_unlinked,
    save_embeddings,
)


def _write_corpus(tmp_path, edges):
    """Minimal corpus dir: manifest.json + one tab-separated edge shard."""
    (tmp_path / "edges.tsv").write_text("".join(f"{s}\t{d}\n" for s, d in edges))
    (tmp_path / "manifest.json").write_text(
        json.dumps({"shards": {"edges": {"files": [{"path": "edges.tsv"}]}}})
    )
    return tmp_path


def test_predicted_unlinked_subtracts_self_and_edges_preserving_rank():
    # source 1; cosine-ranked neighbours (nearest first): 1(self), 2(linked),
    # 3(new), 4(new). Redirect targets are already canonicalized into `linked`
    # at extraction time (resolve_edge), so there is nothing extra to subtract.
    neighbors = [1, 2, 3, 4]
    pred = predicted_unlinked(neighbors, self_id=1, linked={2})
    # self and linked gone; nearest-first order preserved
    assert pred == [3, 4]


def test_predicted_unlinked_all_linked_yields_empty():
    assert predicted_unlinked([2, 3], self_id=1, linked={2, 3}) == []


def test_linked_fraction_excludes_self_and_ratios_over_neighbours():
    # neighbours (excl self 1): 2,3,4,5 ; linked: 2,3 -> 2/4
    assert linked_fraction([1, 2, 3, 4, 5], self_id=1, linked={2, 3}) == 0.5


def test_linked_fraction_empty_neighbours_is_zero():
    assert linked_fraction([1], self_id=1, linked=set()) == 0.0


def test_load_out_edges_scopes_to_src_keep(tmp_path):
    # slice keep = {1,2,3}; edge 3->4 leaves the slice (dropped on dst).
    corpus = _write_corpus(tmp_path, [(1, 2), (1, 3), (2, 3), (3, 1), (3, 4)])
    keep = {1, 2, 3}
    # src_keep restricts the loaded adjacency to the sampled sources only.
    scoped = load_out_edges(corpus, keep, {1})
    assert scoped == {1: {2, 3}}  # source 2 and 3 not ingested
    # None => every in-slice source, dst still filtered by keep (3->4 gone).
    full = load_out_edges(corpus, keep, None)
    assert full == {1: {2, 3}, 2: {3}, 3: {1}}


def test_save_embeddings_roundtrip(tmp_path):
    # vecs and ids must round-trip in matching row order, with meta provenance,
    # so the Phase-2 engine load can reuse them instead of re-embedding.
    vecs = np.array([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]], dtype=np.float32)
    ids = [10, 20, 30]
    p = tmp_path / "wiki_emb.npy"
    save_embeddings(p, vecs, ids, {"model": "bge-small", "dim": 2, "count": 3})

    assert np.array_equal(np.load(p), vecs)
    loaded_ids = np.load(tmp_path / "wiki_emb.ids.npy")
    assert loaded_ids.dtype == np.int64
    assert loaded_ids.tolist() == ids
    meta = json.loads((tmp_path / "wiki_emb.meta.json").read_text())
    assert meta["model"] == "bge-small" and meta["count"] == 3

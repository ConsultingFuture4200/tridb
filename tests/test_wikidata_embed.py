"""Tests for the plan-060 Wikidata slice embedder — no network, no model download.

Injects a deterministic dummy encoder into embed_slice and drives it over a synthetic
2-shard slice (including a duplicate id and a no-text entity) to verify the load-bearing
contract: row i == dense id i exactly as bench/wikidata_h2h.load_dense_map assigns it
(first occurrence wins), L2-normalization at write, the empty-text zero row that keeps
id alignment, the meta sidecar, and that bench/wikidata_h2h.load_emb can consume the
output directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_h2h import WCfg, load_dense_map, load_emb  # noqa: E402
from tools.wikidata_embed import (  # noqa: E402
    default_out,
    embed_slice,
    embed_text,
    iter_dense_entities,
)

DIM = 4


class DummyEncoder:
    """Deterministic, unnormalized vectors: row = [len(text), 1, 0, 0].

    Unnormalized on purpose — proves embed_slice normalizes at write. Records every
    batch so tests can assert batching and that empty texts are never encoded.
    """

    model_name = "dummy-encoder"

    def __init__(self):
        self.batches: list[list[str]] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        self.batches.append(list(texts))
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, 0] = float(len(t))
            out[i, 1] = 1.0
        return out


def _expected_row(text: str) -> np.ndarray:
    v = np.array([float(len(text)), 1.0, 0.0, 0.0], dtype=np.float32)
    return v / np.linalg.norm(v)


# Two shards, manifest order. Shard 1 repeats id 10 with DIFFERENT text (first
# occurrence must win) and carries the no-label/no-description entity id 30 (zero row).
SHARD0 = [
    {"id": 10, "label": "Alpha", "description": "first"},
    {"id": 20, "label": "", "description": "desc only"},
]
SHARD1 = [
    {"id": 10, "label": "AlphaDup", "description": "must be ignored"},
    {"id": 30, "label": "", "description": ""},
    {"id": 40, "label": "Delta", "description": ""},
]
# dense id order: 10, 20, 30, 40 -> texts:
EXPECTED_TEXTS = ["Alpha — first", "desc only", "", "Delta"]


def _write_slice(tmp_path: Path) -> Path:
    sd = tmp_path / "slice"
    sd.mkdir()
    for idx, rows in enumerate((SHARD0, SHARD1)):
        (sd / f"entities-{idx:05d}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
    manifest = {
        "shards": {
            "entities": {
                "files": [
                    {"path": "entities-00000.jsonl", "rows": len(SHARD0)},
                    {"path": "entities-00001.jsonl", "rows": len(SHARD1)},
                ]
            }
        }
    }
    (sd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return sd


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_embed_text_joins_and_degrades():
    assert embed_text("Alpha", "first") == "Alpha — first"
    assert embed_text("Alpha", "") == "Alpha"  # no dangling dash
    assert embed_text("", "desc only") == "desc only"
    assert embed_text("", "") == ""


def test_default_out_is_the_wcfg_location():
    assert default_out(Path("data/wikidata_slice")) == Path(
        "data/wikidata_slice/emb/dense_id_aligned.npy"
    )


def test_iter_dense_entities_order_matches_load_dense_map(tmp_path):
    sd = _write_slice(tmp_path)
    manifest = json.loads((sd / "manifest.json").read_text())
    qids = [q for q, _l, _d in iter_dense_entities(sd, manifest)]
    assert qids == [10, 20, 30, 40]  # dup id 10 collapsed, first occurrence wins
    _qmap, dense_to_qid = load_dense_map(WCfg(slice_dir=sd), manifest)
    assert qids == dense_to_qid  # byte-for-byte the harness's dense id space


# --------------------------------------------------------------------------- #
# embed_slice — alignment, dedupe, normalization, zero row, sidecar
# --------------------------------------------------------------------------- #
def test_embed_slice_rows_are_dense_id_aligned(tmp_path):
    sd = _write_slice(tmp_path)
    out = tmp_path / "emb.npy"
    enc = DummyEncoder()
    meta = embed_slice(sd, out, enc, dim=DIM)
    emb = np.load(out, mmap_mode="r")
    assert emb.shape == (4, DIM) and emb.dtype == np.float32
    for row, text in enumerate(EXPECTED_TEXTS):
        if text:
            np.testing.assert_allclose(emb[row], _expected_row(text), rtol=1e-6)
    assert meta["rows"] == 4


def test_embed_slice_duplicate_id_first_occurrence_wins(tmp_path):
    sd = _write_slice(tmp_path)
    out = tmp_path / "emb.npy"
    enc = DummyEncoder()
    embed_slice(sd, out, enc, dim=DIM)
    encoded = [t for b in enc.batches for t in b]
    assert "Alpha — first" in encoded
    assert all("AlphaDup" not in t for t in encoded)  # the shard-1 dup never embeds


def test_embed_slice_zero_row_keeps_alignment_and_skips_encoder(tmp_path):
    sd = _write_slice(tmp_path)
    out = tmp_path / "emb.npy"
    enc = DummyEncoder()
    embed_slice(sd, out, enc, dim=DIM)
    emb = np.load(out)
    assert np.all(emb[2] == 0.0)  # id 30: no label/description -> zero row, row KEPT
    assert emb[3, 0] != 0.0  # id 40 landed on row 3, not shifted onto row 2
    assert all(t != "" for b in enc.batches for t in b)  # "" never sent to the encoder


def test_embed_slice_normalizes_at_write(tmp_path):
    sd = _write_slice(tmp_path)
    out = tmp_path / "emb.npy"
    embed_slice(sd, out, DummyEncoder(), dim=DIM)
    emb = np.load(out)
    norms = np.linalg.norm(emb, axis=1)
    np.testing.assert_allclose(norms[[0, 1, 3]], 1.0, rtol=1e-6)  # unit rows
    assert norms[2] == 0.0  # the zero row stays zero (unretrievable by cosine)


def test_embed_slice_batching_is_result_invariant(tmp_path):
    sd = _write_slice(tmp_path)
    out1 = tmp_path / "emb1.npy"
    out2 = tmp_path / "emb2.npy"
    embed_slice(sd, out1, DummyEncoder(), dim=DIM, batch=256)
    enc = DummyEncoder()
    embed_slice(sd, out2, enc, dim=DIM, batch=1)
    np.testing.assert_array_equal(np.load(out1), np.load(out2))
    assert len(enc.batches) == 3  # batch=1 -> one flush per non-empty text


def test_embed_slice_rejects_wrong_encoder_shape(tmp_path):
    sd = _write_slice(tmp_path)

    class BadEncoder:
        def encode(self, texts):
            return np.zeros((len(texts), DIM + 1), dtype=np.float32)

    with pytest.raises(ValueError, match="expected"):
        embed_slice(sd, tmp_path / "emb.npy", BadEncoder(), dim=DIM, batch=1)


def test_meta_sidecar(tmp_path):
    sd = _write_slice(tmp_path)
    out = tmp_path / "emb.npy"
    embed_slice(sd, out, DummyEncoder(), dim=DIM)
    meta = json.loads(Path(str(out) + ".meta.json").read_text())
    assert meta["model"] == "dummy-encoder"
    assert meta["dim"] == DIM
    assert meta["rows"] == 4
    assert meta["normalized"] is True
    assert meta["empty_rows_zeroed"] == 1


# --------------------------------------------------------------------------- #
# the harness consumes the output directly
# --------------------------------------------------------------------------- #
def test_load_emb_reads_the_matrix(tmp_path):
    sd = _write_slice(tmp_path)
    out = tmp_path / "emb.npy"
    embed_slice(sd, out, DummyEncoder(), dim=DIM)
    cfg = WCfg(slice_dir=sd, emb_path=out, dim=DIM)
    emb = load_emb(cfg, 4)
    assert emb.shape == (4, DIM)
    np.testing.assert_allclose(emb[0], _expected_row("Alpha — first"), rtol=1e-5)
    # load_emb re-normalizes with an epsilon; the zero row must stay (near) zero
    assert np.linalg.norm(emb[2]) < 1e-6

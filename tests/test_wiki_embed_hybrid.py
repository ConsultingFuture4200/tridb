"""Pure-logic tests for the hybrid wiki embedder (DEV-1354).

No torch, no GPU, no embedding — just the corpus row-layout math, the
cross-file row reader, the shared-checkpoint create/verify plumbing, and the
--emb-in reuse path into wiki_linkpredict. Runs under `make test`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_embed_hybrid import (  # noqa: E402
    create_checkpoint,
    read_rows,
    shard_file_layout,
    verify,
)
from tools.wiki_linkpredict import load_emb_checkpoint  # noqa: E402


def _write_corpus(tmp_path: Path, shards: list[list[dict]]) -> Path:
    files = []
    for i, rows in enumerate(shards):
        name = f"articles-{i:05d}.jsonl"
        (tmp_path / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
        files.append({"path": name, "rows": len(rows)})
    (tmp_path / "manifest.json").write_text(
        json.dumps({"shards": {"articles": {"files": files}}})
    )
    return tmp_path


def test_layout_cumulative_offsets(tmp_path):
    corpus = _write_corpus(
        tmp_path,
        [
            [{"id": i, "title": f"A{i}", "text": "x"} for i in range(3)],
            [{"id": i, "title": f"B{i}", "text": "y"} for i in range(3, 5)],
        ],
    )
    layout, total = shard_file_layout(corpus)
    assert total == 5
    assert layout == [
        ("articles-00000.jsonl", 0, 3, 0),
        ("articles-00001.jsonl", 3, 2, 0),
    ]


def test_read_rows_spans_file_boundary(tmp_path):
    corpus = _write_corpus(
        tmp_path,
        [
            [{"id": i, "title": f"A{i}", "text": "x"} for i in range(3)],
            [{"id": i, "title": f"B{i}", "text": "y"} for i in range(3, 6)],
        ],
    )
    layout, _ = shard_file_layout(corpus)
    # rows [2,5) crosses the file boundary (row 2 in file0, rows 3,4 in file1)
    ids, texts = read_rows(corpus, layout, 2, 5)
    assert ids == [2, 3, 4]
    assert texts[0].startswith("A2.")
    assert texts[1].startswith("B3.")


def test_create_and_verify_roundtrip(tmp_path):
    corpus = _write_corpus(
        tmp_path,
        [[{"id": 100 + i, "title": f"T{i}", "text": "z"} for i in range(4)]],
    )
    out = tmp_path / "emb"
    n, dim, shard_size = 4, 3, 2
    create_checkpoint(out, n, dim, shard_size, "test-model")

    # simulate two workers filling both shards with unit-norm vectors
    layout, _ = shard_file_layout(corpus)
    vectors = np.memmap(
        out / "vectors.f32", dtype=np.float32, mode="r+", shape=(n, dim)
    )
    ids_mm = np.lib.format.open_memmap(out / "ids.i64.npy", mode="r+")
    done = np.memmap(out / "shards.done", dtype=np.uint8, mode="r+")
    for idx in (0, 1):
        s, e = idx * shard_size, min((idx + 1) * shard_size, n)
        sid, _ = read_rows(corpus, layout, s, e)
        v = np.eye(dim, dtype=np.float32)[np.arange(e - s) % dim]  # unit rows
        vectors[s:e] = v
        ids_mm[s:e] = sid
        done[idx] = 1
    vectors.flush()
    ids_mm.flush()
    done.flush()

    verify(out, n, dim, shard_size)  # must not raise

    # --emb-in reuse: load_emb_checkpoint returns aligned vecs + ids
    vecs, ids, model = load_emb_checkpoint(out)
    assert model == "test-model"
    assert ids == [100, 101, 102, 103]
    assert vecs.shape == (4, 3)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0)


def test_verify_rejects_incomplete(tmp_path):
    out = tmp_path / "emb"
    create_checkpoint(out, 4, 3, 2, "test-model")  # no shards marked done
    try:
        verify(out, 4, 3, 2)
    except AssertionError:
        return
    raise AssertionError("verify should reject an incomplete checkpoint")

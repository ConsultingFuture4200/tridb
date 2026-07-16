"""Embed an ingested Wikidata slice into the id-aligned matrix Harness B consumes.

Plan 060 / ADR-0018 (d) — the vector leg of the tri-modal slice. Reads a slice
produced by tools/wikidata_ingest (manifest.json + entities-*.jsonl shards) and writes
one float32 .npy matrix whose ROW ORDER IS THE DENSE ID SPACE: row i == the i-th
entity in emission order across the entities shards, first occurrence winning on a
duplicate id — exactly bench/wikidata_h2h.load_dense_map, which is the order a loader
upserts vids (gph_upsert_vertex, ADR-0013). So row i == dense id i == engine vid ==
table PK, and bench/wikidata_h2h.load_emb can memmap the file directly.

TEXT + MODEL. Embedding source is `label + " — " + description` (ADR-0018 (d)) via the
proven fastembed/BGE path (BAAI/bge-small-en-v1.5, dim 384 — the wiki corpus
convention, tools/wiki_linkpredict). Vectors are L2-NORMALIZED AT WRITE (ADR-0017
B4-interim) so distance is order-equivalent to cosine.

EMPTY ROWS. An entity with neither label nor description gets a ZERO row: ADR-0018 (d)
drops such entities from the vector leg, but the row must still EXIST or every later
dense id would shift off by one. A zero vector is the correct representation — it can
never win a cosine ranking, so it is dropped-from-the-leg while keeping id alignment.
(The ingest already refuses no-label/no-description items, so in practice these rows
only appear on hand-built or degraded slices.)

OUTPUT. `--out` (default <slice>/emb/dense_id_aligned.npy — the WCfg default) via
np.lib.format.open_memmap: constant RAM at any slice size, batched encoding, progress
every ~10k rows, plus a `<out>.meta.json` sidecar {model, dim, rows, normalized}.

CLI:
    python -m tools.wikidata_embed --slice data/wikidata_slice
    python -m tools.wikidata_embed --slice data/wikidata_slice --batch 512 --out emb.npy
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import numpy as np

EMBED_VERSION = "0.1.0"
# Mirrors tools/wiki_linkpredict (the wiki-corpus fastembed convention) and WCfg.dim.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384
DEFAULT_BATCH = 256
LOG_EVERY = 10_000


def default_out(slice_dir: Path) -> Path:
    """The WCfg default location: <slice>/emb/dense_id_aligned.npy."""
    return slice_dir / "emb" / "dense_id_aligned.npy"


def embed_text(label: str, description: str) -> str:
    """The ADR-0018 (d) embedding source: `label + " — " + description`.

    Either side may be empty; a single-sided entity embeds just that side (no dangling
    dash). Both empty -> "" — the caller writes a zero row instead of encoding it.
    """
    if label and description:
        return f"{label} — {description}"
    return label or description


def iter_dense_entities(
    slice_dir: Path, manifest: dict
) -> Iterator[tuple[int, str, str]]:
    """Yield (q_id, label, description) in DENSE ID ORDER over the entities shards.

    Shards in manifest order, lines in file order, first occurrence winning on a
    duplicate id — byte-for-byte the ordering rule of bench/wikidata_h2h.load_dense_map,
    so row i of the emitted matrix is dense id i.
    """
    seen: set[int] = set()
    paths = list(
        dict.fromkeys(s["path"] for s in manifest["shards"]["entities"]["files"])
    )
    for path in paths:
        with (slice_dir / path).open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                obj = json.loads(line)
                q = obj["id"]
                if q in seen:
                    continue
                seen.add(q)
                yield q, obj.get("label", ""), obj.get("description", "")


class FastembedEncoder:
    """Thin fastembed BGE wrapper (onnx, CPU) — mirrors tools/wiki_linkpredict.Embedder.

    Imported lazily so the host tests (which inject a dummy encoder) never need the
    fastembed dependency or a model download.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from fastembed import TextEmbedding  # lazy: host tests never import it

        self.model_name = model_name
        self._m = TextEmbedding(model_name=model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(list(self._m.embed(texts)), dtype=np.float32)


def embed_slice(
    slice_dir: Path,
    out: Path,
    encoder,
    *,
    dim: int = DEFAULT_DIM,
    batch: int = DEFAULT_BATCH,
    log_every: int = LOG_EVERY,
) -> dict:
    """Embed the slice into an id-aligned (rows, dim) float32 .npy at `out`.

    `encoder` is anything with .encode(list[str]) -> (n, dim) array (FastembedEncoder
    in production; a dummy in tests). Two passes over the entities shards: one cheap
    line scan to size the memmap (duplicate ids collapse, so the manifest count is only
    an upper bound), then the batched encode+write pass. Non-empty rows are
    L2-normalized at write; empty-text rows are written as zeros (see module docstring).
    Returns the meta dict (also written to `<out>.meta.json`).
    """
    manifest = json.loads((slice_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = sum(1 for _ in iter_dense_entities(slice_dir, manifest))
    out.parent.mkdir(parents=True, exist_ok=True)
    mm = np.lib.format.open_memmap(out, mode="w+", dtype=np.float32, shape=(rows, dim))

    pending_rows: list[int] = []
    pending_texts: list[str] = []

    def flush() -> None:
        if not pending_texts:
            return
        arr = np.asarray(encoder.encode(pending_texts), dtype=np.float32)
        if arr.shape != (len(pending_texts), dim):
            raise ValueError(
                f"encoder returned {arr.shape}, expected ({len(pending_texts)}, {dim})"
            )
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        np.divide(arr, norms, out=arr, where=norms > 0)  # normalize-at-write, ADR-0017
        mm[np.asarray(pending_rows, dtype=np.int64)] = arr
        pending_rows.clear()
        pending_texts.clear()

    t0 = time.time()
    empty_rows = 0
    for row, (_q, label, description) in enumerate(
        iter_dense_entities(slice_dir, manifest)
    ):
        text = embed_text(label, description)
        if not text:
            mm[row] = 0.0  # keep the row for id alignment; zero == not in the leg
            empty_rows += 1
        else:
            pending_rows.append(row)
            pending_texts.append(text)
            if len(pending_texts) >= batch:
                flush()
        if (row + 1) % log_every == 0:
            rate = (row + 1) / max(time.time() - t0, 1e-9)
            print(
                f"[wikidata_embed] {row + 1:,}/{rows:,} rows ({rate:,.0f} rows/s)",
                flush=True,
            )
    flush()
    mm.flush()

    meta = {
        "tool": "tools/wikidata_embed.py",
        "tool_version": EMBED_VERSION,
        "model": getattr(encoder, "model_name", type(encoder).__name__),
        "dim": dim,
        "rows": rows,
        "normalized": True,
        "empty_rows_zeroed": empty_rows,
        "text": 'label + " — " + description (ADR-0018 (d))',
        "slice": str(slice_dir),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    Path(str(out) + ".meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--slice", type=Path, required=True, help="slice dir (has manifest.json)"
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output .npy (default <slice>/emb/dense_id_aligned.npy)",
    )
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    args = ap.parse_args(argv)
    if args.batch <= 0:
        ap.error("--batch must be positive")
    out = args.out or default_out(args.slice)
    meta = embed_slice(
        args.slice, out, FastembedEncoder(args.model), dim=args.dim, batch=args.batch
    )
    print(
        f"[wikidata_embed] {meta['rows']} rows x {meta['dim']} "
        f"({meta['empty_rows_zeroed']} zero rows) -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

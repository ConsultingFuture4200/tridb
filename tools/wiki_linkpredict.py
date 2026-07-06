"""Link prediction over the offline-wiki corpus — the predictive/connection track (DEV-1354).

Prediction model (lower bound): a candidate link A->B is a pair that is
*semantically close* (high cosine between BGE embeddings) but NOT already linked
by a hyperlink. Concretely, for each article we take its top-k cosine neighbours
and SUBTRACT its existing out-edges and self — the remainder is the ranked set of
"should probably be linked but isn't yet" connections.

Redirect equivalence needs no separate subtraction here: the extractor already
resolves every [[wikilink]] through the redirect map before emitting edges (see
resolve_edge in tools/wiki_extract.py), so an article's out-edges already point at
canonical redirect targets — they are captured by the out-edge subtraction. Redirect
pages are never emitted as ns0 articles, so their alias titles are not in the article
set and could never be neighbours to exclude in the first place.

Signal sanity (reported, not spun): the OVERLAP metric is the fraction of each
article's top-k cosine neighbours that are ALREADY linked out-edges. High overlap
means the embeddings recover the real hyperlink topology; the complement is the
candidate-prediction pool. We report the raw number — a bounded corpus slice
deflates it (most true out-edges point at articles outside the slice), which we
note rather than hide.

Pipeline mirrors the repo's existing patterns: the fastembed BGE Embedder from
tools/hotpot_corpus.py and the hnswlib index usage from bench/recall_decay.py.

SCALE / GATING (honest boundaries):
- This runs on simplewiki (282,900 articles) on CPU, HERE. `--limit` bounds the
  slice for a fast validation run; `--limit 0` embeds the whole corpus.
- Memory envelope (`--limit 0`): the dominant resident structures are the
  normalized fp32 vectors and the hnswlib graph. At enwiki 7.19M x dim=384 that is
  ~11 GB of vectors + a comparable index, plus the id/title lists. A third resident
  structure is the out-edge adjacency (dict[int, set[int]]) — but it is bounded to
  the sampled sources, not the full edge set: load_out_edges only ingests out-edges
  for the `--sample` scored sources (~2000 * avg_degree, a few MB), so it does NOT
  materialize all 232M enwiki edges. All three are bounded and fit the GB10's 128 GB
  coherent unified memory. The encode step has a transient ~2x on the vector array
  (list-of-batches then one np.asarray copy) before the temporaries drop. Caveat:
  `--sample 0` (score every article) loads the full O(E) adjacency — at enwiki that
  is 232M edges as Python int/set members (tens of GB); prefer a bounded --sample.
- Full enwiki (~7M articles) embedding is the Spark/GX10 GPU step — NOT run here.
- The PRODUCTION predictor fuses this vector signal with graph structure via
  tjs_open (the tri-modal join, GX10-gated). This cosine-only predictor is the
  LOWER BOUND: it sees semantic proximity but not multi-hop topology. Do not read
  the numbers here as the fused-engine result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# Reuse the proven fastembed wrapper pattern (tools/hotpot_corpus.py). We keep a
# local copy rather than import it because that module pins dim=768; this
# prototype defaults to the smaller/faster bge-small-384 (spec's storage/speed
# tradeoff) and takes model+dim as CLI flags.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384
TEXT_CHARS = 512  # title + leading body chars fed to the encoder


class Embedder:
    """Thin wrapper around fastembed BGE (onnx, CPU). Mirrors tools/hotpot_corpus."""

    def __init__(self, model_name: str, dim: int):
        from fastembed import TextEmbedding

        self.model_name = model_name
        self.dim = dim
        self._m = TextEmbedding(model_name=model_name)

    def encode(self, texts: list[str], *, batch: int = 256) -> np.ndarray:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), batch):
            out.extend(self._m.embed(texts[i : i + batch]))
        arr = np.asarray(out, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != self.dim:
            raise ValueError(f"expected (*,{self.dim}) embeddings, got {arr.shape}")
        return arr


# --------------------------------------------------------------------------- #
# Pure logic (tested in tests/test_wiki_linkpredict.py — no embedding, no net)
# --------------------------------------------------------------------------- #
def predicted_unlinked(
    neighbors: list[int],
    *,
    self_id: int,
    linked: set[int],
) -> list[int]:
    """Rank-preserving neighbours minus self and existing out-edges.

    `neighbors` is the cosine-ranked neighbour id list (nearest first). The output
    keeps that order — it is the ranked PREDICTED-link list for the source. Redirect
    targets are already canonicalized into `linked` at extraction time (resolve_edge
    in tools/wiki_extract.py), so no extra redirect subtraction is needed here."""
    return [n for n in neighbors if n != self_id and n not in linked]


def linked_fraction(neighbors: list[int], *, self_id: int, linked: set[int]) -> float:
    """Overlap metric: fraction of neighbours (excl. self) already linked out-edges."""
    cand = [n for n in neighbors if n != self_id]
    if not cand:
        return 0.0
    return sum(1 for n in cand if n in linked) / len(cand)


# --------------------------------------------------------------------------- #
# Corpus loading
# --------------------------------------------------------------------------- #
def _embed_text(title: str, body: str) -> str:
    """Title + leading body — the title anchors the entity the graph links on."""
    return f"{title}. {body[:TEXT_CHARS]}".strip()


def load_articles(corpus: Path, limit: int) -> tuple[list[int], list[str], list[str]]:
    """Load up to `limit` articles (0 => all) from the manifest's article shards.

    Returns (ids, titles, embed_texts) in file order."""
    manifest = json.loads((corpus / "manifest.json").read_text())
    shard_files = [s["path"] for s in manifest["shards"]["articles"]["files"]]
    ids: list[int] = []
    titles: list[str] = []
    texts: list[str] = []
    for name in shard_files:
        if limit and len(ids) >= limit:
            break
        with (corpus / name).open() as fh:
            for line in fh:
                if limit and len(ids) >= limit:
                    break
                obj = json.loads(line)
                ids.append(int(obj["id"]))
                titles.append(obj["title"])
                texts.append(_embed_text(obj["title"], obj.get("text", "")))
    return ids, titles, texts


def load_out_edges(
    corpus: Path, keep: set[int], src_keep: set[int] | None = None
) -> dict[int, set[int]]:
    """src_id -> set of dst_ids, restricted to endpoints in `keep` (the slice).

    `out_edges` is only ever consulted for the scored sources, so when `src_keep`
    is given (the sampled source ids) the scan keeps out-edges for those sources
    only. This collapses the resident adjacency from O(E) (232M edges at enwiki
    --limit 0) to O(sample * avg_degree). `src_keep=None` keeps every in-slice
    source (the --sample 0 / score-all path, which legitimately reads them all).
    dst membership always uses the full `keep` set."""
    manifest = json.loads((corpus / "manifest.json").read_text())
    edges: dict[int, set[int]] = {}
    for s in manifest["shards"]["edges"]["files"]:
        with (corpus / s["path"]).open() as fh:
            for line in fh:
                src_s, _, dst_s = line.partition("\t")
                src = int(src_s)
                if src not in keep:
                    continue
                if src_keep is not None and src not in src_keep:
                    continue
                dst = int(dst_s)
                if dst in keep:
                    edges.setdefault(src, set()).add(dst)
    return edges


# --------------------------------------------------------------------------- #
# Embedding artifact (reusable by the Phase-2 engine load — the expensive GPU
# embed runs ONCE; the tri-modal SQL/tjs_open load reads these back, not re-embeds)
# --------------------------------------------------------------------------- #
def save_embeddings(path: Path, vecs: np.ndarray, ids: list[int], meta: dict) -> None:
    """Persist normalized vectors + row->id map as a reusable artifact.

    Writes three sidecars next to `path` (e.g. `wiki_emb.npy`):
      - `wiki_emb.npy`       float32 (N, dim), L2-normalized (unit) rows
      - `wiki_emb.ids.npy`   int64 (N,), row i's article id
      - `wiki_emb.meta.json` provenance (model, dim, count, normalized, corpus)
    Row order is shared across the two .npy files. The engine load reuses these
    instead of re-embedding 7M articles."""
    stem = path.with_suffix("")  # drop the .npy so companions read cleanly
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, vecs)
    np.save(stem.with_name(stem.name + ".ids"), np.asarray(ids, dtype=np.int64))
    stem.with_name(stem.name + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )


# --------------------------------------------------------------------------- #
# Index (mirror bench/recall_decay.py hnswlib usage; cosine via IP on L2-norm)
# --------------------------------------------------------------------------- #
def build_index(vecs: np.ndarray, ids: np.ndarray, *, m: int, efc: int):
    import hnswlib

    idx = hnswlib.Index(space="ip", dim=vecs.shape[1])
    idx.init_index(max_elements=vecs.shape[0], ef_construction=efc, M=m)
    idx.add_items(vecs, ids)
    return idx


def run(corpus: Path, args: argparse.Namespace) -> dict:
    ids, titles, texts = load_articles(corpus, args.limit)
    id_to_title = dict(zip(ids, titles))
    keep = set(ids)
    print(f"[wiki-linkpred] loaded {len(ids)} articles from {corpus}")

    embedder = Embedder(args.model, args.dim)
    print(f"[wiki-linkpred] embedding with {embedder.model_name} (dim={args.dim})...")
    vecs = embedder.encode(texts)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12

    if args.emb_out:
        save_embeddings(
            args.emb_out,
            vecs,
            ids,
            {
                "model": embedder.model_name,
                "dim": args.dim,
                "count": len(ids),
                "normalized": True,
                "corpus": str(corpus),
                "row_order": "row i of the .npy == ids[i] in the .ids.npy sidecar",
            },
        )
        print(f"[wiki-linkpred] wrote embeddings artifact {args.emb_out}")

    # Choose the sources we will score BEFORE loading edges. out_edges is only ever
    # read for these sampled sources, so scope the (up to 232M-edge) adjacency load
    # to them — the full slice is still used for dst membership. --sample 0 scores
    # every source, so it needs every in-slice source's out-edges (src_keep=None).
    rng = np.random.default_rng(args.seed)
    sample_n = len(ids) if args.sample == 0 else min(args.sample, len(ids))
    sample_rows = rng.choice(len(ids), size=sample_n, replace=False)
    src_keep = None if args.sample == 0 else {ids[r] for r in sample_rows}

    print("[wiki-linkpred] loading edges...")
    out_edges = load_out_edges(corpus, keep, src_keep)

    print(f"[wiki-linkpred] building hnswlib index over {len(ids)} vectors...")
    id_arr = np.asarray(ids, dtype=np.int64)
    idx = build_index(vecs, id_arr, m=args.m, efc=args.efc)
    # query k+1 to absorb the self-hit, then trim to k neighbours.
    idx.set_ef(max(2 * (args.k + 1), 64))

    q = vecs[sample_rows]
    labels, dists = idx.knn_query(q, k=args.k + 1)

    overlaps: list[float] = []
    records: list[dict] = []
    for row_i, row in enumerate(sample_rows):
        src_id = ids[row]
        # neighbour ids (nearest first), self dropped, capped at k
        neigh: list[int] = []
        scores: dict[int, float] = {}
        for lab, d in zip(labels[row_i], dists[row_i]):
            lab = int(lab)
            if lab == src_id:
                continue
            neigh.append(lab)
            scores[lab] = 1.0 - float(d)  # ip distance = 1 - cosine
            if len(neigh) >= args.k:
                break
        linked = out_edges.get(src_id, set())
        overlaps.append(linked_fraction(neigh, self_id=src_id, linked=linked))
        pred = predicted_unlinked(neigh, self_id=src_id, linked=linked)
        records.append(
            {
                "id": src_id,
                "title": id_to_title[src_id],
                "n_out_edges_in_slice": len(linked),
                "predicted": [
                    {
                        "id": p,
                        "title": id_to_title.get(p, str(p)),
                        "score": round(scores[p], 4),
                    }
                    for p in pred[: args.top]
                ],
            }
        )

    mean_overlap = float(np.mean(overlaps)) if overlaps else 0.0
    mean_out_edges = (
        float(np.mean([r["n_out_edges_in_slice"] for r in records])) if records else 0.0
    )
    return {
        "source": "simplewiki_full (real, offline-wiki extraction)",
        "corpus": str(corpus),
        "articles_embedded": len(ids),
        "embed_model": embedder.model_name,
        "embed_dim": args.dim,
        "k": args.k,
        "sampled": sample_n,
        "top_per_article": args.top,
        "overlap_metric": {
            "mean_topk_already_linked": round(mean_overlap, 4),
            "mean_out_edges_in_slice": round(mean_out_edges, 4),
            "note": (
                "fraction of top-k cosine neighbours that are already out-edges; "
                "complement is the candidate-prediction pool. Bounded --limit "
                "slices deflate this (most true out-edges leave the slice). "
                "mean_out_edges_in_slice quantifies that deflation: the overlap "
                "ceiling is ~min(k, out_edges_in_slice)/k, so a low in-slice "
                "out-edge count caps how high the overlap can read on a slice."
            ),
        },
        "emb_artifact": str(args.emb_out) if args.emb_out else None,
        "hnsw": {"M": args.m, "ef_construction": args.efc, "space": "ip(cosine)"},
        "predictions": records,
        "gating": (
            "cosine-only LOWER BOUND on CPU/simplewiki; full enwiki embed = "
            "Spark/GX10 GPU; production predictor fuses via tjs_open (GX10-gated)."
        ),
    }


def print_sample(res: dict, n: int) -> None:
    print(f"\n=== predicted (semantically close, NOT-yet-linked) — {n} articles ===")
    for rec in res["predictions"][:n]:
        preds = rec["predicted"]
        head = f"\n[{rec['id']}] {rec['title']}  (out-edges in slice: {rec['n_out_edges_in_slice']})"
        print(head)
        if not preds:
            print("   (top neighbours are all already linked)")
        for p in preds:
            print(f"   -> {p['title']}   (cos={p['score']})")
    print(
        f"\noverlap metric (top-k already linked): "
        f"{res['overlap_metric']['mean_topk_already_linked']:.4f} "
        f"over {res['sampled']} sampled articles"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=Path("data/wiki/simplewiki_full"))
    ap.add_argument(
        "--limit",
        type=int,
        default=30000,
        help="articles to embed (0 => whole corpus)",
    )
    ap.add_argument(
        "--sample",
        type=int,
        default=2000,
        help="articles to score/report (0 => all embedded)",
    )
    ap.add_argument("--k", type=int, default=10, help="cosine neighbours per article")
    ap.add_argument(
        "--top", type=int, default=5, help="predicted links kept per article"
    )
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--efc", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--print-n", type=int, default=15, help="articles to print")
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/out/wiki_linkpred.json")
    )
    ap.add_argument(
        "--emb-out",
        type=Path,
        default=None,
        help="persist normalized embeddings (.npy + .ids.npy + .meta.json) for "
        "the Phase-2 engine load to reuse instead of re-embedding",
    )
    args = ap.parse_args(argv)

    if not (args.corpus / "manifest.json").exists():
        ap.error(f"no manifest.json under {args.corpus}")

    res = run(args.corpus, args)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print_sample(res, args.print_n)
    print(f"\n[wiki-linkpred] wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

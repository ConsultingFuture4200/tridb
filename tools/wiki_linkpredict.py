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


def load_titles(corpus: Path, wanted: set[int]) -> dict[int, str]:
    """id -> title for the given ids (used by the --emb-in reuse path, which loads
    vectors from a checkpoint but still needs human-readable titles for output)."""
    manifest = json.loads((corpus / "manifest.json").read_text())
    out: dict[int, str] = {}
    for s in manifest["shards"]["articles"]["files"]:
        if len(out) >= len(wanted):
            break
        with (corpus / s["path"]).open() as fh:
            for line in fh:
                obj = json.loads(line)
                i = int(obj["id"])
                if i in wanted:
                    out[i] = obj["title"]
    return out


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


class _CuvsIndex:
    """hnswlib-compatible shim around a cuVS CAGRA GPU index (build_cuvs_index).

    Exposes the same knn_query(q, k) -> (id_labels, 1-cosine) contract as the
    hnswlib path so all downstream overlap / subtraction / title logic is reused
    unchanged. The GPU is touched only inside build + knn_query; nothing
    GPU-resident is returned or held on the query path."""

    def __init__(self, index, ids: np.ndarray, build_s: float):
        self._index = index
        self._ids = ids
        self.build_s = build_s

    def set_ef(self, ef: int) -> None:  # hnswlib parity no-op (CAGRA uses itopk_size)
        pass

    def knn_query(self, q: np.ndarray, k: int):
        from cuvs.neighbors import cagra
        from pylibraft.common import device_ndarray

        qd = device_ndarray(np.ascontiguousarray(q, dtype=np.float32))
        sp = cagra.SearchParams(itopk_size=max(4 * k, 256))
        dist, ind = cagra.search(sp, self._index, qd, k)
        rows = np.asarray(ind.copy_to_host())
        d = np.asarray(dist.copy_to_host(), dtype=np.float32)
        labels = self._ids[rows]  # neighbour rows -> article ids
        # unit vectors: sqeuclidean = 2 - 2cos, so sqeuclidean/2 == 1 - cosine,
        # matching the hnswlib 'ip' distance the caller expects (score = 1 - d).
        return labels, d * 0.5


def build_cuvs_index(
    vecs: np.ndarray,
    ids: np.ndarray,
    *,
    graph_degree: int = 32,
    intermediate_graph_degree: int = 64,
):
    """Build a cuVS CAGRA index on the GPU over L2-normalized vectors.

    Vectors are unit-norm, so squared-Euclidean ranks identically to cosine
    (||a-b||^2 = 2 - 2cos). We build with metric='sqeuclidean' (the verified GB10
    path, scripts/spark_gpu_setup.sh + docs/gpu_index_build_v0.1.0.md) and convert
    distances back to cosine at query time. Returns a _CuvsIndex shim."""
    import time

    from cuvs.neighbors import cagra

    data = np.ascontiguousarray(vecs, dtype=np.float32)  # real RAM copy for the build
    params = cagra.IndexParams(
        metric="sqeuclidean",
        graph_degree=graph_degree,
        intermediate_graph_degree=intermediate_graph_degree,
    )
    t0 = time.time()
    index = cagra.build(params, data)
    build_s = time.time() - t0
    print(
        f"[wiki-linkpred] CAGRA build over {data.shape[0]} vectors "
        f"(graph_degree={graph_degree}) in {build_s:.1f}s"
    )
    return _CuvsIndex(index, ids, build_s)


def load_emb_checkpoint(emb_dir: Path) -> tuple[np.ndarray, list[int], str]:
    """Load a wiki_embed_hybrid checkpoint (vectors.f32 + ids.i64.npy + meta.json).

    Returns (vecs (N,dim) float32, ids list, model_name). Vectors are already
    L2-normalized by the embed run (meta.normalized == True). This is the REUSE
    path: the expensive GPU embed runs ONCE via tools/wiki_embed_hybrid; the
    link-prediction overlap metric is then computed cheaply from the artifact."""
    meta = json.loads((emb_dir / "meta.json").read_text())
    n, dim = int(meta["N"]), int(meta["dim"])
    vecs = np.asarray(
        np.memmap(emb_dir / "vectors.f32", dtype=np.float32, mode="r", shape=(n, dim))
    )
    ids = np.load(emb_dir / "ids.i64.npy").tolist()
    return vecs, ids, meta.get("model", "unknown")


def run(corpus: Path, args: argparse.Namespace) -> dict:
    if args.emb_in:
        vecs, ids, model_name = load_emb_checkpoint(args.emb_in)
        titles_by_id = load_titles(corpus, set(ids))
        titles = [titles_by_id.get(i, str(i)) for i in ids]
        id_to_title = dict(zip(ids, titles))
        keep = set(ids)
        print(
            f"[wiki-linkpred] loaded {len(ids)} embeddings from checkpoint "
            f"{args.emb_in} (model={model_name})"
        )
        embed_model = model_name
    else:
        ids, titles, texts = load_articles(corpus, args.limit)
        id_to_title = dict(zip(ids, titles))
        keep = set(ids)
        print(f"[wiki-linkpred] loaded {len(ids)} articles from {corpus}")

        embedder = Embedder(args.model, args.dim)
        print(
            f"[wiki-linkpred] embedding with {embedder.model_name} (dim={args.dim})..."
        )
        vecs = embedder.encode(texts)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
        embed_model = embedder.model_name

    if not args.emb_in and args.emb_out:
        save_embeddings(
            args.emb_out,
            vecs,
            ids,
            {
                "model": embed_model,
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

    id_arr = np.asarray(ids, dtype=np.int64)
    # fetch k + buffer + 1: the +1 absorbs the self-hit, the buffer deepens the
    # predicted-link pool (overlap is still measured over the top-k, below).
    fetch = args.k + args.buffer + 1
    if args.index == "cuvs":
        print(
            f"[wiki-linkpred] building cuVS CAGRA index over {len(ids)} vectors (GPU)..."
        )
        idx = build_cuvs_index(vecs, id_arr, graph_degree=args.graph_degree)
    else:
        print(f"[wiki-linkpred] building hnswlib index over {len(ids)} vectors...")
        idx = build_index(vecs, id_arr, m=args.m, efc=args.efc)
        idx.set_ef(max(2 * fetch, 64))

    q = np.ascontiguousarray(vecs[sample_rows])
    labels, dists = idx.knn_query(q, k=fetch)

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
            scores[lab] = 1.0 - float(d)  # 1 - cosine distance -> cosine
            if len(neigh) >= args.k + args.buffer:
                break
        linked = out_edges.get(src_id, set())
        # overlap: top-k only; predicted: the full k+buffer pool minus linked/self.
        overlaps.append(linked_fraction(neigh[: args.k], self_id=src_id, linked=linked))
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

    # Whole-corpus (emb-in artifact, or --limit 0) => every dst endpoint is
    # in-corpus, so out-edge counts are true out-degrees and the overlap is not
    # slice-deflated. A bounded --limit slice IS deflated (most dsts leave it).
    full_corpus = args.emb_in is not None or args.limit == 0
    mean_overlap = float(np.mean(overlaps)) if overlaps else 0.0
    mean_out_edges = (
        float(np.mean([r["n_out_edges_in_slice"] for r in records])) if records else 0.0
    )
    return {
        "source": f"{corpus.name} (real, offline-wiki extraction)",
        "corpus": str(corpus),
        "articles_embedded": len(ids),
        "embed_model": embed_model,
        "embed_dim": args.dim,
        "k": args.k,
        "buffer": args.buffer,
        "sampled": sample_n,
        "top_per_article": args.top,
        "overlap_metric": {
            "mean_topk_already_linked": round(mean_overlap, 4),
            "mean_out_edges_in_slice": round(mean_out_edges, 4),
            "note": (
                "fraction of top-k cosine neighbours that are already out-edges; "
                "complement is the candidate-prediction pool. "
                + (
                    "Whole-corpus run: every dst endpoint is in-corpus, so "
                    "mean_out_edges_in_slice is the true out-degree and the overlap "
                    "is NOT slice-deflated — it is the genuine fraction of a "
                    "source's global nearest neighbours that it already links out to."
                    if full_corpus
                    else "Bounded --limit slices deflate this (most true out-edges "
                    "leave the slice); mean_out_edges_in_slice quantifies that: the "
                    "overlap ceiling is ~min(k, out_edges_in_slice)/k."
                )
            ),
        },
        "emb_artifact": str(args.emb_out) if args.emb_out else None,
        "index": (
            {
                "backend": "cuvs_cagra",
                "metric": "sqeuclidean (== cosine on unit vectors)",
                "graph_degree": args.graph_degree,
                "build_seconds": round(idx.build_s, 2),
            }
            if args.index == "cuvs"
            else {"M": args.m, "ef_construction": args.efc, "space": "ip(cosine)"}
        ),
        "predictions": records,
        "gating": (
            ("full-corpus GPU CAGRA run; " if args.index == "cuvs" else "CPU run; ")
            + "cosine-only predictor is the LOWER BOUND — it sees semantic "
            "proximity but not multi-hop topology; the production predictor fuses "
            "this vector signal with graph structure via tjs_open (GX10-gated)."
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
    ap.add_argument(
        "--index",
        choices=["hnswlib", "cuvs"],
        default="hnswlib",
        help="ANN backend: hnswlib (CPU, default) or cuvs (GPU CAGRA, GX10-only)",
    )
    ap.add_argument(
        "--buffer",
        type=int,
        default=0,
        help="extra neighbours fetched beyond k for the predicted-link pool; "
        "overlap is always measured over the top-k (default 0)",
    )
    ap.add_argument(
        "--graph-degree",
        type=int,
        default=32,
        help="cuVS CAGRA graph degree (only used with --index cuvs)",
    )
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
    ap.add_argument(
        "--emb-in",
        type=Path,
        default=None,
        help="REUSE a wiki_embed_hybrid checkpoint dir (vectors.f32 + "
        "ids.i64.npy + meta.json) instead of re-embedding; --limit is ignored",
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

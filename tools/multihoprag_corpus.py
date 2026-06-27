"""MultiHopRAG corpus + all-three-modalities builder for the FUSION ABLATION.

The thesis-falsification workload: a real multi-hop QA set where ALL THREE
modalities carry signal, so the 4-way ablation (vector / graph / relational /
fusion) is fair — if fusion doesn't beat the best single modality on recall@k,
the tri-modal thesis is wrong and we want to know.

MultiHopRAG (yixuantt/MultiHopRAG) fits because, unlike HotpotQA, its corpus
carries REAL relational metadata:
  * vector     : article body embedding (BGE-768).
  * graph      : entity-mention edges between articles (embedding-INDEPENDENT:
                 article A -> B when B's title entity appears in A's body/title).
  * relational : per-article {category, source, published_at} + a per-question
                 relational CONSTRAINT derived from the gold evidence (the set of
                 categories/sources and the date span the answer lives in).
Gold = the evidence articles per question (resolved by url, then title).

Measurable-here vs gated: recall is host-side (exact, no engine); the live tjs()
fused-operator latency is GX10/engine-gated as everywhere else.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from tools.fetch_hotpot import _get_json  # reuse the retrying HF rows fetcher

ROWS_API = "https://datasets-server.huggingface.co/rows"
DATASET = "yixuantt/MultiHopRAG"
EMBED_MODEL = "BAAI/bge-base-en-v1.5"
EMBED_DIM = 768
BGE_QUERY = "Represent this sentence for searching relevant passages: "
_WORD = re.compile(r"[a-z0-9]+")
_PROPER = re.compile(r"\b([A-Z][a-zA-Z0-9.\-]+(?:\s+[A-Z][a-zA-Z0-9.\-]+){0,3})\b")


def _page(config: str, split: str, offset: int, length: int) -> dict:
    import urllib.parse

    q = urllib.parse.urlencode(
        {
            "dataset": DATASET,
            "config": config,
            "split": split,
            "offset": offset,
            "length": length,
        }
    )
    return _get_json(f"{ROWS_API}?{q}")


def fetch_all(config: str, split: str, limit: int) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while limit <= 0 or len(rows) < limit:
        want = 100 if limit <= 0 else min(100, limit - len(rows))
        payload = _page(config, split, offset, want)
        page = payload.get("rows", [])
        if not page:
            break
        rows.extend(r["row"] for r in page)
        offset += len(page)
        if len(page) < want:
            break
    return rows


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _year_month(published_at: str) -> int:
    """published_at -> YYYYMM int (the relational scalar), 0 if unparseable."""
    m = re.match(r"(\d{4})-(\d{2})", published_at or "")
    return int(m[1]) * 100 + int(m[2]) if m else 0


def build_corpus(corpus_rows: list[dict]) -> list[dict]:
    """One entity per article: id, title, body, category, source, ym (YYYYMM)."""
    docs = []
    for i, r in enumerate(corpus_rows):
        docs.append(
            {
                "id": i,
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "body": (r.get("body") or "")[:4000],
                "category": r.get("category") or "",
                "source": r.get("source") or "",
                "ym": _year_month(r.get("published_at") or ""),
            }
        )
    return docs


def _entities(text: str, *, min_len: int = 4, max_per_doc: int = 60) -> set[str]:
    """Salient named entities = multi-word OR >=min_len proper-noun phrases.

    Embedding-INDEPENDENT: pure surface capitalization (people/orgs/products like
    'Sam Bankman-Fried', 'FTX'), the links MultiHopRAG's multi-hop questions chain."""
    ents: dict[str, int] = {}
    for m in _PROPER.finditer(text or ""):
        e = " ".join(_tokens(m.group(1)))
        if len(e) >= min_len and (" " in e or len(e) >= 4):
            ents[e] = ents.get(e, 0) + 1
    # keep the most-mentioned (specific) entities per doc
    return set(sorted(ents, key=lambda e: (-ents[e], -len(e)))[:max_per_doc])


def build_entity_graph(
    docs: list[dict], *, min_shared: int = 2, df_drop: float = 0.40
) -> list[tuple[int, int]]:
    """Edges A<->B when articles share >= min_shared named entities (both directions).

    News articles aren't linked by title-in-body (titles are headlines); the real
    cross-article link is a shared entity. We drop corpus-generic entities (document
    frequency > df_drop) so 'Monday'/'The'-style noise doesn't connect everything."""
    n = len(docs)
    doc_ents = [_entities(d["title"] + " " + d["body"]) for d in docs]
    # document frequency -> drop overly common entities
    df: dict[str, int] = defaultdict(int)
    for es in doc_ents:
        for e in es:
            df[e] += 1
    cutoff = max(2, int(df_drop * n))
    inv: dict[str, list[int]] = defaultdict(list)
    for did, es in enumerate(doc_ents):
        for e in es:
            if df[e] <= cutoff:
                inv[e].append(did)
    # count shared entities per doc pair via the inverted index
    pair: dict[tuple[int, int], int] = defaultdict(int)
    for docs_with_e in inv.values():
        if len(docs_with_e) < 2:
            continue
        for i in range(len(docs_with_e)):
            for j in range(i + 1, len(docs_with_e)):
                a, b = docs_with_e[i], docs_with_e[j]
                pair[(a, b)] += 1
    edges: list[tuple[int, int]] = []
    for (a, b), c in pair.items():
        if c >= min_shared:
            edges.append((a, b))
            edges.append((b, a))  # undirected -> both out-edges for traversal
    return edges


def attach_questions(qa_rows: list[dict], docs: list[dict]) -> list[dict]:
    """Resolve each question's gold evidence -> corpus doc ids (by url, then title)
    and derive its relational constraint (gold categories/sources + ym span)."""
    by_url = {d["url"]: d["id"] for d in docs if d["url"]}
    by_title = {" ".join(_tokens(d["title"])): d["id"] for d in docs if d["title"]}
    out = []
    for qi, r in enumerate(qa_rows):
        ev = r.get("evidence_list") or []
        gold_ids, cats, srcs, yms = [], set(), set(), []
        for e in ev:
            did = by_url.get(e.get("url") or "")
            if did is None:
                did = by_title.get(" ".join(_tokens(e.get("title") or "")))
            if did is not None:
                gold_ids.append(did)
            if e.get("category"):
                cats.add(e["category"])
            if e.get("source"):
                srcs.add(e["source"])
            ym = _year_month(e.get("published_at") or "")
            if ym:
                yms.append(ym)
        gold_ids = sorted(set(gold_ids))
        out.append(
            {
                "qid": qi,
                "query": r.get("query") or "",
                "answer": r.get("answer") or "",
                "question_type": r.get("question_type") or "",
                "gold_ids": gold_ids,
                "n_gold": len(gold_ids),
                # relational constraint derived from gold evidence metadata:
                "rel_categories": sorted(cats),
                "rel_sources": sorted(srcs),
                "rel_ym_min": min(yms) if yms else 0,
                "rel_ym_max": max(yms) if yms else 0,
            }
        )
    return out


def embed(texts: list[str], model_name: str = EMBED_MODEL) -> np.ndarray:
    from fastembed import TextEmbedding

    m = TextEmbedding(model_name=model_name)
    out = []
    for i in range(0, len(texts), 256):
        out.extend(m.embed(texts[i : i + 256]))
    arr = np.asarray(out, dtype=np.float32)
    arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
    return arr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build MultiHopRAG ablation corpus.")
    ap.add_argument("--questions", type=int, default=300, help="0 = all")
    ap.add_argument("--outdir", type=Path, default=Path("data/multihoprag"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument(
        "--reuse-embeddings",
        action="store_true",
        help="reuse existing corpus_emb/query_emb .npy (skip the BGE step)",
    )
    args = ap.parse_args(argv)

    print("[mhrag] fetching corpus + QA (HF rows API)...")
    corpus_rows = fetch_all("corpus", "train", 0)
    qa_rows = fetch_all("MultiHopRAG", "train", args.questions)
    docs = build_corpus(corpus_rows)
    edges = build_entity_graph(docs)
    questions = attach_questions(qa_rows, docs)
    resolved = sum(1 for q in questions if q["n_gold"] > 0)
    print(
        f"[mhrag] corpus={len(docs)} docs, {len(edges)} entity edges, "
        f"{len(questions)} questions ({resolved} with >=1 gold resolved)"
    )

    cpath, qpath = args.outdir / "corpus_emb.npy", args.outdir / "query_emb.npy"
    args.outdir.mkdir(parents=True, exist_ok=True)
    if args.reuse_embeddings and cpath.exists() and qpath.exists():
        corpus_emb, query_emb = np.load(cpath), np.load(qpath)
        if corpus_emb.shape[0] != len(docs) or query_emb.shape[0] != len(questions):
            raise SystemExit(
                f"--reuse-embeddings shape mismatch: emb {corpus_emb.shape[0]}/"
                f"{query_emb.shape[0]} vs docs {len(docs)}/q {len(questions)} — re-embed"
            )
        print(f"[mhrag] reused embeddings {corpus_emb.shape} + {query_emb.shape}")
    else:
        print(
            f"[mhrag] embedding {len(docs)} bodies + {len(questions)} queries (BGE-768)..."
        )
        corpus_emb = embed([f"{d['title']}. {d['body']}" for d in docs])
        query_emb = embed([BGE_QUERY + q["query"] for q in questions])
        np.save(cpath, corpus_emb)
        np.save(qpath, query_emb)
    manifest = {
        "source": "yixuantt/MultiHopRAG",
        "embed_model": EMBED_MODEL,
        "dim": EMBED_DIM,
        "entities": len(docs),
        "edges": len(edges),
        "k": args.k,
        "corpus_emb_path": str(args.outdir / "corpus_emb.npy"),
        "query_emb_path": str(args.outdir / "query_emb.npy"),
        "docs": [
            {
                "id": d["id"],
                "title": d["title"],
                "category": d["category"],
                "source": d["source"],
                "ym": d["ym"],
            }
            for d in docs
        ],
        "_edges": [[int(s), int(t)] for s, t in edges],
        "questions": questions,
    }
    (args.outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    print(f"[mhrag] wrote {args.outdir}/manifest.json + embeddings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

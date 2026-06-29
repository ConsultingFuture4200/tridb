"""Embed the HotpotQA dev slice and emit the benchmark manifest (Plan 015, Phase 3).

Pipeline: questions JSON (tools/fetch_hotpot) -> real mention graph
(tools/build_wiki_graph) -> REAL 768-d embeddings (BGE-base via fastembed/onnx,
CPU, no torch) -> a manifest that is a DROP-IN for the existing live harness
(same `tools.bench_corpus.build_sql` #BENCH SQL + the real_corpus `_entities`/
`_edges` carrier convention), plus the per-question gold the accuracy report needs.

Embeddings are written to .npy (corpus_emb / query_emb) rather than inlined in the
manifest JSON (4936x768 floats would bloat it). The manifest references them by
path. The live SQL emitter (:func:`emit_bench_sql`) is provided for the GX10/
engine-gated Phase-5 run; the host-side accuracy report consumes the .npy directly.

Encoder is pinned in the manifest (model id + the BGE query instruction) so the
numbers are reproducible. dim=768 satisfies the GTM "real embeddings, dim>=768".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from tools.build_wiki_graph import HotpotCorpus, build_graph

# BGE-base-en-v1.5: asymmetric retrieval — passages embedded as-is, queries
# prefixed with the model's recommended instruction. Pinned for reproducibility.
EMBED_MODEL = "BAAI/bge-base-en-v1.5"
EMBED_DIM = 768
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


class Embedder:
    """Thin wrapper around fastembed BGE (onnx, CPU). Swappable for tests."""

    def __init__(self, model_name: str = EMBED_MODEL):
        from fastembed import TextEmbedding

        self.model_name = model_name
        self._m = TextEmbedding(model_name=model_name)

    def encode(self, texts: list[str], *, batch: int = 256) -> np.ndarray:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), batch):
            out.extend(self._m.embed(texts[i : i + batch]))
        arr = np.asarray(out, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != EMBED_DIM:
            raise ValueError(f"expected (*,{EMBED_DIM}) embeddings, got {arr.shape}")
        return arr


def _passage_text(title: str, body: str) -> str:
    """Embed title + body — the title carries the entity the mention graph links on."""
    return f"{title}. {body}".strip()


def embed_corpus(g: HotpotCorpus, embedder: Embedder) -> tuple[np.ndarray, np.ndarray]:
    """Return (corpus_emb [n_para,768], query_emb [n_q,768]), L2-normalized."""
    passages = [_passage_text(p.title, p.text) for p in g.paragraphs]
    queries = [BGE_QUERY_INSTRUCTION + q["question"] for q in g.questions]
    corpus_emb = embedder.encode(passages)
    query_emb = embedder.encode(queries)
    # normalize so dot product == cosine (BGE is trained for cosine retrieval).
    corpus_emb /= np.linalg.norm(corpus_emb, axis=1, keepdims=True) + 1e-12
    query_emb /= np.linalg.norm(query_emb, axis=1, keepdims=True) + 1e-12
    return corpus_emb, query_emb


def build_manifest(
    g: HotpotCorpus,
    *,
    k: int,
    embed_model: str,
    corpus_emb_path: str,
    query_emb_path: str,
    source_slice: str,
) -> dict:
    """Real_corpus-compatible manifest + GraphRAG gold/answer carriers.

    Public fields mirror tools/bench_corpus.py (entities/dim/edges/k/queries);
    `_edges` carries the REAL mention graph; per-question gold/answer ride
    alongside for the accuracy report. Embeddings live in the referenced .npy.
    """
    return {
        "source": "hotpotqa-distractor-devslice",
        "source_slice": source_slice,
        "graph_kind": "real-title-mention (embedding-independent proxy for wiki hyperlinks)",
        "embed_model": embed_model,
        "embed_dim": EMBED_DIM,
        "bge_query_instruction": BGE_QUERY_INSTRUCTION,
        # public corpus metadata
        "entities": len(g.paragraphs),
        "dim": EMBED_DIM,
        "edges": len(g.edges),
        "num_queries": len(g.questions),
        "k": k,
        # embeddings referenced by path (kept out of JSON)
        "corpus_emb_path": corpus_emb_path,
        "query_emb_path": query_emb_path,
        # paragraph metadata for the reader + title mapping
        "paragraphs": [
            {"id": p.id, "title": p.title, "text": p.text} for p in g.paragraphs
        ],
        "_edges": [[int(s), int(d)] for s, d in g.edges],
        "questions": [
            {
                "qid": q["qid"],
                "hotpot_id": q["hotpot_id"],
                "question": q["question"],
                "answer": q["answer"],
                "type": q["type"],
                "level": q["level"],
                "gold_ids": q["gold_ids"],
                "gold_titles": q["gold_titles"],
            }
            for q in g.questions
        ],
    }


def emit_bench_sql(
    manifest: dict, corpus_emb: np.ndarray, query_emb: np.ndarray, k: int
) -> str:
    """Emit the canonical #BENCH SQL for the GX10/engine-gated live tjs() run.

    Reuses tools.bench_corpus.build_sql (single source of truth). HotpotQA has no
    time predicate, so every entity gets ts=0 and each query a full window — the
    canonical relational filter becomes a pass-through, leaving the vector+graph
    legs intact. Each query's `src` is its vector-seed (the corpus paragraph
    nearest the question), exactly the seed the host retriever uses, and the
    query embedding is inlined so the engine runs the real similarity leg.
    UNBUILT-HERE: this drives scripts/bench_graphrag.sh on-target.
    """
    from tools.bench_corpus import build_sql

    n = len(manifest["paragraphs"])
    entities = [(i, 0, corpus_emb[i].tolist()) for i in range(n)]
    full_window = list(range(0, 1))  # ts==0 for all -> filter passes everything
    queries = []
    for q in manifest["questions"]:
        qv = query_emb[q["qid"]]
        src = int(np.argmax(corpus_emb @ qv))  # vector-seed paragraph
        queries.append(
            {
                "qid": q["qid"],
                "src": src,
                "embedding": qv.tolist(),
                "window": full_window,
            }
        )
    qmani = dict(manifest)
    qmani.update({"hubs": 0, "fanout": 0, "entities": n, "k": k, "queries": queries})
    return build_sql(
        manifest=qmani,
        entities=entities,
        edges=[tuple(e) for e in manifest["_edges"]],
        source="tools/hotpot_corpus.py",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Embed HotpotQA slice + emit manifest.")
    ap.add_argument("--slice", type=Path, default=Path("data/hotpot/dev_slice.json"))
    ap.add_argument("--outdir", type=Path, default=Path("data/hotpot"))
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args(argv)

    questions = json.loads(args.slice.read_text())["questions"]
    g = build_graph(questions)
    print(
        f"[hotpot_corpus] corpus={len(g.paragraphs)} paras, "
        f"{len(g.edges)} real mention edges, {len(g.questions)} questions"
    )

    embedder = Embedder()
    print(f"[hotpot_corpus] embedding with {embedder.model_name} (dim={EMBED_DIM})...")
    corpus_emb, query_emb = embed_corpus(g, embedder)

    args.outdir.mkdir(parents=True, exist_ok=True)
    cpath = args.outdir / "corpus_emb.npy"
    qpath = args.outdir / "query_emb.npy"
    np.save(cpath, corpus_emb)
    np.save(qpath, query_emb)

    manifest = build_manifest(
        g,
        k=args.k,
        embed_model=embedder.model_name,
        corpus_emb_path=str(cpath),
        query_emb_path=str(qpath),
        source_slice=str(args.slice),
    )
    mpath = args.outdir / "manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False))
    print(
        f"[hotpot_corpus] wrote {mpath} + {cpath.name} {corpus_emb.shape} "
        f"+ {qpath.name} {query_emb.shape}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

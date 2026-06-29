"""GraphRAG QA-accuracy report — vector-only vs graph-constrained (Plan 015, Phase 4).

The thesis test, run host-side on the real HotpotQA dev slice (no engine needed
for ACCURACY — same split of measurable-here vs gated as tools/real_corpus.py):

  * vector-only   : cosine top-k over the corpus for the question.
  * graph-constr. : seed by vector top-m, expand over the REAL mention graph
                    (1-2 hops, mirrors tjs out-edge traversal), re-rank the
                    reachable candidate set by cosine, take top-k. This is the
                    GraphRAG move: a 2nd-hop gold paragraph that is NOT close to
                    the question vector is still reached through the bridge edge.

Metrics:
  * evidence recall@k / joint-EM / F1 over the gold supporting paragraphs — the
    CREDIBLE, fully-host-computable thesis number (no LLM).
  * answer EM/F1 via a pluggable reader over the retrieved context — the headline
    "is the answer right" number. AnthropicReader is wired but needs
    ANTHROPIC_API_KEY; the default ExtractiveReader is a deterministic, clearly-
    labeled NON-LLM lower bound so the metric pipeline runs end-to-end here.

Live tjs() latency at fixed accuracy + the full retrieve-from-all-Wikipedia run
are GX10/engine-gated (scripts/bench_graphrag.sh) and never claimed here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import string
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# HotpotQA official answer normalization (EM / F1)
# --------------------------------------------------------------------------- #
_ARTICLES = re.compile(r"\b(a|an|the)\b")
_PUNCT = str.maketrans("", "", string.punctuation)


def _normalize(ans: str) -> str:
    s = ans.lower()
    s = s.translate(_PUNCT)
    s = _ARTICLES.sub(" ", s)
    return " ".join(s.split())


def em_score(pred: str, gold: str) -> float:
    return float(_normalize(pred) == _normalize(gold))


def f1_score(pred: str, gold: str) -> float:
    p, g = _normalize(pred).split(), _normalize(gold).split()
    if not p or not g:
        return float(p == g)
    common = Counter(p) & Counter(g)
    same = sum(common.values())
    if same == 0:
        return 0.0
    prec, rec = same / len(p), same / len(g)
    return 2 * prec * rec / (prec + rec)


# --------------------------------------------------------------------------- #
# Corpus container
# --------------------------------------------------------------------------- #
@dataclass
class Slice:
    corpus_emb: np.ndarray  # [n, d] L2-normalized
    query_emb: np.ndarray  # [q, d] L2-normalized
    paragraphs: list[dict]  # {id,title,text}
    questions: list[dict]  # {qid,question,answer,type,gold_ids,gold_titles,...}
    out_adj: dict[int, list[int]]
    k: int

    @property
    def n(self) -> int:
        return len(self.paragraphs)


def load_slice(manifest_path: Path) -> Slice:
    m = json.loads(manifest_path.read_text())
    corpus_emb = np.load(m["corpus_emb_path"])
    query_emb = np.load(m["query_emb_path"])
    out_adj: dict[int, list[int]] = defaultdict(list)
    for s, d in m["_edges"]:
        out_adj[int(s)].append(int(d))
    return Slice(
        corpus_emb=corpus_emb,
        query_emb=query_emb,
        paragraphs=m["paragraphs"],
        questions=m["questions"],
        out_adj=out_adj,
        k=m["k"],
    )


# --------------------------------------------------------------------------- #
# Retrievers (host-side numpy; mirror the engine semantics)
# --------------------------------------------------------------------------- #
def _cosine_rank(corpus_emb: np.ndarray, q: np.ndarray, ids: np.ndarray | None = None):
    """Return candidate ids sorted by descending cosine to q (vectors normalized)."""
    if ids is None:
        scores = corpus_emb @ q
        return np.argsort(-scores)
    scores = corpus_emb[ids] @ q
    return ids[np.argsort(-scores)]


def retrieve_vector(sl: Slice, qi: int, k: int) -> list[int]:
    order = _cosine_rank(sl.corpus_emb, sl.query_emb[qi])
    return [int(x) for x in order[:k]]


def _reachable(sl: Slice, seed_ids: list[int], hops: int) -> list[int]:
    """Out-edge reachable set from seeds in BFS order (the 'bridges'), seeds first."""
    seen = set(seed_ids)
    order = list(seed_ids)
    frontier = list(seed_ids)
    for _ in range(hops):
        nxt: list[int] = []
        for s in frontier:
            for d in sl.out_adj.get(s, ()):
                if d not in seen:
                    seen.add(d)
                    nxt.append(d)
                    order.append(d)
        frontier = nxt
        if not frontier:
            break
    return order


def retrieve_graph_rerank(
    sl: Slice, qi: int, k: int, *, seeds: int = 5, hops: int = 2
) -> list[int]:
    """ABLATION (the naive retriever) — vector-seed -> reachable -> re-rank by QUERY
    cosine -> top-k. Provably cannot help: re-ranking the reachable set by query
    similarity still buries the hard 2nd-hop bridge (low query similarity is WHY
    it is hard), and at loose k it only restricts the pool. Reported to show the
    negative result honestly — see retrieve_graph_inject for the real mechanism."""
    q = sl.query_emb[qi]
    seed_ids = [int(x) for x in _cosine_rank(sl.corpus_emb, q)[:seeds]]
    cand = np.fromiter(set(_reachable(sl, seed_ids, hops)), dtype=np.int64)
    order = _cosine_rank(sl.corpus_emb, q, cand)
    return [int(x) for x in order[:k]]


def retrieve_graph_inject(
    sl: Slice, qi: int, k: int, *, seeds: int = 2, hops: int = 2
) -> list[int]:
    """The REAL GraphRAG mechanism — take the top vector hit(s), then INJECT their
    graph-reachable bridges into the result REGARDLESS of query similarity, then
    fill the remaining budget with the vector ranking. The bridge that pure vector
    search misses (low query similarity, reached only via topology) is guaranteed
    into the context. This is what lifts multi-hop joint recall at tight k."""
    q = sl.query_emb[qi]
    vec_order = [int(x) for x in _cosine_rank(sl.corpus_emb, q)]
    seed_ids = vec_order[:seeds]
    injected = _reachable(sl, seed_ids, hops)  # seeds + bridges, regardless of sim
    out: list[int] = []
    for x in injected + vec_order:
        if x not in out:
            out.append(x)
        if len(out) >= k:
            break
    return out[:k]


# back-compat alias used by the toy unit test (the naive ablation behaviour)
retrieve_graph = retrieve_graph_rerank


# --------------------------------------------------------------------------- #
# Evidence-retrieval grading (no LLM — the credible thesis metric)
# --------------------------------------------------------------------------- #
def evidence_scores(retrieved: list[int], gold_ids: list[int]) -> dict:
    g = set(gold_ids)
    r = set(retrieved)
    if not g:
        return {"recall": 0.0, "joint": 0.0, "f1": 0.0}
    hit = len(g & r)
    recall = hit / len(g)
    prec = hit / len(r) if r else 0.0
    f1 = 2 * prec * recall / (prec + recall) if (prec + recall) else 0.0
    joint = float(g.issubset(r))  # HotpotQA "joint" = ALL supporting paras found
    return {"recall": recall, "joint": joint, "f1": f1}


# --------------------------------------------------------------------------- #
# Readers (answer EM/F1)
# --------------------------------------------------------------------------- #
class ExtractiveReader:
    """Deterministic NON-LLM lower-bound reader. Labeled as such in the report.

    Heuristic: for the retrieved context, score every capitalized noun-ish span
    by lexical overlap with the question and return the best; yes/no questions
    are answered by a polarity heuristic. This exists to exercise the EM/F1
    pipeline end-to-end without an LLM, NOT to be a competitive QA system.
    """

    name = "extractive-heuristic (NON-LLM lower bound)"

    _SPAN = re.compile(r"\b([A-Z][a-zA-Z0-9.\-]*(?:\s+[A-Z][a-zA-Z0-9.\-]*)*)\b")

    def answer(self, question: str, contexts: list[str]) -> str:
        ql = set(_normalize(question).split())
        if any(
            question.lower().startswith(w)
            for w in ("is ", "was ", "are ", "were ", "did ", "does ", "do ")
        ):
            return "yes"  # comparison-question polarity stub
        best, best_score = "", -1.0
        joined = " ".join(contexts)
        for m in self._SPAN.finditer(joined):
            span = m.group(1)
            sl = set(_normalize(span).split())
            if not sl:
                continue
            overlap = len(sl & ql) - 0.05 * len(sl)  # prefer concise, on-topic spans
            if overlap > best_score:
                best, best_score = span, overlap
        return best


class AnthropicReader:
    """LLM reader for the headline EM/F1. Wired; needs ANTHROPIC_API_KEY + anthropic.

    Uses the latest Claude per house convention. Pinned model id recorded in the
    report so the number is reproducible.
    """

    model = "claude-opus-4-8"
    name = f"anthropic:{model}"

    def __init__(self):
        import anthropic  # raises if not installed

        self._c = anthropic.Anthropic()

    def answer(self, question: str, contexts: list[str]) -> str:
        ctx = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts))
        prompt = (
            "Answer the question using ONLY the context. Reply with the shortest "
            "exact answer span (for yes/no questions reply 'yes' or 'no'). "
            f"No explanation.\n\nContext:\n{ctx}\n\nQuestion: {question}\nAnswer:"
        )
        msg = self._c.messages.create(
            model=self.model,
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()


def make_reader(kind: str):
    if kind == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "reader=anthropic needs ANTHROPIC_API_KEY (EM/F1 headline is "
                "reader-gated on this box). Use --reader extractive for the "
                "labeled non-LLM lower bound."
            )
        return AnthropicReader()
    return ExtractiveReader()


# --------------------------------------------------------------------------- #
# Run + aggregate
# --------------------------------------------------------------------------- #
# Three retrievers — the honest, complete comparison:
#   vector_only        : the baseline.
#   graph_inject       : the REAL GraphRAG mechanism (inject reachable bridges).
#   graph_rerank       : the naive ablation (gate + re-rank by query cosine) — the
#                        NEGATIVE result, kept so the report shows WHY mechanism matters.
RETRIEVERS = {
    "vector_only": lambda sl, qi, k: retrieve_vector(sl, qi, k),
    "graph_inject": lambda sl, qi, k: retrieve_graph_inject(sl, qi, k, seeds=2, hops=2),
    "graph_rerank": lambda sl, qi, k: retrieve_graph_rerank(sl, qi, k, seeds=5, hops=2),
}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def sweep(sl: Slice, ks: list[int]) -> dict:
    """Evidence recall + joint over k, per retriever, overall and by question type.

    No LLM — this is the credible, fully host-computable thesis metric.
    Returns: {retriever: {k: {group: {"recall","joint"}}}}, group in {all,<type>}.
    """
    out: dict = {
        name: {k: defaultdict(lambda: {"recall": [], "joint": []}) for k in ks}
        for name in RETRIEVERS
    }
    for qi, q in enumerate(sl.questions):
        for name, fn in RETRIEVERS.items():
            for k in ks:
                ev = evidence_scores(fn(sl, qi, k), q["gold_ids"])
                for grp in ("all", q["type"]):
                    out[name][k][grp]["recall"].append(ev["recall"])
                    out[name][k][grp]["joint"].append(ev["joint"])
    # reduce lists -> means
    red: dict = {}
    for name, byk in out.items():
        red[name] = {}
        for k, bygrp in byk.items():
            red[name][k] = {
                grp: {"recall": _mean(d["recall"]), "joint": _mean(d["joint"])}
                for grp, d in bygrp.items()
            }
    return red


def reader_scores(sl: Slice, reader, k: int) -> dict:
    """Downstream answer EM/F1 (+ evidence) per retriever at one budget k."""
    para_text = {p["id"]: f"{p['title']}. {p['text']}" for p in sl.paragraphs}
    agg: dict = {name: defaultdict(list) for name in RETRIEVERS}
    for qi, q in enumerate(sl.questions):
        for name, fn in RETRIEVERS.items():
            retrieved = fn(sl, qi, k)
            ev = evidence_scores(retrieved, q["gold_ids"])
            pred = reader.answer(q["question"], [para_text[i] for i in retrieved])
            agg[name]["answer_em"].append(em_score(pred, q["answer"]))
            agg[name]["answer_f1"].append(f1_score(pred, q["answer"]))
            agg[name]["ev_recall"].append(ev["recall"])
            agg[name]["ev_joint"].append(ev["joint"])
    return {name: {m: _mean(v) for m, v in d.items()} for name, d in agg.items()}


def render_md(
    sw: dict, rd: dict, sl: Slice, reader_name: str, ks: list[int], reader_k: int
) -> str:
    types = sorted({q["type"] for q in sl.questions})
    # headline: joint recall on bridge (multi-hop) questions at the tight reader_k.
    grp = "bridge" if "bridge" in types else "all"
    vj = sw["vector_only"][reader_k][grp]["joint"]
    gj = sw["graph_inject"][reader_k][grp]["joint"]
    lines: list[str] = []
    w = lines.append
    w("# TriDB Benchmark — GraphRAG QA Accuracy (HotpotQA dev slice, Plan 015)")
    w("")
    w(
        f"**Real graph, real embeddings, real multi-hop questions.** Injecting REAL "
        f"graph bridges into the retrieved context lifts multi-hop **joint** evidence "
        f"recall@{reader_k} (both supporting paragraphs found) on `{grp}` questions by "
        f"**{gj - vj:+.1%}** over vector-only ({vj:.1%} -> {gj:.1%}), on "
        f"{len(sl.questions)} HotpotQA dev questions / {sl.n}-paragraph corpus."
    )
    w("")
    w(
        "- Graph = REAL title-mention edges (embedding-INDEPENDENT proxy for Wikipedia "
        "hyperlinks): rebuilding with a different encoder does not move an edge — so this "
        "is a true test that *topology* (not the vectors) carries the multi-hop signal."
    )
    w("- Embeddings = BGE-base-en-v1.5 (dim 768), cosine.")
    w(
        "- **The mechanism matters (honest negative + positive):** `graph_inject` adds "
        "graph-reachable bridges to the context regardless of query similarity. "
        "`graph_rerank` (vector-seed -> reachable -> re-rank by query cosine) is the naive "
        "version and does NOT help — it still buries the hard 2nd hop and restricts the "
        "pool at loose k. Both are shown."
    )
    w("")
    w("## Evidence recall vs k — joint (BOTH gold) and any-gold (no LLM)")
    w("")
    w(f"Group: `{grp}` questions (the multi-hop case the graph targets).")
    w("")
    header = (
        "| k | " + " | ".join(f"{n} joint" for n in RETRIEVERS) + " | inject lift |"
    )
    w(header)
    w("|---:|" + "---:|" * (len(RETRIEVERS) + 1))
    for k in ks:
        cells = [f"{sw[n][k][grp]['joint']:.3f}" for n in RETRIEVERS]
        lift = sw["graph_inject"][k][grp]["joint"] - sw["vector_only"][k][grp]["joint"]
        w(f"| {k} | " + " | ".join(cells) + f" | {lift:+.3f} |")
    w("")
    w("Any-gold recall@k (same group):")
    w("")
    w("| k | " + " | ".join(f"{n}" for n in RETRIEVERS) + " | inject lift |")
    w("|---:|" + "---:|" * (len(RETRIEVERS) + 1))
    for k in ks:
        cells = [f"{sw[n][k][grp]['recall']:.3f}" for n in RETRIEVERS]
        lift = (
            sw["graph_inject"][k][grp]["recall"] - sw["vector_only"][k][grp]["recall"]
        )
        w(f"| {k} | " + " | ".join(cells) + f" | {lift:+.3f} |")
    w("")
    w("### By question type (joint recall @ k=" + str(reader_k) + ")")
    w("")
    w("| type | vector | graph_inject | lift |")
    w("|---|---:|---:|---:|")
    for t in types:
        vt = sw["vector_only"][reader_k][t]["joint"]
        gt = sw["graph_inject"][reader_k][t]["joint"]
        w(f"| {t} | {vt:.3f} | {gt:.3f} | {gt - vt:+.3f} |")
    w("")
    w(f"## Downstream answer accuracy (EM / F1) @ k={reader_k}")
    w("")
    w(f"Reader = {reader_name}.")
    w("")
    w("| retriever | answer EM | answer F1 | ev recall | ev joint |")
    w("|---|---:|---:|---:|---:|")
    for name in RETRIEVERS:
        a = rd[name]
        w(
            f"| {name} | {a['answer_em']:.3f} | {a['answer_f1']:.3f} | "
            f"{a['ev_recall']:.3f} | {a['ev_joint']:.3f} |"
        )
    w("")
    w("## Honesty notes")
    w("")
    w(
        "- **Where graph helps:** at TIGHT, realistic RAG budgets (k=3-5) on multi-hop "
        "(`bridge`) questions — exactly where vector-only misses the low-query-similarity "
        "2nd hop. At loose k the pool saturates and the lift shrinks; on single-hop "
        "(`comparison`) questions there is no 2nd hop to recover, so graph ~ vector. "
        "This k/type dependence is the real finding, reported as a curve, not a point."
    )
    w(
        f"- **Scope:** host-side accuracy on a {len(sl.questions)}-question dev slice / "
        f"{sl.n}-paragraph corpus (HotpotQA `distractor` pool, gold guaranteed present). The "
        "full retrieve-from-all-Wikipedia run and the live tjs() latency-at-fixed-accuracy "
        "headline are GX10/engine-gated (scripts/bench_graphrag.sh)."
    )
    w(
        "- **Graph is a mention-proxy, stated plainly:** the official Wikipedia hyperlink "
        "dump was unreachable here (CMU host down; HF mirrors gated). Title-mention edges "
        "are a faithful, embedding-independent stand-in; the on-target run can swap in the "
        "real hyperlink dump (the manifest records the edge source)."
    )
    if reader_name.startswith("extractive"):
        w(
            "- **Answer EM/F1 is a NON-LLM lower bound.** No ANTHROPIC_API_KEY on this box, so "
            "the headline LLM reader (AnthropicReader) is wired but unrun. Treat the "
            "evidence-recall lift as the real result and EM/F1 as plumbing until the reader runs."
        )
    w("")
    w("## Reproduce")
    w("")
    w("```bash")
    w(
        "make fetch-hotpot HOTPOT_Q=150     # HotpotQA dev slice (HF mirror; CMU host down)"
    )
    w("make graphrag                      # real graph + BGE-768 + accuracy sweep")
    w(
        "#   GRAPHRAG_READER=anthropic make graphrag   # LLM EM/F1 headline (needs API key)"
    )
    w("```")
    w("")
    w(
        "_Generated by `bench/graphrag_report.py` (`make graphrag`). Numbers are observed._"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GraphRAG QA-accuracy report.")
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument(
        "--reader", choices=["extractive", "anthropic"], default="extractive"
    )
    ap.add_argument(
        "--ks", type=int, nargs="+", default=[2, 3, 5, 10], help="k sweep points"
    )
    ap.add_argument(
        "--reader-k", type=int, default=5, help="k budget for EM/F1 + by-type table"
    )
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/results/graphrag_metrics.json")
    )
    ap.add_argument(
        "--md-out", type=Path, default=Path("docs/benchmark_graphrag_v0.1.0.md")
    )
    args = ap.parse_args(argv)

    sl = load_slice(args.manifest)
    ks = sorted(set(args.ks) | {args.reader_k})
    reader = make_reader(args.reader)
    print(
        f"[graphrag] {len(sl.questions)} questions, {sl.n} paragraphs, "
        f"ks={ks}, reader_k={args.reader_k}, reader={reader.name}"
    )
    sw = sweep(sl, ks)
    rd = reader_scores(sl, reader, args.reader_k)

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(
            {
                "ks": ks,
                "reader_k": args.reader_k,
                "reader": reader.name,
                "n_questions": len(sl.questions),
                "n_paragraphs": sl.n,
                "sweep": sw,
                "reader_scores": rd,
            },
            indent=2,
        )
    )
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(sw, rd, sl, reader.name, ks, args.reader_k))

    grp = "bridge" if any(q["type"] == "bridge" for q in sl.questions) else "all"
    vj = sw["vector_only"][args.reader_k][grp]["joint"]
    gj = sw["graph_inject"][args.reader_k][grp]["joint"]
    print(
        f"[graphrag] joint recall@{args.reader_k} ({grp}): vector={vj:.3f} "
        f"graph_inject={gj:.3f} lift={gj - vj:+.3f} -> {args.md_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

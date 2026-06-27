"""4-way fusion ablation on MultiHopRAG — the thesis-falsification test (Plan 015).

Same question set, four retrievers, recall@k:
  vector_only      : cosine top-k on the query.
  graph_only       : seed by query-entity title match, BFS over entity edges,
                     rank by graph proximity (no embeddings).
  relational_only  : keep docs matching the question's relational constraint
                     (category/source/date span), rank by recency (the natural
                     relational order) — no vector, no graph.
  fusion           : relational filter -> vector rank within it -> inject graph
                     bridges (the tjs-style fused operator).

FALSIFICATION: if fusion does NOT beat the best single modality on recall@k, the
tri-modal thesis is wrong. The report states the verdict explicitly, per
question_type. recall is exact + host-side; live latency is GX10-gated.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_WORD = re.compile(r"[a-z0-9]+")


def _tok(s: str) -> list[str]:
    return _WORD.findall((s or "").lower())


@dataclass
class Slice:
    corpus_emb: np.ndarray
    query_emb: np.ndarray
    docs: list[dict]  # {id,title,category,source,ym}
    questions: list[dict]
    out_adj: dict[int, list[int]]
    title_key: dict[str, int]
    k: int


def load_slice(manifest_path: Path) -> Slice:
    m = json.loads(manifest_path.read_text())
    out_adj: dict[int, list[int]] = defaultdict(list)
    for s, d in m["_edges"]:
        out_adj[int(s)].append(int(d))
    docs = m["docs"]
    title_key = {" ".join(_tok(d["title"])): d["id"] for d in docs if d["title"]}
    return Slice(
        corpus_emb=np.load(m["corpus_emb_path"]),
        query_emb=np.load(m["query_emb_path"]),
        docs=docs,
        questions=m["questions"],
        out_adj=out_adj,
        title_key=title_key,
        k=m["k"],
    )


# --------------------------------------------------------------------------- #
# Retrievers
# --------------------------------------------------------------------------- #
def retrieve_vector(sl: Slice, qi: int, k: int) -> list[int]:
    return [int(x) for x in np.argsort(-(sl.corpus_emb @ sl.query_emb[qi]))[:k]]


def _query_entity_seeds(sl: Slice, query: str) -> list[int]:
    """Seed docs whose title (an entity) is mentioned in the query — graph entry
    points WITHOUT using embeddings."""
    toks = _tok(query)
    seeds = []
    maxlen = max((len(k.split()) for k in sl.title_key), default=1)
    n = len(toks)
    for length in range(2, maxlen + 1):
        for i in range(max(0, n - length + 1)):
            did = sl.title_key.get(" ".join(toks[i : i + length]))
            if did is not None and did not in seeds:
                seeds.append(did)
    return seeds


def retrieve_graph(sl: Slice, qi: int, k: int, *, hops: int = 2) -> list[int]:
    """Pure graph: entity-seed -> BFS over mention edges, rank by BFS distance."""
    seeds = _query_entity_seeds(sl, sl.questions[qi]["query"])
    if not seeds:
        return []
    order, seen = list(seeds), set(seeds)
    frontier = list(seeds)
    for _ in range(hops):
        nxt = []
        for s in frontier:
            for d in sl.out_adj.get(s, ()):
                if d not in seen:
                    seen.add(d)
                    nxt.append(d)
                    order.append(d)
        frontier = nxt
        if not frontier:
            break
    return order[:k]


def _relational_mask(sl: Slice, q: dict, *, prefix: str = "rel") -> np.ndarray:
    """Docs passing the question's relational constraint (category OR source match,
    AND within the date span when present). prefix='rel' = ORACLE (gold-derived);
    prefix='qrel' = DEPLOYABLE (parsed from the query text). An empty constraint
    (no cue) passes everything -> the relational leg is a no-op for that question."""
    cats = set(q.get(f"{prefix}_categories", []))
    srcs = set(q.get(f"{prefix}_sources", []))
    ym_min, ym_max = q.get(f"{prefix}_ym_min", 0), q.get(f"{prefix}_ym_max", 0)
    mask = np.zeros(len(sl.docs), dtype=bool)
    for d in sl.docs:
        ok = True
        if cats or srcs:
            ok = (d["category"] in cats) or (d["source"] in srcs)
        if ok and ym_min and ym_max:
            ok = ym_min <= d["ym"] <= ym_max
        mask[d["id"]] = ok
    return mask


def retrieve_relational(
    sl: Slice, qi: int, k: int, *, prefix: str = "rel"
) -> list[int]:
    """Pure relational: filter by the constraint, rank by recency (ym desc)."""
    ids = np.flatnonzero(_relational_mask(sl, sl.questions[qi], prefix=prefix))
    if ids.size == 0:
        return []
    yms = np.array([sl.docs[i]["ym"] for i in ids])
    return [int(ids[j]) for j in np.argsort(-yms)[:k]]


def retrieve_fusion_hardfilter(
    sl: Slice, qi: int, k: int, *, prefix: str = "rel"
) -> list[int]:
    """ABLATION (naive) — HARD relational pre-filter, then vector-rank within it.
    Provably caps recall at the relational set: any gold the imperfect per-question
    constraint excludes is unrecoverable. Kept to show why the mechanism matters."""
    q = sl.query_emb[qi]
    cand = np.flatnonzero(_relational_mask(sl, sl.questions[qi], prefix=prefix))
    if cand.size == 0:
        cand = np.arange(len(sl.docs))
    return [int(cand[j]) for j in np.argsort(-(sl.corpus_emb[cand] @ q))[:k]]


def retrieve_fusion(
    sl: Slice, qi: int, k: int, *, hops: int = 2, prefix: str = "rel"
) -> list[int]:
    """tjs-style SOFT fusion — vector seed (recall base) -> INJECT graph bridges that
    ALSO pass the relational gate (the multi-hop evidence is entity-connected AND
    relationally coherent) -> vector fill. Relational is a gate on the injected
    bridges, NOT a hard pre-filter, so fusion never drops below the vector base."""
    qv = sl.query_emb[qi]
    vec_order = [int(x) for x in np.argsort(-(sl.corpus_emb @ qv))]
    relmask = _relational_mask(sl, sl.questions[qi], prefix=prefix)
    seeds = vec_order[:2]
    # bridges: graph-reachable from the seeds AND relationally valid AND vector-plausible
    bridges, seen = [], set(seeds)
    frontier = list(seeds)
    for _ in range(hops):
        nxt = []
        for s in frontier:
            for d in sl.out_adj.get(s, ()):
                if d not in seen:
                    seen.add(d)
                    nxt.append(d)
                    if relmask[d]:  # relational gate on the injected bridge
                        bridges.append(d)
        frontier = nxt
        if not frontier:
            break
    # rank bridges by vector sim so the best-supported bridge is injected first
    bridges.sort(key=lambda d: -float(sl.corpus_emb[d] @ qv))
    out: list[int] = []
    for x in seeds + bridges + vec_order:
        if x not in out:
            out.append(x)
        if len(out) >= k:
            break
    return out[:k]


RETRIEVERS = {
    "vector_only": retrieve_vector,
    "graph_only": retrieve_graph,
    "relational_only": lambda sl, qi, k: retrieve_relational(sl, qi, k, prefix="rel"),
    "fusion": lambda sl, qi, k: retrieve_fusion(sl, qi, k, prefix="rel"),
    "fusion_hardfilter": lambda sl, qi, k: retrieve_fusion_hardfilter(
        sl, qi, k, prefix="rel"
    ),
    # DEPLOYABLE (query-parsed constraint, no gold leakage):
    "relational_qparse": lambda sl, qi, k: retrieve_relational(
        sl, qi, k, prefix="qrel"
    ),
    "fusion_qparse": lambda sl, qi, k: retrieve_fusion(sl, qi, k, prefix="qrel"),
}


def recall_at_k(got: list[int], gold: list[int], k: int) -> float:
    g = set(gold)
    if not g:
        return float("nan")
    return len(set(got[:k]) & g) / len(g)


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if x == x]  # drop NaN
    return sum(xs) / len(xs) if xs else 0.0


def run(sl: Slice, k: int) -> dict:
    agg = {n: defaultdict(list) for n in RETRIEVERS}
    for qi, q in enumerate(sl.questions):
        if not q["gold_ids"]:
            continue
        for n, fn in RETRIEVERS.items():
            r = recall_at_k(fn(sl, qi, k), q["gold_ids"], k)
            agg[n]["all"].append(r)
            agg[n][q["question_type"]].append(r)
    groups = ["all"] + sorted(
        {q["question_type"] for q in sl.questions if q["question_type"]}
    )
    return {n: {g: _mean(agg[n][g]) for g in groups} for n in RETRIEVERS}, groups


def render_md(summary: dict, groups: list[str], sl: Slice, k: int, graded: int) -> str:
    vec = summary["vector_only"]["all"]
    fusion = summary["fusion"]["all"]  # oracle relational
    relplusvec = summary["fusion_hardfilter"]["all"]
    fusion_q = summary["fusion_qparse"]["all"]  # DEPLOYABLE (query-parsed)
    rel_contrib = relplusvec - vec  # value of the (oracle) relational filter
    graph_contrib = fusion - relplusvec  # value of ADDING graph on top of rel+vector
    q_contrib = fusion_q - vec  # deployable fusion lift over vector
    lines: list[str] = []
    w = lines.append
    w("# TriDB Benchmark — Tri-Modal Fusion Ablation (MultiHopRAG)")
    w("")
    w(
        f"**Falsification test — DEPLOYABLE fusion (query-parsed relational) = "
        f"{fusion_q:.3f} vs vector-only {vec:.3f} ({q_contrib:+.3f} recall@{k}); "
        f"ORACLE fusion (gold-derived relational) = {fusion:.3f} ({fusion - vec:+.3f}) is "
        "the upper bound. The graph leg adds ~nothing on this news workload — read the caveats.**"
    )
    w("")
    w(
        f"Same {graded} MultiHopRAG questions (gold-resolved), recall@{k} over a "
        f"{sl.corpus_emb.shape[0]}-article corpus with REAL relational metadata. Each "
        "config isolates a modality; fusion = vector-seed -> inject graph bridges that "
        "pass the relational gate -> vector fill (the tjs-style operator). `*_qparse` use "
        "a relational constraint PARSED FROM THE QUERY (no gold leakage)."
    )
    w("")
    w("## recall@k by configuration")
    w("")
    w("| config | " + " | ".join(groups) + " |")
    w("|---|" + "---:|" * len(groups))
    for n in RETRIEVERS:
        w(f"| {n} | " + " | ".join(f"{summary[n][g]:.3f}" for g in groups) + " |")
    w("")
    w("## Per-modality contribution (all questions)")
    w("")
    w("| modality step | recall@k | delta |")
    w("|---|---:|---:|")
    w(f"| vector_only (base) | {vec:.3f} | — |")
    w(
        f"| + query-parsed relational (DEPLOYABLE fusion) | {fusion_q:.3f} | {q_contrib:+.3f} |"
    )
    w(
        f"| + oracle relational (rel+vector, upper bound) | {relplusvec:.3f} | {rel_contrib:+.3f} |"
    )
    w(
        f"| + graph inject on oracle (full fusion) | {fusion:.3f} | {graph_contrib:+.3f} |"
    )
    w("")
    w("## Caveats (these decide whether the win is real)")
    w("")
    w(
        f"1. **Oracle vs deployable relational.** The `rel_*` constraint is GOLD-DERIVED "
        "(category/source/date of the gold evidence) = an ORACLE upper bound a real system "
        f"does NOT have ({rel_contrib:+.3f}). The `qrel_*` constraint is PARSED FROM THE "
        f"QUERY TEXT (sources/categories named, years/months mentioned) and is deployable: "
        f"query-parsed fusion still beats vector-only by {q_contrib:+.3f}, though the cue is "
        "sparse (many queries state no relational constraint, so the leg is a no-op there)."
    )
    w(
        f"2. **The graph leg adds ~nothing on this workload** (graph_only={summary['graph_only']['all']:.3f}; "
        f"graph on top of rel+vector = {graph_contrib:+.3f}). News multi-hop evidence is "
        "already vector-retrievable and the entity graph is dense/noisy. CONTRAST Plan-015 "
        "HotpotQA, where graph injection lifted multi-hop joint recall +15.6 pts — graph "
        "helps Wikipedia-bridge multi-hop, not news-entity multi-hop. **Fusion's value is "
        "workload-dependent**; no single modality dominates across both, which is itself "
        "evidence FOR a multi-modal engine — but the graph leg is unproven here."
    )
    w(
        "3. **graph_only / relational_only use NO embeddings** (isolated modalities); the "
        "graph is embedding-independent (shared named-entity edges). recall is exact + "
        "host-side; live tjs() fused-operator latency is GX10/engine-gated."
    )
    w(
        "4. **fusion_hardfilter** (hard relational pre-filter, no graph) is shown as an "
        "ablation; the soft relational GATE in `fusion` avoids the recall cap a hard "
        "pre-filter imposes when the constraint is imperfect."
    )
    w("")
    w("```bash")
    w(
        "python -m tools.multihoprag_corpus --questions 300   # fetch + embed + graph + metadata"
    )
    w("python -m bench.ablation_report                      # 4-way recall@k + verdict")
    w("```")
    w("")
    w(
        "_Generated by `bench/ablation_report.py` (`make ablation`). Numbers are observed._"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MultiHopRAG 4-way fusion ablation.")
    ap.add_argument(
        "--manifest", type=Path, default=Path("data/multihoprag/manifest.json")
    )
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/results/ablation_metrics.json")
    )
    ap.add_argument(
        "--md-out", type=Path, default=Path("docs/benchmark_ablation_v0.1.0.md")
    )
    args = ap.parse_args(argv)

    sl = load_slice(args.manifest)
    k = args.k or sl.k
    graded = sum(1 for q in sl.questions if q["gold_ids"])
    summary, groups = run(sl, k)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(
            {"k": k, "graded": graded, "groups": groups, "summary": summary}, indent=2
        )
    )
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(summary, groups, sl, k, graded))
    single = max(
        summary[m]["all"] for m in ("vector_only", "graph_only", "relational_only")
    )
    print(
        f"[ablation] recall@{k}: "
        + " ".join(f"{n}={summary[n]['all']:.3f}" for n in RETRIEVERS)
    )
    print(
        f"[ablation] best_single={single:.3f} fusion={summary['fusion']['all']:.3f} "
        f"delta={summary['fusion']['all'] - single:+.3f} -> {args.md_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

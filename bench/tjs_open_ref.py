"""Host reference for `tjs_open` realization (B) — the TR-1-pure operator (ADR-0012).

Plan 007 spike. The shippable `tjs_open` (ADR-0012 §2 B) is GX10/engine-gated C; this
module is its *executable specification*: a pure-Python, no-engine, no-LLM reference for
the three algorithms the fork patch must implement, measured on the SAME HotpotQA recall
metric (recall@k vs `gold_ids`) the (A) oracle `bench/v2a_open.py` uses.

The two algorithmic holes ADR-0012 (B) leaves to hand-tuning are closed by citable results:

1. **Ranking — bounded forward-push PPR** (Andersen-Chung-Lang, FOCS 2006). A *graded*
   graph-relevance score seeded by the ANN top-`m_seeds`, computed by a local push that
   touches O(1/(alpha*r_max)) nodes — `nodes_examined`, the in-host TR-1 proxy. Replaces
   ADR-0012's O(1) reachability-membership (in/out) graph leg. The trap (HippoRAG: PPR to
   convergence then sort all passages = blocking = forfeits TR-1) is explicitly NOT used;
   we push to a residue floor `r_max` and read reserves incrementally.

2. **Termination — NRA / FR-bound** (Fagin-Lotem-Naor PODS 2001; Schnaitter-Polyzotis
   TODS 2010). A provable stopping rule over the (vector-rank, PPR-reserve) merge with
   best/worst-score bookkeeping. A "bridge" (graph-high / vector-past-frontier candidate)
   needs NO special case: it is just a candidate whose vector leg is unseen, kept alive by
   its best-score B until its worst-score W settles. Replaces ADR-0012's ad-hoc
   "injected bridges don't reset the drop counter".

3. **Fusion — RRF** (Cormack et al., SIGIR 2009). Rank-only fusion of the vector stream and
   the PPR-reserve stream: score(d) = sum_legs 1/(c + rank_leg(d)). Score-based fusion is
   doubly fragile on the fork (scalar `<->` returns 0 outside an index scan; PPR mass is on
   an incompatible scale), so rank-only RRF is the safe default.

Everything is streaming / bounded-buffer so the reference models the TR-1 operator, not a
second copy of the (A) BLOCKING oracle. `nodes_examined` / `candidates_examined` counters
are honest (every node/candidate touched is counted).
"""

from __future__ import annotations

import argparse
import heapq
import json
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Graph + seed primitives                                                      #
# --------------------------------------------------------------------------- #


def build_adjacency(edges, n_nodes: int) -> dict[int, list[int]]:
    """Adjacency dict from `_edges` (list of (src,dst)). Undirected: HotpotQA's
    title-mention graph is a symmetric co-occurrence proxy, so a bridge is reachable
    from either endpoint (matches v2a_open's UNION of neighbors)."""
    adj: dict[int, list[int]] = {i: [] for i in range(n_nodes)}
    for s, d in edges:
        s, d = int(s), int(d)
        if 0 <= s < n_nodes and 0 <= d < n_nodes and s != d:
            adj[s].append(d)
            adj[d].append(s)
    # dedup (parallel edges from repeated mentions) — keep deterministic order
    for k in adj:
        adj[k] = sorted(set(adj[k]))
    return adj


def ann_seeds(corpus_emb: np.ndarray, qv: np.ndarray, m_seeds: int) -> list[int]:
    """ANN top-`m_seeds` by L2 to the query embedding — the personalization vector.

    Mirrors v2a_open's seed CTE (`ORDER BY embedding <-> q LIMIT m_seeds`)."""
    d2 = np.sum((corpus_emb - qv) ** 2, axis=1)
    kk = min(m_seeds, len(d2))
    part = np.argpartition(d2, kk - 1)[:kk]
    return [int(i) for i in part[np.argsort(d2[part])]]


# --------------------------------------------------------------------------- #
# Step 1: bounded forward-push Personalized PageRank (Andersen-Chung-Lang)     #
# --------------------------------------------------------------------------- #


def bounded_push_ppr(
    adj: dict[int, list[int]],
    seeds: list[int],
    *,
    alpha: float = 0.15,
    r_max: float = 1e-3,
    seed_weights: dict[int, float] | None = None,
) -> tuple[dict[int, float], int]:
    """Local push PPR. Returns (reserves, nodes_examined).

    Priority-queue variant: repeatedly pop the node with max residue, move `alpha` of
    its residue to its reserve and spread `(1-alpha)` over out-neighbors; stop pushing a
    node once its residue < r_max. `nodes_examined` = distinct nodes whose residue was
    ever touched (the TR-1 proxy). Work is O(1/(alpha*r_max)), independent of |V|."""
    if seed_weights is None:
        w = 1.0 / max(1, len(seeds))
        seed_weights = {s: w for s in seeds}
    # normalize personalization vector to sum 1
    tot = sum(seed_weights.values()) or 1.0
    residue: dict[int, float] = {s: v / tot for s, v in seed_weights.items()}
    reserve: dict[int, float] = {}
    touched: set[int] = set(residue)
    # max-heap on residue via negation; entries are lazily invalidated
    heap = [(-residue[s], s) for s in residue]
    heapq.heapify(heap)
    while heap:
        neg_r, u = heapq.heappop(heap)
        ru = residue.get(u, 0.0)
        # lazy-delete stale heap entries
        if -neg_r != ru or ru < r_max:
            continue
        deg = len(adj.get(u, ()))
        reserve[u] = reserve.get(u, 0.0) + alpha * ru
        residue[u] = 0.0
        if deg == 0:
            continue
        spread = (1.0 - alpha) * ru / deg
        for v in adj[u]:
            nv = residue.get(v, 0.0) + spread
            residue[v] = nv
            touched.add(v)
            if nv >= r_max:
                heapq.heappush(heap, (-nv, v))
    return reserve, len(touched)


def power_iteration_ppr(
    adj: dict[int, list[int]],
    seeds: list[int],
    n_nodes: int,
    *,
    alpha: float = 0.15,
    iters: int = 200,
    tol: float = 1e-9,
    seed_weights: dict[int, float] | None = None,
) -> dict[int, float]:
    """BLOCKING convergence oracle (reference only — used to validate the local push)."""
    if seed_weights is None:
        w = 1.0 / max(1, len(seeds))
        seed_weights = {s: w for s in seeds}
    tot = sum(seed_weights.values()) or 1.0
    p = np.zeros(n_nodes)
    for s, v in seed_weights.items():
        if 0 <= s < n_nodes:
            p[s] = v / tot
    rank = p.copy()
    for _ in range(iters):
        nxt = alpha * p
        for u in range(n_nodes):
            deg = len(adj.get(u, ()))
            if deg and rank[u]:
                share = (1.0 - alpha) * rank[u] / deg
                for v in adj[u]:
                    nxt[v] += share
        if np.abs(nxt - rank).sum() < tol:
            rank = nxt
            break
        rank = nxt
    return {i: float(rank[i]) for i in range(n_nodes) if rank[i] > 0.0}


def topk_ids(scores: dict[int, float], k: int) -> list[int]:
    return [i for i, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


# --------------------------------------------------------------------------- #
# Step 2: NRA / FR-bound termination over (vector-rank, PPR-reserve) merge     #
# --------------------------------------------------------------------------- #


def _minmax_norm(d: dict[int, float]) -> dict[int, float]:
    if not d:
        return {}
    vals = list(d.values())
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return {k: 1.0 for k in d}
    return {k: (v - lo) / (hi - lo) for k, v in d.items()}


def nra_fr_merge(
    vec_stream: list[tuple[int, float]],
    ppr_stream: list[tuple[int, float]],
    *,
    k: int,
) -> tuple[list[int], int]:
    """NRA/FR-bounded top-k over two descending-sorted score streams.

    Each leg yields (id, score) in DESCENDING score order. Per candidate keep:
      W = sum of KNOWN partial scores (missing legs floored at 0),
      B = W + (current frontier ceiling of each UNSEEN leg).
    Stop when the k-th largest settled W >= the best B of any candidate that is not yet
    in the confirmed top-k (the FR bound). A "bridge" (seen only via PPR, vector unseen)
    is kept alive by its B until its W settles — no special case. Returns
    (top_k_ids, candidates_examined)."""
    n_legs = 2
    seen: dict[int, list[float | None]] = {}
    # frontier ceilings: the score of the most recent item pulled from each leg
    frontier = [
        vec_stream[0][1] if vec_stream else 0.0,
        ppr_stream[0][1] if ppr_stream else 0.0,
    ]
    iv = ip = 0
    examined: set[int] = set()

    def best_worst(cid: int) -> tuple[float, float]:
        partial = seen[cid]
        w = sum(p for p in partial if p is not None)
        b = w + sum(frontier[leg] for leg in range(n_legs) if partial[leg] is None)
        return b, w

    while iv < len(vec_stream) or ip < len(ppr_stream):
        # pull next item from whichever leg has the higher frontier (sorted-access NRA)
        pull_vec = iv < len(vec_stream) and (
            ip >= len(ppr_stream) or vec_stream[iv][1] >= ppr_stream[ip][1]
        )
        if pull_vec:
            cid, sc = vec_stream[iv]
            iv += 1
            frontier[0] = sc
            seen.setdefault(cid, [None, None])[0] = sc
        else:
            cid, sc = ppr_stream[ip]
            ip += 1
            frontier[1] = sc
            seen.setdefault(cid, [None, None])[1] = sc
        examined.add(cid)
        if iv >= len(vec_stream):
            frontier[0] = 0.0
        if ip >= len(ppr_stream):
            frontier[1] = 0.0

        # FR stop test: top-k by W, vs best B of everything outside that top-k set
        worsts = sorted(seen.items(), key=lambda kv: -best_worst(kv[0])[1])
        if len(worsts) >= k:
            kth_w = best_worst(worsts[k - 1][0])[1]
            outside_best_b = 0.0
            for cid2, _ in worsts[k:]:
                b2, _ = best_worst(cid2)
                outside_best_b = max(outside_best_b, b2)
            # also: any UNSEEN candidate could appear with B = sum(frontier)
            unseen_ceiling = sum(frontier)
            if kth_w >= max(outside_best_b, unseen_ceiling):
                top = [c for c, _ in worsts[:k]]
                return top, len(examined)

    # streams exhausted: rank by final W
    final = sorted(seen.items(), key=lambda kv: (-best_worst(kv[0])[1], kv[0]))
    return [c for c, _ in final[:k]], len(examined)


def consecutive_drops_merge(
    ranked: list[tuple[int, float]], *, k: int, term_cond: int
) -> tuple[list[int], int]:
    """Baseline heuristic: emit from a single fused-score-sorted list, stop after
    `term_cond` consecutive items that do not improve the running k-th score (VBASE
    consecutive_drops analogue). Returns (top_k_ids, candidates_examined)."""
    heap: list[tuple[float, int]] = []  # min-heap of (score, id)
    drops = 0
    examined = 0
    for cid, sc in ranked:
        examined += 1
        if len(heap) < k:
            heapq.heappush(heap, (sc, cid))
            drops = 0
        elif sc > heap[0][0]:
            heapq.heapreplace(heap, (sc, cid))
            drops = 0
        else:
            drops += 1
            if drops >= term_cond:
                break
    top = [c for _, c in sorted(heap, key=lambda x: (-x[0], x[1]))]
    return top, examined


# --------------------------------------------------------------------------- #
# Step 3: RRF fusion (Cormack, SIGIR 2009)                                     #
# --------------------------------------------------------------------------- #


def rrf_fuse(
    vec_ranked: list[int],
    ppr_ranked: list[int],
    *,
    k: int,
    c: int = 60,
    window: int = 0,
) -> list[int]:
    """RRF over two rank lists. score(d) = sum_legs 1/(c + rank_leg(d)) (rank 1-based).

    `window` bounds how far down each leg we consume (0 = full list); the windowed form
    is the non-blocking variant for the operator. A graph-high/vector-low bridge gets a
    high PPR rank and is promoted even with no vector rank — the bridge-injection
    requirement, score-free."""
    vr = vec_ranked if window <= 0 else vec_ranked[:window]
    pr = ppr_ranked if window <= 0 else ppr_ranked[:window]
    score: dict[int, float] = {}
    for rank, d in enumerate(vr, start=1):
        score[d] = score.get(d, 0.0) + 1.0 / (c + rank)
    for rank, d in enumerate(pr, start=1):
        score[d] = score.get(d, 0.0) + 1.0 / (c + rank)
    return [d for d, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


# --------------------------------------------------------------------------- #
# End-to-end per-query retrieval                                              #
# --------------------------------------------------------------------------- #


def vector_ranked(
    corpus_emb: np.ndarray, qv: np.ndarray, limit: int
) -> list[tuple[int, float]]:
    """Top-`limit` by descending similarity (= ascending L2). Returns (id, sim) with
    sim in [0,1] via min-max over the returned window (the rank is what matters)."""
    d2 = np.sum((corpus_emb - qv) ** 2, axis=1)
    limit = min(limit, len(d2))
    part = np.argpartition(d2, limit - 1)[:limit]
    order = part[np.argsort(d2[part])]
    sims = -d2[order]
    norm = _minmax_norm({int(i): float(s) for i, s in zip(order, sims)})
    return sorted(norm.items(), key=lambda kv: -kv[1])


def retrieve(
    corpus_emb: np.ndarray,
    qv: np.ndarray,
    adj: dict[int, list[int]],
    *,
    k: int,
    m_seeds: int,
    alpha: float,
    r_max: float,
    vec_limit: int,
    seed_weights_fn=None,
) -> dict:
    """One query through PPR(ranking) + NRA/FR(termination) + RRF(fusion).

    Returns ids per strategy plus the honest examined counters."""
    seeds = ann_seeds(corpus_emb, qv, m_seeds)
    sw = seed_weights_fn(seeds) if seed_weights_fn else None
    reserves, nodes_examined = bounded_push_ppr(
        adj, seeds, alpha=alpha, r_max=r_max, seed_weights=sw
    )
    ppr_norm = _minmax_norm(reserves)
    ppr_stream = sorted(ppr_norm.items(), key=lambda kv: -kv[1])
    vec_stream = vector_ranked(corpus_emb, qv, vec_limit)

    vec_ranked_ids = [i for i, _ in vec_stream]
    ppr_ranked_ids = [i for i, _ in ppr_stream]

    fr_ids, cand_examined = nra_fr_merge(vec_stream, ppr_stream, k=k)
    rrf_ids = rrf_fuse(vec_ranked_ids, ppr_ranked_ids, k=k)

    return {
        "seeds": seeds,
        "nodes_examined": nodes_examined,
        "candidates_examined": cand_examined,
        "vector_only": vec_ranked_ids[:k],
        "ppr_only": ppr_ranked_ids[:k],
        "fr_fused": fr_ids,
        "rrf_fused": rrf_ids,
        "n_reach": len(reserves),
    }


def grade(gold: set[int], ids: list[int], k: int) -> float:
    if not gold:
        return float("nan")
    return len(gold & set(ids[:k])) / len(gold)


# --------------------------------------------------------------------------- #
# Driver: sweep r_max and term_cond, emit the curve                            #
# --------------------------------------------------------------------------- #


def run_corpus(
    manifest: dict,
    corpus_emb,
    query_emb,
    *,
    k: int,
    k5: int,
    m_seeds: int,
    alpha: float,
    vec_limit: int,
    r_max_sweep,
    term_cond_sweep,
) -> dict:
    n = len(manifest["paragraphs"])
    adj = build_adjacency(manifest["_edges"], n)
    questions = manifest["questions"]
    gold = {q["qid"]: set(q.get("gold_ids", [])) for q in questions}

    # --- r_max sweep: recall@k, nodes_examined, candidates_examined ---
    rmax_curve = []
    for r_max in r_max_sweep:
        rec_vec, rec_ppr, rec_fr, rec_rrf = [], [], [], []
        nodes_ex, cand_ex, reach = [], [], []
        for q in questions:
            qid = q["qid"]
            g = gold.get(qid, set())
            if not g:
                continue
            out = retrieve(
                corpus_emb,
                query_emb[qid],
                adj,
                k=k,
                m_seeds=m_seeds,
                alpha=alpha,
                r_max=r_max,
                vec_limit=vec_limit,
            )
            rec_vec.append(grade(g, out["vector_only"], k))
            rec_ppr.append(grade(g, out["ppr_only"], k))
            rec_fr.append(grade(g, out["fr_fused"], k))
            rec_rrf.append(grade(g, out["rrf_fused"], k))
            nodes_ex.append(out["nodes_examined"])
            cand_ex.append(out["candidates_examined"])
            reach.append(out["n_reach"])
        rmax_curve.append(
            {
                "r_max": r_max,
                "recall_at_k_vector": _mean(rec_vec),
                "recall_at_k_ppr": _mean(rec_ppr),
                "recall_at_k_fr": _mean(rec_fr),
                "recall_at_k_rrf": _mean(rec_rrf),
                "nodes_examined_mean": _mean(nodes_ex),
                "nodes_examined_pct_corpus": 100.0 * _mean(nodes_ex) / n if n else 0.0,
                "candidates_examined_mean": _mean(cand_ex),
                "reach_mean": _mean(reach),
            }
        )

    # --- term_cond sweep for the consecutive_drops baseline (at a fixed mid r_max) ---
    mid_rmax = r_max_sweep[len(r_max_sweep) // 2]
    tc_curve = []
    for tc in term_cond_sweep:
        rec, cand = [], []
        for q in questions:
            qid = q["qid"]
            g = gold.get(qid, set())
            if not g:
                continue
            seeds = ann_seeds(corpus_emb, query_emb[qid], m_seeds)
            reserves, _ = bounded_push_ppr(adj, seeds, alpha=alpha, r_max=mid_rmax)
            ppr_norm = _minmax_norm(reserves)
            vec_stream = vector_ranked(corpus_emb, query_emb[qid], vec_limit)
            # fused single stream = max of the two normalized scores per id
            fused: dict[int, float] = {i: s for i, s in vec_stream}
            for i, s in ppr_norm.items():
                fused[i] = max(fused.get(i, 0.0), s)
            ranked = sorted(fused.items(), key=lambda kv: -kv[1])
            ids, ce = consecutive_drops_merge(ranked, k=k, term_cond=tc)
            rec.append(grade(g, ids, k))
            cand.append(ce)
        tc_curve.append(
            {
                "term_cond": tc,
                "recall_at_k": _mean(rec),
                "candidates_examined_mean": _mean(cand),
            }
        )

    # --- recall@5 strategy comparison at the best r_max (lowest that maxes FR recall) ---
    best = max(rmax_curve, key=lambda c: c["recall_at_k_fr"])
    best_rmax = best["r_max"]
    r5 = {"vector_only": [], "ppr_only": [], "rrf_fused": [], "fr_fused": []}
    for q in questions:
        qid = q["qid"]
        g = gold.get(qid, set())
        if not g:
            continue
        out = retrieve(
            corpus_emb,
            query_emb[qid],
            adj,
            k=k5,
            m_seeds=m_seeds,
            alpha=alpha,
            r_max=best_rmax,
            vec_limit=vec_limit,
        )
        r5["vector_only"].append(grade(g, out["vector_only"], k5))
        r5["ppr_only"].append(grade(g, out["ppr_only"], k5))
        r5["rrf_fused"].append(grade(g, out["rrf_fused"], k5))
        r5["fr_fused"].append(grade(g, out["fr_fused"], k5))

    return {
        "corpus": {
            "n_paragraphs": n,
            "n_edges": len(manifest["_edges"]),
            "n_questions_graded": sum(1 for q in questions if gold.get(q["qid"])),
        },
        "params": {
            "k": k,
            "k5": k5,
            "m_seeds": m_seeds,
            "alpha": alpha,
            "vec_limit": vec_limit,
        },
        "rmax_curve": rmax_curve,
        "term_cond_curve": tc_curve,
        "best_r_max": best_rmax,
        "recall_at_5": {
            "vector_only": _mean(r5["vector_only"]),
            "ppr_only": _mean(r5["ppr_only"]),
            "rrf_fused": _mean(r5["rrf_fused"]),
            "fr_fused": _mean(r5["fr_fused"]),
            "A_oracle": _a_oracle_recall_at_k(
                manifest, corpus_emb, query_emb, adj, k=k5, m_seeds=m_seeds
            ),
        },
    }


def _a_oracle_recall_at_k(manifest, corpus_emb, query_emb, adj, *, k, m_seeds) -> float:
    """The (A) BLOCKING composition oracle, host-side: ANN top-m_seeds UNION their graph
    neighbors, vector-reranked, top-k (mirrors bench/v2a_open.py's SQL). Reference target
    the streaming strategies must approach. Blocking by construction — oracle only."""
    gold = {q["qid"]: set(q.get("gold_ids", [])) for q in manifest["questions"]}
    recs = []
    for q in manifest["questions"]:
        qid = q["qid"]
        g = gold.get(qid, set())
        if not g:
            continue
        qv = query_emb[qid]
        seeds = ann_seeds(corpus_emb, qv, m_seeds)
        reach = set(seeds)
        for s in seeds:
            reach.update(adj.get(s, ()))
        reach_ids = np.array(sorted(reach))
        d2 = np.sum((corpus_emb[reach_ids] - qv) ** 2, axis=1)
        order = reach_ids[np.argsort(d2)][:k]
        recs.append(grade(g, [int(i) for i in order], k))
    return _mean(recs)


def _mean(xs) -> float:
    xs = [x for x in xs if x == x]  # drop nan
    return float(np.mean(xs)) if xs else float("nan")


def render_md(res: dict) -> str:
    lines: list[str] = []
    w = lines.append
    c = res["corpus"]
    p = res["params"]
    w(
        "# TriDB — `tjs_open` (B) host reference: PPR ranking + NRA/FR termination + RRF fusion"
    )
    w("")
    w(
        f"Pure-host spike (Plan 007). Corpus: {c['n_paragraphs']} paragraphs, "
        f"{c['n_edges']} edges, {c['n_questions_graded']} graded questions. "
        f"m_seeds={p['m_seeds']}, alpha={p['alpha']}, vec_limit={p['vec_limit']}."
    )
    w("")
    w("## recall@k vs r_max vs examined-% (the TR-1 curve)")
    w("")
    w(
        "| r_max | recall@k vec | recall@k ppr | recall@k FR | recall@k RRF | nodes examined | %corpus | cand examined |"
    )
    w("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in res["rmax_curve"]:
        w(
            f"| {row['r_max']:g} | {row['recall_at_k_vector']:.3f} | "
            f"{row['recall_at_k_ppr']:.3f} | {row['recall_at_k_fr']:.3f} | "
            f"{row['recall_at_k_rrf']:.3f} | {row['nodes_examined_mean']:.1f} | "
            f"{row['nodes_examined_pct_corpus']:.2f}% | {row['candidates_examined_mean']:.1f} |"
        )
    w("")
    w("## consecutive_drops baseline (the heuristic FR replaces), fixed mid r_max")
    w("")
    w("| term_cond | recall@k | cand examined |")
    w("|---:|---:|---:|")
    for row in res["term_cond_curve"]:
        w(
            f"| {row['term_cond']} | {row['recall_at_k']:.3f} | "
            f"{row['candidates_examined_mean']:.1f} |"
        )
    w("")
    r5 = res["recall_at_5"]
    w(f"## recall@5 strategy comparison (at best r_max = {res['best_r_max']:g})")
    w("")
    w("| strategy | recall@5 |")
    w("|---|---:|")
    for name in ("vector_only", "ppr_only", "rrf_fused", "fr_fused", "A_oracle"):
        w(f"| {name} | {r5[name]:.3f} |")
    w("")
    w(
        "_Generated by `bench/tjs_open_ref.py`. Recall@k vs `gold_ids`, no LLM reader. "
        "`A_oracle` is the BLOCKING (A) composition (ADR-0012 §2 A), the target the "
        "streaming FR/RRF strategies must approach. `nodes_examined` / `candidates_examined` "
        "are the in-host TR-1 proxy for the engine's early termination._"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="tjs_open (B) host reference: PPR + NRA/FR + RRF."
    )
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--k5", type=int, default=5)
    ap.add_argument("--m-seeds", type=int, default=5)
    ap.add_argument("--alpha", type=float, default=0.15)
    ap.add_argument(
        "--vec-limit",
        type=int,
        default=200,
        help="bounded vector-stream window (non-blocking proxy)",
    )
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/results/tjs_open_ref_metrics.json")
    )
    ap.add_argument(
        "--md-out", type=Path, default=Path("docs/benchmark_tjs_open_ref_v0.1.0.md")
    )
    args = ap.parse_args(argv)

    if not args.manifest.exists():
        ap.error(
            f"manifest {args.manifest} not found — the full-corpus run is DATA-GATED on "
            "this box (no data/hotpot/manifest.json; build it with `make fetch-hotpot` / "
            "tools/hotpot_corpus.py on a box with HF reachable). The unit tests "
            "(tests/test_tjs_open_ref.py) exercise the algorithms on synthetic graphs and "
            "run anywhere."
        )

    manifest = json.loads(args.manifest.read_text())
    corpus_emb = np.load(manifest["corpus_emb_path"])
    query_emb = np.load(manifest["query_emb_path"])

    r_max_sweep = [1e-2, 5e-3, 1e-3, 5e-4, 1e-4, 5e-5]
    term_cond_sweep = [10, 50, 200, 1000, 5000]
    res = run_corpus(
        manifest,
        corpus_emb,
        query_emb,
        k=args.k,
        k5=args.k5,
        m_seeds=args.m_seeds,
        alpha=args.alpha,
        vec_limit=args.vec_limit,
        r_max_sweep=r_max_sweep,
        term_cond_sweep=term_cond_sweep,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(res, indent=2))
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(res))
    r5 = res["recall_at_5"]
    print(
        f"[tjs-open-ref] recall@5: vector={r5['vector_only']:.3f} ppr={r5['ppr_only']:.3f} "
        f"rrf={r5['rrf_fused']:.3f} fr={r5['fr_fused']:.3f} A_oracle={r5['A_oracle']:.3f} "
        f"(best r_max={res['best_r_max']:g}) -> {args.md_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

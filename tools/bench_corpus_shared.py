"""Shared deterministic corpus generation for the TriDB benchmarks (DEV-1171/1172).

Single source of truth for the corpus both the TriDB live side and the
multi-system baseline run on. tools/bench_corpus.py (SM-1/3/4 + EXPLAIN latency)
and tools/bench_sm2_corpus.py (SM-2 client wall-clock) both build their corpus
here, so the entity ids / embeddings / timestamps / edges / queries are provably
IDENTICAL across both — the non-negotiable fairness requirement.

The numpy generation here is byte-for-byte the same draws (same RNG call order)
as tools/bench_corpus.py:build() and bench/live_report.py:rebuild_corpus(); a
regression test (tests/test_bench_corpus_shared.py) pins that equivalence.
"""

from __future__ import annotations

import numpy as np


def vec_literal(v) -> str:
    """Postgres float8[] literal '{a,b,c}' with full repr precision (matches
    tools/bench_corpus.py:_vec_literal)."""
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def build_corpus(args) -> dict:
    """Deterministically build the corpus from `args` (same fields bench_corpus.py
    uses: entities, dim, hubs, fanout, queries, k, window, time_min, time_max,
    query_jitter, seed).

    Returns a manifest dict. In addition to the public metadata fields
    (entities, dim, hubs, fanout, num_queries, k, edges, seed, time_min,
    time_max, window, queries, hub_dsts) it carries two private fields the SQL
    emitters need but the public manifest drops:

      * "_entities": [(id, ts:int, embedding:list[float])] for every entity
      * "_edges":    [(src, dst)] in generation order

    RNG draw order MUST match tools/bench_corpus.py:build() exactly:
      1. emb = standard_normal((n, dim)); normalize
      2. ts  = integers(time_min, time_max+1, n)
      3. per hub (in id order): centroid = standard_normal(dim); normalize;
         then choice(pool, fanout, replace=False)
      4. per query: jitter = standard_normal(dim) * query_jitter; then
         integers(time_min, time_max-window+2) for the window start
    """
    rng = np.random.default_rng(args.seed)
    n = args.entities
    dim = args.dim

    emb = rng.standard_normal((n, dim)).astype(np.float64)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    ts = rng.integers(args.time_min, args.time_max + 1, size=n)

    hubs = list(range(args.hubs))
    edges: list[tuple[int, int]] = []
    hub_dsts: dict[int, list[int]] = {}
    hub_centroids: dict[int, list[float]] = {}
    for h in hubs:
        centroid = rng.standard_normal(dim).astype(np.float64)
        centroid /= np.linalg.norm(centroid)
        hub_centroids[h] = centroid.tolist()
        d2 = np.sum((emb - centroid) ** 2, axis=1)
        pool = np.argsort(d2)[: max(args.fanout * 3, args.fanout + 1)]
        dsts = rng.choice(pool, size=min(args.fanout, len(pool)), replace=False)
        dsts = [int(d) for d in dsts if int(d) != h]
        hub_dsts[h] = dsts
        for d in dsts:
            edges.append((h, d))

    queries = []
    for qid in range(args.queries):
        h = hubs[qid % len(hubs)]
        centroid = np.array(hub_centroids[h])
        jitter = rng.standard_normal(dim).astype(np.float64) * args.query_jitter
        qv = centroid + jitter
        qv /= np.linalg.norm(qv)
        start = int(rng.integers(args.time_min, args.time_max - args.window + 2))
        window = list(range(start, start + args.window))
        queries.append(
            {"qid": qid, "src": h, "embedding": qv.tolist(), "window": window}
        )

    return {
        "entities": n,
        "dim": dim,
        "hubs": args.hubs,
        "fanout": args.fanout,
        "num_queries": len(queries),
        "k": args.k,
        "edges": len(edges),
        "seed": args.seed,
        "time_min": args.time_min,
        "time_max": args.time_max,
        "window": args.window,
        "queries": queries,
        "hub_dsts": {str(h): hub_dsts[h] for h in hubs},
        "_entities": [(i, int(ts[i]), emb[i].tolist()) for i in range(n)],
        "_edges": edges,
    }

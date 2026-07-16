"""HELD-OUT query set + brute-force L2 top-k oracle for the wiki vector-leg head-to-head.

The recall check for the 1M vector leg (TriDB plain HNSW scan vs Milvus ANN) must NOT be a
single exact-member probe (trivially recall 1.0). This builds a >=300-query held-out set with a
mix that actually exercises approximate-kNN quality:

  members     : an article's OWN stored vector (id in [0, N)); ANN should return it + true nbrs.
  midpoints   : the (renormalized) mean of two random member vectors — an in-manifold point that
                is NOT any stored row, so the top-k is a non-trivial neighborhood.
  non-members : a real article vector whose id is OUTSIDE the loaded slice (id in [N, corpus)),
                a genuine out-of-set probe (the article is not a row the stores hold).

Oracle = exact brute-force L2 top-k over the RAW stored 1M vectors (the engine's HNSW is built
`distmethod = l2_distance`; the corpus is ~unit-norm so this ranking == Milvus COSINE). Query
vectors are stored verbatim so the downstream latency harness can feed them to BOTH stores
(engine `embedding <-> '{vec}'::float8[]`, Milvus `col.search([vec])`) at EQUAL recall.

    python bench/wiki_h2h_queryset.py --emb data/wiki/enwiki/emb/dense_id_aligned.npy \
        --n 1000000 --k 10 --members 160 --midpoints 80 --nonmembers 80 \
        --out bench/results/wiki_h2h_queryset.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


def _valid(mm, lo, hi):
    """ids in [lo, hi) whose stored row is non-degenerate (gap ids are zero-filled)."""
    block = np.asarray(mm[lo:hi], dtype=np.float32)
    norms = np.linalg.norm(block, axis=1)
    return (lo + np.flatnonzero(norms > 0.5)).astype(np.int64)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb", required=True)
    ap.add_argument("--n", type=int, default=1_000_000)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--members", type=int, default=160)
    ap.add_argument("--midpoints", type=int, default=80)
    ap.add_argument("--nonmembers", type=int, default=80)
    ap.add_argument("--seed", type=int, default=1354)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    mm = np.load(args.emb, mmap_mode="r")
    corpus_rows = mm.shape[0]
    dim = mm.shape[1]
    if corpus_rows < args.n:
        raise SystemExit(f"emb has {corpus_rows} rows < N={args.n}")

    t0 = time.time()
    # RAW (un-renormalized) 1M slice — matches the engine's stored float8[] and l2_distance.
    base = np.ascontiguousarray(mm[: args.n]).astype(np.float32)
    print(f"[queryset] loaded base {base.shape} in {time.time() - t0:.1f}s", flush=True)

    member_ids = _valid(mm, 0, args.n)
    nonmember_ids = _valid(mm, args.n, corpus_rows)
    print(
        f"[queryset] valid members={member_ids.size} nonmembers={nonmember_ids.size}",
        flush=True,
    )

    queries = []  # {type, source_id|source_ids, vec}
    for qid in rng.choice(member_ids, size=args.members, replace=False):
        queries.append({"type": "member", "source_id": int(qid), "vec": base[qid]})
    for _ in range(args.midpoints):
        a, b = (int(x) for x in rng.choice(member_ids, size=2, replace=False))
        mid = (base[a] + base[b]) / 2.0
        mid = mid / (np.linalg.norm(mid) + 1e-12)
        queries.append(
            {"type": "midpoint", "source_ids": [a, b], "vec": mid.astype(np.float32)}
        )
    for qid in rng.choice(nonmember_ids, size=args.nonmembers, replace=False):
        queries.append(
            {
                "type": "nonmember",
                "source_id": int(qid),
                "vec": np.asarray(mm[qid], dtype=np.float32),
            }
        )

    # Brute-force EXACT L2 top-k over the RAW 1M slice, per query (RAM-resident matmul).
    t1 = time.time()
    sq_norms = np.einsum(
        "ij,ij->i", base, base
    )  # |x|^2 for L2 = |x|^2 - 2 q.x (+|q|^2 const)
    out_queries = []
    for i, q in enumerate(queries):
        v = q["vec"]
        d2 = sq_norms - 2.0 * (base @ v)  # rank-equivalent to full L2 (drops +|q|^2)
        top = np.argpartition(d2, args.k)[: args.k]
        top = top[np.argsort(d2[top])]
        rec = {
            "type": q["type"],
            "vec": [float(x) for x in v],
            "oracle": [int(x) for x in top],
        }
        if "source_id" in q:
            rec["source_id"] = q["source_id"]
        if "source_ids" in q:
            rec["source_ids"] = q["source_ids"]
        out_queries.append(rec)
        if (i + 1) % 50 == 0:
            print(f"[queryset] oracle {i + 1}/{len(queries)}", flush=True)
    print(f"[queryset] oracle built in {time.time() - t1:.1f}s", flush=True)

    payload = {
        "n": args.n,
        "dim": dim,
        "k": args.k,
        "metric": "l2_raw",
        "corpus_rows": int(corpus_rows),
        "seed": args.seed,
        "counts": {
            "member": args.members,
            "midpoint": args.midpoints,
            "nonmember": args.nonmembers,
            "total": len(out_queries),
        },
        "queries": out_queries,
    }
    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload))
    print(
        f"[queryset] {len(out_queries)} queries (k={args.k}) -> {outp} "
        f"({outp.stat().st_size / 1e6:.1f} MB)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

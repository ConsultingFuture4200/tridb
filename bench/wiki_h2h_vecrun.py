"""Vector-leg head-to-head over the HELD-OUT mixed query set (bench/wiki_h2h_queryset.py).

The id-based runners (wiki_h2h_run.py) feed each query as a loaded row's own id; the held-out set
mixes members / midpoints / non-members whose vectors are NOT rows in the slice, so they must be
fed as explicit vectors. This runner does that against BOTH stores and compares latency at EQUAL
recall@k vs the brute-force L2 oracle:

  TriDB   : psycopg -> engine plain HNSW scan `SELECT id FROM articles ORDER BY embedding <-> 'v'
            LIMIT k` (enable_seqscan=off). The scan self-terminates early; its recall is a SINGLE
            operating point (the hnsw_max_examined cap is inert here). Latency = client wall-clock.
  Milvus  : col.search([v], ef in --efs). Sweeps ef to trace the recall/latency curve, then the
            matched point = the ef whose recall matches TriDB's within --eps.

WARM only: run a throwaway query per side first (the disclosed ~13min TriDB cold LoadIndex is
measured separately). No fabricated win — if TriDB loses at equal recall, it is reported.

    python bench/wiki_h2h_vecrun.py --queryset bench/results/wiki_h2h_queryset.json \
        --engine-port 5446 --milvus-port 19531 --milvus-collection wiki_articles \
        --runs 5 --out bench/results/wiki_h2h_vecleg_1m.json

The engine (scripts/wiki_engine_serve.sh, advisor 044) requires a password over TCP. Pass it via
--engine-password or the TRIDB_ENGINE_PGPASSWORD env var (read from the serve script's
$OUT/pg_password); omit both only against an older trust-auth engine.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path


def recall_at_k(got, gold, k):
    g = set(gold[:k])
    return len(g & set(got[:k])) / len(g) if g else float("nan")


def _lit(v):
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def run_tridb(port, host, queries, k, runs, password=None):
    import psycopg

    conn = psycopg.connect(host=host, port=port, dbname="postgres", user="postgres",
                            password=password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET enable_seqscan = off;")
    cur.execute("SET statement_timeout = 0;")
    recs, lats = [], []
    for q in queries:
        sql = f"SELECT id FROM articles ORDER BY embedding <-> '{_lit(q['vec'])}' LIMIT {k}"
        cur.execute(sql)  # warm-up
        got = [int(r[0]) for r in cur.fetchall()]
        recs.append(recall_at_k(got, q["oracle"], k))
        ts = []
        for _ in range(runs):
            t0 = time.perf_counter()
            cur.execute(sql)
            cur.fetchall()
            ts.append((time.perf_counter() - t0) * 1e3)
        lats.append(statistics.median(ts))
    cur.close()
    conn.close()
    return {"recall": _mean(recs), "p50_ms": _pct(lats, 50), "p95_ms": _pct(lats, 95),
            "n": len(recs)}


def run_milvus(host, port, collection, metric, queries, k, runs, efs):
    from pymilvus import Collection, connections

    connections.connect(alias="vr", host=host, port=port)
    col = Collection(collection, using="vr")
    col.load()
    out = {}
    for ef in efs:
        recs, lats = [], []
        params = {"metric_type": metric, "params": {"ef": ef}}
        for q in queries:
            v = [float(x) for x in q["vec"]]
            res = col.search([v], "embedding", params, limit=k, output_fields=["id"])
            got = [int(h.id) for h in res[0]]
            recs.append(recall_at_k(got, q["oracle"], k))
            ts = []
            for _ in range(runs):
                t0 = time.perf_counter()
                col.search([v], "embedding", params, limit=k, output_fields=["id"])
                ts.append((time.perf_counter() - t0) * 1e3)
            lats.append(statistics.median(ts))
        out[f"ef{ef}"] = {"recall": _mean(recs), "p50_ms": _pct(lats, 50),
                          "p95_ms": _pct(lats, 95), "n": len(recs)}
    return out


def _mean(xs):
    xs = [x for x in xs if x == x]
    return sum(xs) / len(xs) if xs else float("nan")


def _pct(xs, p):
    import numpy as np
    return float(np.percentile(xs, p)) if xs else float("nan")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--queryset", required=True)
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--engine-host", default="localhost")
    ap.add_argument("--engine-password",
                     default=os.environ.get("TRIDB_ENGINE_PGPASSWORD", ""))
    ap.add_argument("--milvus-host", default="localhost")
    ap.add_argument("--milvus-port", default="19531")
    ap.add_argument("--milvus-collection", default="wiki_articles")
    ap.add_argument("--milvus-metric", default="COSINE")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--efs", default="16,32,64,128,256")
    ap.add_argument("--eps", type=float, default=0.02)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    qs = json.loads(Path(args.queryset).read_text())
    queries, k = qs["queries"], qs["k"]
    print(f"[vecrun] {len(queries)} held-out queries (k={k}) types={qs['counts']}", flush=True)

    t0 = time.time()
    tridb = run_tridb(args.engine_port, args.engine_host, queries, k, args.runs,
                       args.engine_password or None)
    print(f"[vecrun] TriDB recall={tridb['recall']:.4f} p50={tridb['p50_ms']:.3f}ms "
          f"p95={tridb['p95_ms']:.3f}ms ({time.time()-t0:.1f}s)", flush=True)

    efs = [int(x) for x in args.efs.split(",")]
    milvus = run_milvus(args.milvus_host, args.milvus_port, args.milvus_collection,
                        args.milvus_metric, queries, k, args.runs, efs)
    for tag, m in milvus.items():
        print(f"[vecrun] Milvus {tag} recall={m['recall']:.4f} p50={m['p50_ms']:.3f}ms", flush=True)

    # matched point: Milvus ef whose recall is closest to TriDB's within eps.
    tr = tridb["recall"]
    matched = None
    for tag, m in milvus.items():
        if abs(m["recall"] - tr) <= args.eps:
            if matched is None or abs(m["recall"] - tr) < abs(milvus[matched]["recall"] - tr):
                matched = tag

    result = {"n": qs["n"], "dim": qs["dim"], "k": k, "queries": len(queries),
              "counts": qs["counts"], "eps": args.eps, "tridb": tridb, "milvus": milvus,
              "matched_milvus_ef": matched}
    if matched:
        m = milvus[matched]
        result["comparison"] = {
            "recall_tridb": round(tr, 4), "recall_milvus": round(m["recall"], 4),
            "tridb_p50_ms": round(tridb["p50_ms"], 3), "milvus_p50_ms": round(m["p50_ms"], 3),
            "tridb_p95_ms": round(tridb["p95_ms"], 3), "milvus_p95_ms": round(m["p95_ms"], 3),
            "milvus_faster_x": round(tridb["p50_ms"] / m["p50_ms"], 2) if m["p50_ms"] else None,
        }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result.get("comparison", {"matched": None}), indent=2), flush=True)
    print(f"[vecrun] -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

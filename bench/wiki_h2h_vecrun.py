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

Per-query instrumentation (reviewer asks, v0.4.0):
  - per-query recall + median latency + query type -> recall broken out by member/midpoint/nonmember
    (50% of the set are self-recovering members that both systems ace; the discriminating recall
    lives in the harder midpoint/nonmember queries).
  - per-query root buffers via EXPLAIN(ANALYZE,BUFFERS): the out-of-manifold HNSW-entry-guard
    fallback does a ~300k-buffer full scan that returns EXACT top-k (recall 1.0) at hundreds-of-ms
    latency. We flag those queries, report TriDB max/p99 latency and the fallback count, and report
    recall with the fallback queries EXCLUDED so the headline recall is not propped up by exact
    seqscan results Milvus never gets.
  - client round-trip FLOOR for each transport (libpq SELECT 1 vs a minimal pymilvus gRPC query):
    the p50 gap is a full-stack client-latency comparison (in-process libpq vs out-of-process gRPC),
    NOT a pure index-scan benchmark. The floors quantify how much of the gap is transport.

WARM only: run a throwaway query per side first (the disclosed ~13min TriDB cold LoadIndex is
measured separately). No fabricated win -- if TriDB loses at equal recall, it is reported.

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
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

# Buffers above this on the root plan node = the out-of-manifold seqscan-style fallback,
# not the ~86-buffer self-terminating HNSW scan. Flip lands far below it.
FALLBACK_BUFFER_THRESHOLD = 1000

_HIT = re.compile(r"shared hit=(\d+)")
_READ = re.compile(r"read=(\d+)")


def recall_at_k(got, gold, k):
    g = set(gold[:k])
    return len(g & set(got[:k])) / len(g) if g else float("nan")


def _lit(v):
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def _root_buffers(cur, sql):
    """Root-node cumulative buffers touched (shared hit + read). Cumulative up the plan tree,
    so the root is the max; the fallback seqscan shows ~300k, the HNSW scan ~86."""
    try:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
        plan = cur.fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        root = plan[0]["Plan"]
        return int(root.get("Shared Hit Blocks", 0)) + int(root.get("Shared Read Blocks", 0))
    except Exception:
        # Text fallback: cumulative root = max across nodes.
        cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
        txt = "\n".join(r[0] for r in cur.fetchall())
        hit = max((int(x) for x in _HIT.findall(txt)), default=0)
        read = max((int(x) for x in _READ.findall(txt)), default=0)
        return hit + read


def _mean(xs):
    xs = [x for x in xs if x == x]
    return sum(xs) / len(xs) if xs else float("nan")


def _pct(xs, p):
    import numpy as np

    return float(np.percentile(xs, p)) if xs else float("nan")


def _recall_by_type(records):
    by = defaultdict(list)
    for r in records:
        by[r["type"]].append(r["recall"])
    return {t: round(_mean(v), 4) for t, v in sorted(by.items())}


def run_tridb(port, host, queries, k, runs, password=None):
    import psycopg

    conn = psycopg.connect(host=host, port=port, dbname="postgres", user="postgres",
                            password=password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET enable_seqscan = off;")
    cur.execute("SET statement_timeout = 0;")
    records = []
    for q in queries:
        sql = f"SELECT id FROM articles ORDER BY embedding <-> '{_lit(q['vec'])}' LIMIT {k}"
        cur.execute(sql)  # warm-up (absorbs the one-time cold LoadIndex on the very first query)
        got = [int(r[0]) for r in cur.fetchall()]
        rec = recall_at_k(got, q["oracle"], k)
        buffers = _root_buffers(cur, sql)
        fallback = buffers > FALLBACK_BUFFER_THRESHOLD
        ts = []
        for _ in range(runs):
            t0 = time.perf_counter()
            cur.execute(sql)
            cur.fetchall()
            ts.append((time.perf_counter() - t0) * 1e3)
        records.append({"type": q["type"], "recall": rec, "lat_ms": statistics.median(ts),
                        "buffers": buffers, "fallback": fallback})
    # client round-trip floor: pure libpq round-trip, no table access.
    floor = []
    for _ in range(50):
        t0 = time.perf_counter()
        cur.execute("SELECT 1")
        cur.fetchall()
        floor.append((time.perf_counter() - t0) * 1e3)
    cur.close()
    conn.close()

    lats = [r["lat_ms"] for r in records]
    kept = [r for r in records if not r["fallback"]]
    fb = [r for r in records if r["fallback"]]
    return {
        "recall": _mean([r["recall"] for r in records]),
        "recall_excl_fallback": _mean([r["recall"] for r in kept]),
        "recall_by_type": _recall_by_type(records),
        "p50_ms": _pct(lats, 50), "p95_ms": _pct(lats, 95),
        "p99_ms": _pct(lats, 99), "max_ms": max(lats) if lats else float("nan"),
        "fallback_count": len(fb),
        "fallback_lats_ms": sorted(round(r["lat_ms"], 3) for r in fb),
        "fallback_max_buffers": max((r["buffers"] for r in fb), default=0),
        "floor_ms": _pct(floor, 50),
        "n": len(records), "n_excl_fallback": len(kept),
    }


def run_milvus(host, port, collection, metric, queries, k, runs, efs):
    from pymilvus import Collection, connections

    connections.connect(alias="vr", host=host, port=port)
    col = Collection(collection, using="vr")
    col.load()
    out = {}
    for ef in efs:
        records = []
        params = {"metric_type": metric, "params": {"ef": ef}}
        for q in queries:
            v = [float(x) for x in q["vec"]]
            res = col.search([v], "embedding", params, limit=k, output_fields=["id"])
            got = [int(h.id) for h in res[0]]
            rec = recall_at_k(got, q["oracle"], k)
            ts = []
            for _ in range(runs):
                t0 = time.perf_counter()
                col.search([v], "embedding", params, limit=k, output_fields=["id"])
                ts.append((time.perf_counter() - t0) * 1e3)
            records.append({"type": q["type"], "recall": rec, "lat_ms": statistics.median(ts)})
        lats = [r["lat_ms"] for r in records]
        out[f"ef{ef}"] = {"recall": _mean([r["recall"] for r in records]),
                          "recall_by_type": _recall_by_type(records),
                          "p50_ms": _pct(lats, 50), "p95_ms": _pct(lats, 95),
                          "p99_ms": _pct(lats, 99), "n": len(records)}
    # client round-trip floor: minimal gRPC search-path round-trip (point query).
    floor = []
    for _ in range(50):
        t0 = time.perf_counter()
        col.query(expr="id == 0", output_fields=["id"], limit=1)
        floor.append((time.perf_counter() - t0) * 1e3)
    out["_floor_ms"] = _pct(floor, 50)
    return out


def _interp_equal_recall_p50(milvus, target_recall):
    """Linear-interpolate Milvus p50 at exactly TriDB's recall, between the two bracketing efs.
    The matched ef sits ABOVE TriDB's recall (does more work), so the raw ratio is an upper bound;
    this is the honest equal-recall number."""
    pts = sorted((m["recall"], m["p50_ms"]) for tag, m in milvus.items()
                 if tag.startswith("ef"))
    lo = hi = None
    for rec, p50 in pts:
        if rec <= target_recall:
            lo = (rec, p50)
        if rec >= target_recall and hi is None:
            hi = (rec, p50)
    if lo and hi and hi[0] != lo[0]:
        frac = (target_recall - lo[0]) / (hi[0] - lo[0])
        return lo[1] + frac * (hi[1] - lo[1])
    return None


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
    print(f"[vecrun] TriDB recall={tridb['recall']:.4f} (excl-fallback "
          f"{tridb['recall_excl_fallback']:.4f}) p50={tridb['p50_ms']:.3f}ms p95={tridb['p95_ms']:.3f}ms "
          f"p99={tridb['p99_ms']:.3f}ms max={tridb['max_ms']:.3f}ms fallback={tridb['fallback_count']} "
          f"floor={tridb['floor_ms']:.3f}ms ({time.time()-t0:.1f}s)", flush=True)
    print(f"[vecrun] TriDB recall_by_type={tridb['recall_by_type']}", flush=True)

    efs = [int(x) for x in args.efs.split(",")]
    milvus = run_milvus(args.milvus_host, args.milvus_port, args.milvus_collection,
                        args.milvus_metric, queries, k, args.runs, efs)
    milvus_floor = milvus.pop("_floor_ms", None)
    for tag, m in milvus.items():
        print(f"[vecrun] Milvus {tag} recall={m['recall']:.4f} p50={m['p50_ms']:.3f}ms", flush=True)
    print(f"[vecrun] Milvus floor={milvus_floor:.3f}ms", flush=True)

    # matched point: Milvus ef whose recall is closest to TriDB's within eps.
    tr = tridb["recall"]
    matched = None
    for tag, m in milvus.items():
        if abs(m["recall"] - tr) <= args.eps:
            if matched is None or abs(m["recall"] - tr) < abs(milvus[matched]["recall"] - tr):
                matched = tag

    result = {"n": qs["n"], "dim": qs["dim"], "k": k, "queries": len(queries),
              "counts": qs["counts"], "eps": args.eps, "tridb": tridb, "milvus": milvus,
              "matched_milvus_ef": matched,
              "client_floor_ms": {"tridb_libpq": round(tridb["floor_ms"], 3),
                                  "milvus_grpc": round(milvus_floor, 3) if milvus_floor else None}}
    if matched:
        m = milvus[matched]
        interp = _interp_equal_recall_p50(milvus, tr)
        result["comparison"] = {
            "recall_tridb": round(tr, 4), "recall_milvus": round(m["recall"], 4),
            "tridb_p50_ms": round(tridb["p50_ms"], 3), "milvus_p50_ms": round(m["p50_ms"], 3),
            "tridb_p95_ms": round(tridb["p95_ms"], 3), "milvus_p95_ms": round(m["p95_ms"], 3),
            # TriDB p50 is LOWER, so TriDB is FASTER: milvus_p50 / tridb_p50 > 1.
            "tridb_faster_x": round(m["p50_ms"] / tridb["p50_ms"], 2) if tridb["p50_ms"] else None,
            # matched ef sits above TriDB's recall, so the above is an upper bound; interpolate to
            # exactly TriDB's recall for the honest equal-recall multiplier.
            "milvus_p50_at_tridb_recall_ms": round(interp, 3) if interp else None,
            "tridb_faster_x_equal_recall": round(interp / tridb["p50_ms"], 2)
            if interp and tridb["p50_ms"] else None,
            # how much of the gap is transport, not index scan:
            "gap_ms": round(m["p50_ms"] - tridb["p50_ms"], 3),
            "transport_floor_gap_ms": round(milvus_floor - tridb["floor_ms"], 3)
            if milvus_floor else None,
        }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result.get("comparison", {"matched": None}), indent=2), flush=True)
    print(f"[vecrun] -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

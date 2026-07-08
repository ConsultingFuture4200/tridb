"""EXECUTED matched wiki head-to-head driver (DEV-1354, Milestone A -> executed).

Reuses the recall-tuned core of `bench/wiki_h2h.py` (the exact fused oracle, recall grading,
query sampling, and the publication gates) but measures BOTH sides CLIENT-SIDE over TCP so the
timers are at the SAME boundary (timer parity, honestly earned rather than asserted):

  * TriDB   : psycopg -> the persistent engine's published PG port; one in-process `tjs_open`
              call per query. Latency = client wall-clock over the TCP round-trip. examined /
              bridges via the session counter functions. The TR-1 work cap GUC
              (`vectordb.tjs_open_max_examined`) is SWEPT to trace the recall/latency curve.
  * baseline: pymilvus ANN (seed) -> neo4j hop -> pgvector rerank, fused app-side, client
              wall-clock over the three TCP round-trips. For a matched N<store-N run, every leg
              is capped to id < WH_ID_CAP so the baseline answers over the SAME induced slice.

Two points (both dim-384, RAM-resident = the COMPUTE-bound regime, NOT the I/O thesis):
  point1  fused graph+vector @ N=200,000 (engine loaded at 200k; baseline capped to id<200k).
  point2  vector-leg-only @ N=1,000,000 (tjs_open hops=0/no bridges vs Milvus ANN, both @ 1M).

Compare latency (p50/p95) ONLY at EQUAL recall@10 vs the exact induced-subgraph oracle.
No fabricated win; if the vector leg walls (the intermittent 1M HNSW relaxed-monotonicity hang,
examined=0), report the honest blocker per ADR-0017.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import numpy as np

from bench.wiki_h2h import (
    Cfg,
    compute_oracle,
    load_emb,
    load_induced_adj,
    recall_at_k,
    sample_queries,
    _connect_baseline,
)


# ------------------------------------------------------------------ TriDB (client-side) -----
def _vec_lit(v) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def measure_tridb(port, table, emb, qids, grid, *, k, runs, host="localhost"):
    """grid = [(m_seeds, hops, term_cond, cap), ...]; returns {tag: {qid: {...}}}."""
    import psycopg

    conn = psycopg.connect(host=host, port=port, dbname="postgres", user="postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET enable_seqscan = off;")
    out: dict[str, dict] = {}
    for (ms, hops, tc, cap) in grid:
        tag = f"m{ms}h{hops}t{tc}c{cap}"
        cur.execute(f"SET vectordb.tjs_open_max_examined = {cap};")
        per: dict[int, dict] = {}
        for qid in qids:
            qv = _vec_lit(emb[qid])
            expr = f"embedding <-> '{qv}'"
            expr_lit = "'" + expr.replace("'", "''") + "'"
            sql = (
                f"SELECT t.id FROM tjs_open('{table}', {k}, {tc}, {ms}, {hops}, "
                f"'id', '', {expr_lit}) AS t(id bigint)"
            )

            def one():
                t0 = time.perf_counter()
                cur.execute(sql)
                rows = cur.fetchall()
                dt = (time.perf_counter() - t0) * 1e3
                return [int(r[0]) for r in rows], dt

            top, _ = one()  # warm-up (also the graded id set)
            cur.execute("SELECT tjs_open_candidates_examined(), tjs_open_bridges_injected();")
            ex, br = cur.fetchone()
            times = []
            for _ in range(runs):
                top, dt = one()
                times.append(dt)
            per[qid] = {
                "ids": top,
                "median_ms": float(statistics.median(times)),
                "examined": int(ex),
                "bridges": int(br),
            }
        out[tag] = per
    cur.close()
    conn.close()
    return out


# ------------------------------------------------------------------ baseline (client-side) --
def measure_baseline(cfg, emb, qids, grid, *, k, runs, id_cap, efs, vector_only=False):
    """grid=[(seeds,hops),...]. id_cap: keep only id<id_cap on every leg (matched slice).
    vector_only: skip the graph hop (Milvus ANN -> pgvector rerank of the seeds only)."""
    col, driver, pg = _connect_baseline(cfg)
    curpg = pg.cursor()
    expr = f"id < {id_cap}" if id_cap else None

    def milvus_seed(qv, seeds, ef):
        res = col.search(
            [qv.tolist()], "embedding",
            {"metric_type": cfg.milvus_metric, "params": {"ef": ef}},
            limit=seeds, output_fields=["id"], expr=expr,
        )
        return [int(h.id) for h in res[0]]

    def neo4j_hop(seed_ids, hops):
        # NB: the wiki loader stores Article.id as a STRING property, so seeds must be matched
        # as strings and the id-cap must toInteger() the property — an int `a.id IN $ids` or a
        # numeric `n.id < cap` silently matches NOTHING (empty graph leg => fabricated result).
        capf = f" AND all(n IN nodes(p) WHERE toInteger(n.id) < {id_cap})" if id_cap else ""
        cy = (
            f"MATCH p=(a:{cfg.neo4j_node_label})-[:{cfg.neo4j_rel}*1..{hops}]->"
            f"(b:{cfg.neo4j_node_label}) WHERE a.id IN $ids{capf} RETURN DISTINCT b.id AS id"
        )
        with driver.session() as s:
            return {int(r["id"]) for r in s.run(cy, ids=[str(x) for x in seed_ids])}

    def pg_rerank(qv, cand, k):
        lit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
        curpg.execute(
            f"SELECT id FROM {cfg.pg_table} WHERE id = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector LIMIT %s",
            (list(cand), lit, k),
        )
        return [int(r[0]) for r in curpg.fetchall()]

    out: dict[str, dict] = {}
    for (seeds, hops) in grid:
        for ef in efs:
            tag = f"m{seeds}h{hops}e{ef}"
            per: dict[int, dict] = {}
            for qid in qids:
                qv = emb[qid]

                def one():
                    t0 = time.perf_counter()
                    seed_ids = milvus_seed(qv, max(seeds, k), ef)
                    if vector_only:
                        reach = set(seed_ids)
                    else:
                        reach = neo4j_hop(seed_ids, hops) | set(seed_ids)
                    top = pg_rerank(qv, reach, k)
                    return top, (time.perf_counter() - t0) * 1e3

                top, _ = one()
                times = []
                for _ in range(runs):
                    top, dt = one()
                    times.append(dt)
                per[qid] = {"ids": top, "median_ms": float(statistics.median(times))}
            out[tag] = per
    curpg.close()
    pg.close()
    driver.close()
    return out


# ------------------------------------------------------------------ grading -----------------
def grade(per_combo, oracle, k, *, has_examined=False):
    out = {}
    for tag, per in per_combo.items():
        recs, lats, exs = [], [], []
        for qid, d in per.items():
            g = oracle.get(qid) or oracle.get(str(qid))
            if g is None:
                continue
            r = recall_at_k(d["ids"], g, k)
            if r == r:
                recs.append(r)
            lats.append(d["median_ms"])
            if has_examined and d.get("examined") is not None:
                exs.append(d["examined"])
        out[tag] = {
            "recall_at_k": float(np.mean(recs)) if recs else float("nan"),
            "p50_ms": float(np.percentile(lats, 50)) if lats else float("nan"),
            "p95_ms": float(np.percentile(lats, 95)) if lats else float("nan"),
            "median_examined": float(np.median(exs)) if exs else float("nan"),
            "n_queries": len(recs),
        }
    return out


def matched_point(tridb_curve, base_curve, *, eps, target=None):
    """Find the (tridb, baseline) combo pair whose recalls match within eps, preferring the
    highest matched recall (or >= target). Returns (t_tag, t_row, b_tag, b_row) or None."""
    best = None
    for tt, tr in tridb_curve.items():
        if tr["recall_at_k"] != tr["recall_at_k"]:
            continue
        for bt, br in base_curve.items():
            if br["recall_at_k"] != br["recall_at_k"]:
                continue
            if abs(tr["recall_at_k"] - br["recall_at_k"]) > eps:
                continue
            r = min(tr["recall_at_k"], br["recall_at_k"])
            if target is not None and r < target:
                continue
            score = r
            if best is None or score > best[0]:
                best = (score, tt, tr, bt, br)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


# ------------------------------------------------------------------ CLI ---------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("point", choices=["point1", "point2"])
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--engine-host", default="localhost")
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--id-cap", type=int, default=0)
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--seed", type=int, default=1354)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--eps", type=float, default=0.03)
    ap.add_argument("--out-prefix", required=True)
    ap.add_argument("--vector-only", action="store_true")
    args = ap.parse_args(argv)

    import os
    os.environ["WH_N"] = str(args.n)
    cfg = Cfg()
    emb = load_emb(cfg)
    qids = sample_queries(cfg, args.queries, args.seed, emb)

    if args.vector_only:
        adj = {}
        oracle = compute_oracle(emb, adj, qids, k=args.k, m_seeds=max(200, args.k), hops=0)
        tridb_grid = [(ms, 0, 64, cap) for ms in (10, 20, 40, 80)
                      for cap in (256, 1024, 4000, 16000)]
        base_grid = [(args.k, 0)]
        efs = [16, 32, 64, 128, 256]
        oracle_meta = {"m_seeds": max(200, args.k), "hops": 0}
    else:
        adj = load_induced_adj(cfg)
        oracle = compute_oracle(emb, adj, qids, k=args.k, m_seeds=16, hops=2)
        tridb_grid = [(ms, h, 64, cap) for ms in (8, 16) for h in (1, 2)
                      for cap in (128, 256, 512, 1024, 2048, 4000)]
        base_grid = [(4, 1), (8, 1), (8, 2), (16, 2)]
        efs = [32, 64, 128, 256]
        oracle_meta = {"m_seeds": 16, "hops": 2}

    induced_edges = sum(len(v) for v in adj.values())
    print(f"[run {args.point}] N={args.n} q={len(qids)} induced_edges={induced_edges} "
          f"tridb_combos={len(tridb_grid)}", flush=True)

    t0 = time.time()
    tridb_raw = measure_tridb(args.engine_port, "articles", emb, qids, tridb_grid,
                              k=args.k, runs=args.runs, host=args.engine_host)
    print(f"[run] tridb measured {time.time()-t0:.1f}s", flush=True)
    t0 = time.time()
    base_raw = measure_baseline(cfg, emb, qids, base_grid, k=args.k, runs=args.runs,
                                id_cap=args.id_cap, efs=efs, vector_only=args.vector_only)
    print(f"[run] baseline measured {time.time()-t0:.1f}s", flush=True)

    tridb_curve = grade(tridb_raw, oracle, args.k, has_examined=True)
    base_curve = grade(base_raw, oracle, args.k, has_examined=False)
    mp = matched_point(tridb_curve, base_curve, eps=args.eps)

    result = {
        "point": args.point, "n": args.n, "dim": cfg.dim, "k": args.k,
        "queries": len(qids), "induced_edges": induced_edges,
        "vector_only": args.vector_only, "eps": args.eps,
        "oracle_meta": oracle_meta,
        "tridb_curve": tridb_curve, "baseline_curve": base_curve,
    }
    if mp:
        tt, tr, bt, br = mp
        result["matched"] = {
            "recall": round(min(tr["recall_at_k"], br["recall_at_k"]), 4),
            "tridb_combo": tt, "tridb_recall": round(tr["recall_at_k"], 4),
            "tridb_p50_ms": round(tr["p50_ms"], 3), "tridb_p95_ms": round(tr["p95_ms"], 3),
            "tridb_examined": tr["median_examined"],
            "baseline_combo": bt, "baseline_recall": round(br["recall_at_k"], 4),
            "baseline_p50_ms": round(br["p50_ms"], 3), "baseline_p95_ms": round(br["p95_ms"], 3),
        }
    Path(args.out_prefix).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_prefix + ".json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result.get("matched", {"matched": None}), indent=2), flush=True)
    print(f"[run] -> {args.out_prefix}.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FUSION head-to-head: TriDB in-process `tjs_open` vs an app-side Milvus->Neo4j->pgvector
pipeline, on FUSION-HEAVY multi-hop workloads, as a function of #hops (DEV-1354, Milestone B).

THE QUESTION this harness answers is NOT merely "who is faster once" — v0.4.0 already banked the
vector-leg point (TriDB ~2.1x at 1M, warm). It is: **does TriDB's advantage GROW with hop depth?**
The mechanism thesis is that a fused, in-process operator (one round-trip, no cross-system
serialization) pays a FLAT cost as hops increase, while an app-side multi-store pays a compounding
cost — each extra hop enlarges the intermediate result that must be shipped between three separate
processes. So we plot latency vs #hops (1,2,3) for BOTH systems AT EQUAL recall.

  TriDB    : ONE call `tjs_open('articles', k, term_cond, m_seeds, hops, 'id', '', 'embedding<->v')`
             over a TCP libpq connection to the loaded engine. Vector seed + native graph bridge +
             early-terminating vector-ranked top-k, all inside the Postgres backend.
  baseline : Milvus ANN (seed) -> ship seed ids -> Neo4j h-hop traversal -> ship reached ids ->
             pgvector exact rerank/filter -> app-side merge. THREE processes, THREE round-trips,
             the reached set serialized across each boundary.

MATCHED-RECALL CONTRACT (identical to bench/wiki_h2h.py). For each hop h we build ONE exact fused
oracle at depth h (exact top-`oracle_mseeds` by cosine UNION their h-hop induced-graph neighbours,
reranked exactly, top-k). Both systems approximate THAT oracle; recall@k = overlap with it. We
sweep each side's knobs, trace the recall/latency curve, and read latency ONLY at the operating
point where the two recalls match within eps. Deeper hop => larger oracle => the comparison stays
apples-to-apples at every depth.

TIMER PARITY (wiki_h2h publication_gate finding 2): BOTH sides are timed client-side as Python
wall-clock over a TCP connection (psycopg to the engine; pymilvus/neo4j/psycopg to the stores).
No server-side \\timing asymmetry. WARM only (a throwaway query per side first; the one-time cold
HNSW LoadIndex is measured + disclosed separately).

MECHANISM INSTRUMENTATION (the cost TriDB structurally avoids):
  baseline -> round_trips (=3, constant), bytes_shipped across each store boundary, and the
              intermediate-result cardinalities |seeds| / |reached| / |cand| — the |reached| set
              is what COMPOUNDS with hops and drives the shipped bytes.
  TriDB    -> candidates_examined (SM-3), bridges_injected (graph leg fired), root buffers (pages).

HONESTY (may become a public claim; every prior review's finding is a hard gate here):
  * Loopback caveat: the baseline is all-localhost = its BEST case (minimal glue cost). A real
    split-machine deployment only INCREASES the multi-store's per-round-trip cost. So a loopback
    TriDB win is CONSERVATIVE; a loopback TriDB loss is DECISIVE.
  * dim-384 RAM-resident = COMPUTE regime, not the spec's I/O thesis. The cold LoadIndex is disclosed.
  * Compare latency ONLY at equal recall. No fabricated win — if TriDB loses, it is reported
    cleanly (ADR-0017: value = one-WAL consistency, not raw speed).

    python -m bench.wiki_fusion run --engine-port 5447 --milvus-port 19531 --pg-port 5434 \
        --neo4j-uri bolt://localhost:7688 --n 200000 --queries 200 --hops 1,2,3 \
        --out bench/results/wiki_fusion_200k.json
    python -m bench.wiki_fusion report --results bench/results/wiki_fusion_200k.json \
        --md-out docs/benchmark_wiki_fusion_v0.1.0.md

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

import numpy as np

from bench.wiki_h2h import (
    Cfg,
    compute_oracle,
    expand,
    load_emb,
    load_induced_adj,
    recall_at_k,
    sample_queries,
)

# ID payload size in bytes when serialized across a store boundary. Neo4j receives ids as string
# Cypher params (~7 ascii bytes for a 6-7 digit id); pgvector receives them as an int8[] bind (8
# bytes each). We charge 8 bytes/id as a conservative common denominator, plus the dim-384 query
# vector (4 bytes/float) once into pgvector. Reported as an ESTIMATE alongside the raw cardinalities.
ID_BYTES = 8


# ======================================================================================
# TriDB side — live tjs_open over libpq, client-timed (timer parity with the baseline).
# ======================================================================================


def _vec_lit(v) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def _pct(xs, p):
    return float(np.percentile(xs, p)) if xs else float("nan")


def run_tridb(cfg: Cfg, port: int, host: str, emb, qids, oracle_by_hop, *, k, hops_list,
              grid, runs, cold_probe=True, password=None):
    """Sweep (m_seeds, term_cond) at each hop; client-timed p50/p95 + examined/bridges/pages.

    Returns {hop: {combo_tag: {recall, p50_ms, p95_ms, examined, bridges, pages, n}}} plus a
    top-level {"cold_loadindex_ms": ...} measured on the very first vector query in a fresh
    connection (the disclosed HNSW LoadIndex the multi-store does not pay)."""
    import psycopg

    conn = psycopg.connect(host=host, port=port, dbname="postgres", user="postgres",
                            password=password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET enable_seqscan = off;")
    cur.execute("SET statement_timeout = 0;")

    out: dict = {}
    cold_ms = None

    def tjs_sql(qv, tc, ms, hops):
        return (
            f"SELECT t.id FROM tjs_open('{cfg.engine_table}', {k}, {tc}, {ms}, {hops}, "
            f"'id', '', 'embedding <-> ''{_vec_lit(qv)}''') AS t(id bigint)"
        )

    # Cold LoadIndex: first vector query in this fresh backend pays the HNSW rebuild-from-heap.
    if cold_probe:
        first = grid[0]
        sql0 = tjs_sql(emb[qids[0]], first[1], first[0], hops_list[0])
        t0 = time.perf_counter()
        cur.execute(sql0)
        cur.fetchall()
        cold_ms = (time.perf_counter() - t0) * 1e3

    for hop in hops_list:
        oracle = oracle_by_hop[hop]
        by_combo: dict = {}
        for (ms, tc) in grid:
            recs, lats, exs, bris, pgs = [], [], [], [], []
            for qi, qid in enumerate(qids):
                sql = tjs_sql(emb[qid], tc, ms, hop)
                cur.execute(sql)  # warm-up + graded ids
                got = [int(r[0]) for r in cur.fetchall()]
                g = oracle.get(qid) or oracle.get(str(qid))
                r = recall_at_k(got, g, k)
                if r == r:
                    recs.append(r)
                cur.execute("SELECT tjs_open_candidates_examined(), tjs_open_bridges_injected()")
                ex, bri = cur.fetchone()
                exs.append(int(ex)); bris.append(int(bri))
                # pages: root buffers of the operator scan (SM-3 locality proxy). EXPLAIN
                # re-executes tjs_open, so sample it only on the first query per combo.
                if qi < 3:
                    try:
                        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
                        plan = cur.fetchone()[0]
                        if isinstance(plan, str):
                            plan = json.loads(plan)
                        root = plan[0]["Plan"]
                        pgs.append(int(root.get("Shared Hit Blocks", 0)) + int(root.get("Shared Read Blocks", 0)))
                    except Exception:
                        pass
                ts = []
                for _ in range(runs):
                    t0 = time.perf_counter()
                    cur.execute(sql)
                    cur.fetchall()
                    ts.append((time.perf_counter() - t0) * 1e3)
                lats.append(statistics.median(ts))
            tag = f"m{ms}t{tc}"
            by_combo[tag] = {
                "recall": float(np.mean(recs)) if recs else float("nan"),
                "p50_ms": _pct(lats, 50), "p95_ms": _pct(lats, 95),
                "examined": float(np.median(exs)) if exs else float("nan"),
                "bridges": float(np.median(bris)) if bris else float("nan"),
                "pages": float(np.median(pgs)) if pgs else float("nan"),
                "n": len(recs),
            }
        out[hop] = by_combo

    # libpq round-trip floor (transport component).
    floor = []
    for _ in range(50):
        t0 = time.perf_counter(); cur.execute("SELECT 1"); cur.fetchall()
        floor.append((time.perf_counter() - t0) * 1e3)
    cur.close(); conn.close()
    return {"by_hop": out, "cold_loadindex_ms": cold_ms, "floor_ms": _pct(floor, 50)}


# ======================================================================================
# Baseline side — Milvus -> Neo4j -> pgvector, client-timed + mechanism instrumentation.
# ======================================================================================


def run_baseline(cfg: Cfg, emb, qids, oracle_by_hop, *, k, hops_list, grid, efs, runs):
    from pymilvus import Collection, connections
    from neo4j import GraphDatabase
    import psycopg

    connections.connect(alias="wf", host=cfg.milvus_host, port=cfg.milvus_port)
    col = Collection(cfg.milvus_collection, using="wf")
    col.load()
    driver = GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))
    pg = psycopg.connect(host=cfg.pg_host, port=cfg.pg_port, dbname=cfg.pg_db,
                         user=cfg.pg_user, password=cfg.pg_password)
    pgcur = pg.cursor()

    def milvus_seed(qv, seeds, ef):
        res = col.search([qv.tolist()], "embedding",
                         {"metric_type": cfg.milvus_metric, "params": {"ef": ef}},
                         limit=seeds, output_fields=["id"])
        return [int(h.id) for h in res[0]]

    def neo4j_hop(seed_ids, hops):
        cy = (f"MATCH (a:{cfg.neo4j_node_label})-[:{cfg.neo4j_rel}*1..{hops}]->"
              f"(b:{cfg.neo4j_node_label}) WHERE a.id IN $ids RETURN DISTINCT b.id AS id")
        with driver.session() as s:
            rows = s.run(cy, ids=[str(x) for x in seed_ids])
            return {int(r["id"]) for r in rows}

    def pg_rerank(qv, cand, k):
        lit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
        pgcur.execute(
            f"SELECT id FROM {cfg.pg_table} WHERE id = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector LIMIT %s", (list(cand), lit, k))
        return [int(r[0]) for r in pgcur.fetchall()]

    out: dict = {}
    floor = []
    for _ in range(50):
        t0 = time.perf_counter(); col.query(expr="id == 0", output_fields=["id"], limit=1)
        floor.append((time.perf_counter() - t0) * 1e3)

    for hop in hops_list:
        oracle = oracle_by_hop[hop]
        by_combo: dict = {}
        for (seeds, _h) in grid:
            for ef in efs:
                recs, lats = [], []
                n_seeds, n_reached, n_cand, bytes_ship = [], [], [], []
                for qid in qids:
                    qv = emb[qid]

                    def one():
                        t0 = time.perf_counter()
                        seed_ids = milvus_seed(qv, seeds, ef)
                        t1 = time.perf_counter()
                        reach = neo4j_hop(seed_ids, hop)
                        cand = reach | set(seed_ids)
                        t2 = time.perf_counter()
                        top = pg_rerank(qv, cand, k)
                        t3 = time.perf_counter()
                        return top, seed_ids, reach, cand, ((t1-t0)*1e3, (t2-t1)*1e3, (t3-t2)*1e3)

                    top, seed_ids, reach, cand, _ = one()  # warm-up
                    g = oracle.get(qid) or oracle.get(str(qid))
                    r = recall_at_k(top, g, k)
                    if r == r:
                        recs.append(r)
                    n_seeds.append(len(seed_ids)); n_reached.append(len(reach)); n_cand.append(len(cand))
                    # bytes shipped across store boundaries: seeds into neo4j, reached out of neo4j,
                    # cand + query vector into pg. round_trips is a constant 3 regardless of hop.
                    bytes_ship.append(len(seed_ids)*ID_BYTES + len(reach)*ID_BYTES
                                      + len(cand)*ID_BYTES + cfg.dim*4)
                    ts = []
                    for _ in range(runs):
                        t0 = time.perf_counter(); one(); ts.append((time.perf_counter()-t0)*1e3)
                    lats.append(statistics.median(ts))
                tag = f"m{seeds}e{ef}"
                by_combo[tag] = {
                    "recall": float(np.mean(recs)) if recs else float("nan"),
                    "p50_ms": _pct(lats, 50), "p95_ms": _pct(lats, 95),
                    "round_trips": 3,
                    "seeds_med": float(np.median(n_seeds)),
                    "reached_med": float(np.median(n_reached)),
                    "cand_med": float(np.median(n_cand)),
                    "bytes_shipped_med": float(np.median(bytes_ship)),
                    "n": len(recs),
                }
        out[hop] = by_combo
    pgcur.close(); pg.close(); driver.close()
    return {"by_hop": out, "floor_ms": _pct(floor, 50)}


# ======================================================================================
# Matched-recall operating points + report
# ======================================================================================


def _best_at_target(curve: dict, target: float):
    """Lowest-p50 combo whose recall >= target."""
    ok = [(t, c) for t, c in curve.items() if c["recall"] == c["recall"] and c["recall"] >= target]
    return min(ok, key=lambda kv: kv[1]["p50_ms"]) if ok else None


def _max_recall(curve: dict) -> float:
    return max((c["recall"] for c in curve.values() if c["recall"] == c["recall"]), default=float("nan"))


def matched_points(tridb_by_hop, baseline_by_hop, hops_list, eps):
    """Per hop: a common recall target both can hit, then each side's lowest-latency combo there."""
    out = {}
    for hop in hops_list:
        tc, bc = tridb_by_hop[hop], baseline_by_hop[hop]
        feasible = min(_max_recall(tc), _max_recall(bc))
        # step target down from 0.9 to a feasible common level
        target = None
        for cand in (0.90, 0.85, 0.80, 0.75, 0.70, 0.60, 0.50):
            if cand <= feasible:
                target = cand
                break
        if target is None:
            out[hop] = {"target": None, "feasible": feasible}
            continue
        tp = _best_at_target(tc, target)
        bp = _best_at_target(bc, target)
        rec = {"target": target, "feasible": round(feasible, 4)}
        if tp:
            rec["tridb"] = {"combo": tp[0], **{kk: tp[1][kk] for kk in
                            ("recall", "p50_ms", "p95_ms", "examined", "bridges", "pages")}}
        if bp:
            rec["baseline"] = {"combo": bp[0], **{kk: bp[1][kk] for kk in
                              ("recall", "p50_ms", "p95_ms", "round_trips", "seeds_med",
                               "reached_med", "cand_med", "bytes_shipped_med")}}
        if tp and bp:
            dr = abs(tp[1]["recall"] - bp[1]["recall"])
            rec["recall_matched"] = dr <= eps
            rec["recall_delta"] = round(dr, 4)
            rec["tridb_faster_x"] = (round(bp[1]["p50_ms"] / tp[1]["p50_ms"], 2)
                                     if tp[1]["p50_ms"] else None)
        out[hop] = rec
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--engine-port", type=int, required=True)
    r.add_argument("--engine-host", default="localhost")
    r.add_argument("--engine-password",
                    default=os.environ.get("TRIDB_ENGINE_PGPASSWORD", ""))
    r.add_argument("--milvus-port", default="19531")
    r.add_argument("--pg-port", default="5434")
    r.add_argument("--neo4j-uri", default="bolt://localhost:7688")
    r.add_argument("--n", type=int, default=200000)
    r.add_argument("--queries", type=int, default=200)
    r.add_argument("--seed", type=int, default=1354)
    r.add_argument("--k", type=int, default=10)
    r.add_argument("--hops", default="1,2,3")
    r.add_argument("--oracle-mseeds", type=int, default=16)
    r.add_argument("--runs", type=int, default=5)
    r.add_argument("--eps", type=float, default=0.03)
    r.add_argument("--tridb-grid", default="8,32;16,64;32,64;64,128")  # m_seeds,term_cond
    r.add_argument("--baseline-grid", default="8;16;32;64")            # seeds
    r.add_argument("--efs", default="32,64,128,256")
    r.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    cfg = Cfg()
    cfg.n = a.n  # dataclass default was bound at import (before any WH_N); set the real slice size
    cfg.milvus_port = a.milvus_port
    cfg.pg_port = a.pg_port
    cfg.neo4j_uri = a.neo4j_uri

    hops_list = [int(x) for x in a.hops.split(",")]
    tgrid = [tuple(int(x) for x in p.split(",")) for p in a.tridb_grid.split(";")]
    bgrid = [(int(s), 0) for s in a.baseline_grid.split(";")]
    efs = [int(x) for x in a.efs.split(",")]

    print(f"[fusion] loading emb + induced adj for N={a.n} ...", flush=True)
    emb = load_emb(cfg)
    adj = load_induced_adj(cfg)
    induced_edges = sum(len(v) for v in adj.values())
    qids = sample_queries(cfg, a.queries, a.seed, emb)
    print(f"[fusion] {len(qids)} queries, induced_edges={induced_edges}", flush=True)

    oracle_by_hop = {}
    for hop in hops_list:
        t0 = time.time()
        oracle_by_hop[hop] = compute_oracle(emb, adj, qids, k=a.k, m_seeds=a.oracle_mseeds, hops=hop)
        reach_sz = statistics.median(
            len(expand(adj, [qid], hop)) for qid in qids[:50])
        print(f"[fusion] oracle hop={hop} built {time.time()-t0:.1f}s "
              f"(median 1-seed reach ~{reach_sz:.0f})", flush=True)

    print("[fusion] running TriDB tjs_open sweep ...", flush=True)
    tridb = run_tridb(cfg, a.engine_port, a.engine_host, emb, qids, oracle_by_hop,
                      k=a.k, hops_list=hops_list, grid=tgrid, runs=a.runs,
                      password=a.engine_password or None)
    print(f"[fusion] TriDB cold LoadIndex = {tridb['cold_loadindex_ms']:.0f} ms", flush=True)
    for hop in hops_list:
        for tag, c in tridb["by_hop"][hop].items():
            print(f"[fusion] TriDB hop={hop} {tag} recall={c['recall']:.3f} "
                  f"p50={c['p50_ms']:.2f}ms examined={c['examined']:.0f} bridges={c['bridges']:.0f}",
                  flush=True)

    print("[fusion] running baseline Milvus->Neo4j->pg sweep ...", flush=True)
    baseline = run_baseline(cfg, emb, qids, oracle_by_hop, k=a.k, hops_list=hops_list,
                            grid=bgrid, efs=efs, runs=a.runs)
    for hop in hops_list:
        for tag, c in baseline["by_hop"][hop].items():
            print(f"[fusion] base hop={hop} {tag} recall={c['recall']:.3f} "
                  f"p50={c['p50_ms']:.2f}ms reached={c['reached_med']:.0f} "
                  f"bytes={c['bytes_shipped_med']:.0f}", flush=True)

    matched = matched_points(tridb["by_hop"], baseline["by_hop"], hops_list, a.eps)

    result = {
        "n": a.n, "dim": cfg.dim, "k": a.k, "queries": len(qids), "hops": hops_list,
        "oracle_mseeds": a.oracle_mseeds, "induced_edges": induced_edges, "eps": a.eps,
        "runs": a.runs,
        "cold_loadindex_ms": tridb["cold_loadindex_ms"],
        "floor_ms": {"tridb_libpq": round(tridb["floor_ms"], 3),
                     "baseline_grpc": round(baseline["floor_ms"], 3)},
        "tridb": {str(h): tridb["by_hop"][h] for h in hops_list},
        "baseline": {str(h): baseline["by_hop"][h] for h in hops_list},
        "matched": {str(h): matched[h] for h in hops_list},
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=2))
    print(f"[fusion] -> {a.out}", flush=True)

    # advantage-grows-with-hops signal
    xs = [(h, matched[h].get("tridb_faster_x")) for h in hops_list
          if matched[h].get("tridb_faster_x") is not None]
    if len(xs) >= 2:
        grows = xs[-1][1] > xs[0][1]
        print(f"[fusion] tridb_faster_x by hop: {xs} -> advantage_grows_with_hops={grows}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

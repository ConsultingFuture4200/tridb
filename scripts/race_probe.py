"""Per-query race data for the TriDB demo GIF.

At the MATCHED operating points discovered by bench.wiki_fusion (fusion_30q.json "matched"
section), take the FIRST 10 of the same seeded query set (seed 1354, 30 queries, n=200k),
warm both sides, and measure client wall-clock ms per query:
  (a) TriDB: single tjs_open call over libpq (psycopg), same SQL shape as run_tridb.
  (b) baseline: full Milvus seed -> Neo4j traverse -> pgvector rerank -> merge, same code
      shape as run_baseline's closures.
3 repeats per query per side, keep the median. Recall@10 vs the harness oracle recorded for
BOTH sides on exactly these queries (parity proof, not cherry-picked).

Query logic is imported from the harness where importable (Cfg, load_emb, load_induced_adj,
sample_queries, compute_oracle, recall_at_k, _vec_lit). The tjs SQL string and the three
baseline store calls are verbatim copies of the closures inside run_tridb/run_baseline
(they are nested functions and cannot be imported).

Honesty: same queries, same k=10, knobs taken ONLY from the sweep's matched points; both
sides timed client-side wall-clock on this machine (spark, loopback), warm.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

from bench.wiki_fusion import _vec_lit
from bench.wiki_h2h import (
    Cfg,
    compute_oracle,
    load_emb,
    load_induced_adj,
    recall_at_k,
    sample_queries,
)

SWEEP_PATH = Path.home() / "race" / "fusion_30q.json"
OUT_PATH = Path.home() / "race" / "race_data.json"

K = 10
SEED = 1354
QUERIES_TOTAL = 30  # must match the sweep run
RACE_Q = 10
REPEATS = 3
ORACLE_MSEEDS = 16  # sweep default


def eps_matched_pair(sweep: dict, hop: int):
    """Recall-parity operating point from the sweep curves.

    TriDB: lowest-p50 combo with recall >= the harness's stepped target for this hop.
    Baseline: lowest-p50 combo whose recall is within eps of THAT TriDB recall.
    This is stricter than the harness's matched_points() baseline rule (lowest-p50 with
    recall >= target), which at hop=2 picked a baseline point 0.057 BELOW TriDB's recall
    and flagged it recall_matched=false. The race must be at parity, so we enforce eps.
    """
    eps = sweep["eps"]
    target = sweep["matched"][str(hop)]["target"]
    tcurve = sweep["tridb"][str(hop)]
    bcurve = sweep["baseline"][str(hop)]
    t = min(
        ((tag, c) for tag, c in tcurve.items() if c["recall"] >= target),
        key=lambda kv: kv[1]["p50_ms"],
    )
    ok = [
        (tag, c)
        for tag, c in bcurve.items()
        if abs(c["recall"] - t[1]["recall"]) <= eps
    ]
    if not ok:
        raise SystemExit(
            f"hop {hop}: no baseline combo within eps={eps} of tridb recall"
        )
    b = min(ok, key=lambda kv: kv[1]["p50_ms"])
    return t, b


def parse_tridb_combo(tag: str):
    m = re.fullmatch(r"m(\d+)t(\d+)", tag)
    return {"m_seeds": int(m.group(1)), "term_cond": int(m.group(2))}


def parse_baseline_combo(tag: str):
    m = re.fullmatch(r"m(\d+)e(\d+)", tag)
    return {"seeds": int(m.group(1)), "ef": int(m.group(2))}


def main():
    sweep = json.loads(SWEEP_PATH.read_text())
    assert (
        sweep["n"] == 200000 and sweep["k"] == K and sweep["queries"] == QUERIES_TOTAL
    )

    cfg = Cfg()
    cfg.n = 200000
    cfg.milvus_port = "19531"
    cfg.pg_port = "5434"
    cfg.neo4j_uri = "bolt://localhost:7688"

    emb = load_emb(cfg)
    adj = load_induced_adj(cfg)
    qids_all = sample_queries(cfg, QUERIES_TOTAL, SEED, emb)
    race_qids = qids_all[:RACE_Q]
    warm_qid = qids_all[-1]  # throwaway warm query, outside the race subset

    # ---------------- TriDB side ----------------
    import psycopg

    eng = psycopg.connect(
        host="localhost", port=5447, dbname="postgres", user="postgres"
    )
    eng.autocommit = True
    ecur = eng.cursor()
    ecur.execute("SET enable_seqscan = off;")
    ecur.execute("SET statement_timeout = 0;")

    def tjs_sql(qv, tc, ms, hops):
        # verbatim from bench.wiki_fusion.run_tridb
        return (
            f"SELECT t.id FROM tjs_open('{cfg.engine_table}', {K}, {tc}, {ms}, {hops}, "
            f"'id', '', 'embedding <-> ''{_vec_lit(qv)}''') AS t(id bigint)"
        )

    def tridb_query(qv, knobs, hop):
        sql = tjs_sql(qv, knobs["term_cond"], knobs["m_seeds"], hop)
        t0 = time.perf_counter()
        ecur.execute(sql)
        got = [int(r[0]) for r in ecur.fetchall()]
        return got, (time.perf_counter() - t0) * 1e3

    # ---------------- baseline side ----------------
    from pymilvus import Collection, connections
    from neo4j import GraphDatabase

    connections.connect(alias="race", host=cfg.milvus_host, port=cfg.milvus_port)
    col = Collection(cfg.milvus_collection, using="race")
    col.load()
    driver = GraphDatabase.driver(
        cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password)
    )
    pg = psycopg.connect(
        host=cfg.pg_host,
        port=cfg.pg_port,
        dbname=cfg.pg_db,
        user=cfg.pg_user,
        password=cfg.pg_password,
    )
    pgcur = pg.cursor()

    def milvus_seed(qv, seeds, ef):
        res = col.search(
            [qv.tolist()],
            "embedding",
            {"metric_type": cfg.milvus_metric, "params": {"ef": ef}},
            limit=seeds,
            output_fields=["id"],
        )
        return [int(h.id) for h in res[0]]

    def neo4j_hop(seed_ids, hops):
        cy = (
            f"MATCH (a:{cfg.neo4j_node_label})-[:{cfg.neo4j_rel}*1..{hops}]->"
            f"(b:{cfg.neo4j_node_label}) WHERE a.id IN $ids RETURN DISTINCT b.id AS id"
        )
        with driver.session() as s:
            rows = s.run(cy, ids=[str(x) for x in seed_ids])
            return {int(r["id"]) for r in rows}

    def pg_rerank(qv, cand, k):
        lit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
        pgcur.execute(
            f"SELECT id FROM {cfg.pg_table} WHERE id = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector LIMIT %s",
            (list(cand), lit, k),
        )
        return [int(r[0]) for r in pgcur.fetchall()]

    def baseline_query(qv, knobs, hop):
        t0 = time.perf_counter()
        seed_ids = milvus_seed(qv, knobs["seeds"], knobs["ef"])
        reach = neo4j_hop(seed_ids, hop)
        cand = reach | set(seed_ids)
        top = pg_rerank(qv, cand, K)
        return top, (time.perf_counter() - t0) * 1e3

    def race_at_hop(hop: int) -> dict:
        mp = sweep["matched"][str(hop)]
        (ttag, tpt), (btag, bpt) = eps_matched_pair(sweep, hop)
        tknobs = parse_tridb_combo(ttag)
        bknobs = parse_baseline_combo(btag)
        print(
            f"[race] hop={hop} tridb={ttag}{tknobs} baseline={btag}{bknobs} "
            f"(sweep recalls {tpt['recall']:.3f}/{bpt['recall']:.3f}, "
            f"sweep p50s {tpt['p50_ms']:.2f}/{bpt['p50_ms']:.2f} ms)",
            flush=True,
        )

        t0 = time.time()
        oracle = compute_oracle(
            emb, adj, race_qids, k=K, m_seeds=ORACLE_MSEEDS, hops=hop
        )
        print(f"[race] oracle hop={hop} built in {time.time() - t0:.1f}s", flush=True)

        # warm both sides (one throwaway query each, at these knobs)
        tridb_query(emb[warm_qid], tknobs, hop)
        baseline_query(emb[warm_qid], bknobs, hop)

        rows = []
        for qi, qid in enumerate(race_qids):
            qv = emb[qid]
            gold = oracle.get(qid) or oracle.get(str(qid))

            t_ts, t_got = [], None
            for _ in range(REPEATS):
                got, ms = tridb_query(qv, tknobs, hop)
                t_got = got
                t_ts.append(ms)
            b_ts, b_got = [], None
            for _ in range(REPEATS):
                got, ms = baseline_query(qv, bknobs, hop)
                b_got = got
                b_ts.append(ms)

            row = {
                "qi": qi,
                "qid": int(qid),
                "tridb_ms": round(statistics.median(t_ts), 3),
                "baseline_ms": round(statistics.median(b_ts), 3),
                "tridb_recall": round(recall_at_k(t_got, gold, K), 4),
                "baseline_recall": round(recall_at_k(b_got, gold, K), 4),
            }
            rows.append(row)
            print(
                f"[race] hop={hop} q{qi} tridb={row['tridb_ms']}ms "
                f"base={row['baseline_ms']}ms r={row['tridb_recall']}/{row['baseline_recall']}",
                flush=True,
            )

        t_med = statistics.median(r["tridb_ms"] for r in rows)
        b_med = statistics.median(r["baseline_ms"] for r in rows)
        notes = []
        div = [
            r["qi"] for r in rows if abs(r["tridb_recall"] - r["baseline_recall"]) > 0.1
        ]
        if div:
            notes.append(
                f"per-query recall diverges >0.1 between sides on queries {div}"
            )
        losses = [r["qi"] for r in rows if r["baseline_ms"] < r["tridb_ms"]]
        if losses:
            notes.append(f"baseline beat TriDB on queries {losses} (kept in)")
        tr = statistics.mean(r["tridb_recall"] for r in rows)
        br = statistics.mean(r["baseline_recall"] for r in rows)
        notes.append(
            f"race-subset mean recall@{K}: tridb={tr:.3f} baseline={br:.3f} "
            f"(sweep matched target {mp['target']})"
        )
        if btag != mp.get("baseline", {}).get("combo"):
            notes.append(
                f"baseline combo {btag} chosen for recall parity (eps={sweep['eps']}) instead of "
                f"the harness matched_points pick {mp.get('baseline', {}).get('combo')} "
                f"(recall {mp.get('baseline', {}).get('recall'):.3f}, flagged "
                f"recall_matched={mp.get('recall_matched')})"
            )
        return {
            "hop": hop,
            "tridb_knobs": tknobs,
            "baseline_knobs": bknobs,
            "sweep_eps_matched_point": {
                "tridb": {
                    "combo": ttag,
                    "recall": tpt["recall"],
                    "p50_ms": tpt["p50_ms"],
                },
                "baseline": {
                    "combo": btag,
                    "recall": bpt["recall"],
                    "p50_ms": bpt["p50_ms"],
                },
            },
            "sweep_matched_point": mp,
            "queries": rows,
            "tridb_median_ms": round(t_med, 3),
            "baseline_median_ms": round(b_med, 3),
            "ratio": round(b_med / t_med, 2),
            "notes": notes,
        }

    hop2 = race_at_hop(2)
    hop1 = race_at_hop(1)

    out = dict(hop2)  # hop-2 race is the headline object, exact schema per spec
    out["hop1"] = hop1
    out["sweep"] = {
        "n": sweep["n"],
        "k": sweep["k"],
        "queries": sweep["queries"],
        "eps": sweep["eps"],
        "runs": sweep["runs"],
        "cold_loadindex_ms": sweep["cold_loadindex_ms"],
        "floor_ms": sweep["floor_ms"],
        "matched": sweep["matched"],
    }
    out["provenance"] = {
        "seed": SEED,
        "race_queries": "first 10 of the seeded 30-query set",
        "repeats": REPEATS,
        "timing": "client wall-clock, loopback, warm, same machine both sides",
        "corpus": "enwiki 200k, dim 384",
    }
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"[race] -> {OUT_PATH}", flush=True)

    ecur.close()
    eng.close()
    pgcur.close()
    pg.close()
    driver.close()


if __name__ == "__main__":
    main()

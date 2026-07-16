"""SM-4 recall curve for the SEEDLESS (vector-first) stock-PG tjs_open (ADR-0019 / ADR-0015 E3.3).

The fork's seedless recall knob is term_cond alone (the stream never ends before the operator
decides). On stock PG the stream is pgvector's iterative scan, whose budget
(hnsw.max_scan_tuples) can end the stream first — so recall is (term_cond, budget)-shaped.
This harness MEASURES that surface at 1M: filtered ANN (the ADR-0015 E3 probe shape, scaled),
exact oracle, client-clocked latency, per-point budget-capped fraction. Honesty: a point where
most queries ended budget-capped is labeled; recall is reported per point, never averaged
across the sweep.

Runs ON the Spark against the stock PG17 container (tjs_pg + graph_store_am + pgvector, the
1M Wikidata slice loaded by the Gate B pass). Query set: seeded-random entities with a P31
type filter of moderate selectivity (member count in [100, 100000]); the query vector is the
entity's own embedding, self excluded.

Usage:
    python -m bench.wikidata_sm4_seedless --host <container-ip> [--queries 50] \
        [--out bench/results/wd_1m_sm4_seedless.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

TERM_CONDS = [16, 64, 256]
BUDGETS = [1000, 5000, 20000, 80000]
K = 10


def connect(host: str, port: int):
    import psycopg

    return psycopg.connect(
        host=host, port=port, dbname="postgres", user="postgres", autocommit=True
    )


def sample_queries(cur, n: int, seed: int) -> list[dict]:
    """Seeded-random entities with a moderately selective P31 type (100..100k members)."""
    cur.execute(
        """
        WITH tc AS (
          SELECT t AS type_id, count(*) AS members
          FROM entities, unnest(p31) AS t
          GROUP BY t
          HAVING count(*) BETWEEN 100 AND 100000
        )
        SELECT e.id, (SELECT t FROM unnest(e.p31) t JOIN tc ON tc.type_id = t
                      ORDER BY tc.members LIMIT 1) AS type_id
        FROM entities e
        WHERE e.p31 && (SELECT array_agg(type_id) FROM tc)
        ORDER BY hashint8(e.id + %s)
        LIMIT %s
        """,
        (seed, n),
    )
    return [{"x": r[0], "t": r[1]} for r in cur.fetchall() if r[1] is not None]


def exact_oracle(cur, q: dict) -> list[int]:
    cur.execute("SET LOCAL enable_indexscan = off")
    cur.execute("SET LOCAL enable_bitmapscan = off")
    cur.execute(
        f"SELECT id FROM entities WHERE p31 @> ARRAY[%s] AND id <> %s "
        f"ORDER BY embedding <-> (SELECT embedding FROM entities WHERE id = %s) "
        f"LIMIT {K}",
        (q["t"], q["x"], q["x"]),
    )
    return [r[0] for r in cur.fetchall()]


def run_point(cur, queries, oracle, tc: int, budget: int) -> dict:
    cur.execute("SET hnsw.iterative_scan = relaxed_order")
    cur.execute("SET hnsw.max_scan_tuples = %s", (budget,))
    recalls, lats, exams = [], [], []
    capped = 0
    for q in queries:
        filt = f"p31 @> ARRAY[{int(q['t'])}] AND id <> {int(q['x'])}"
        # warm-up + graded ids
        cur.execute(
            f"SELECT t FROM tjs_open('entities', {K}, {tc}, 0, 0, 'id', %s, "
            f"(SELECT embedding FROM entities WHERE id = %s)) AS t",
            (filt, q["x"]),
        )
        ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT tjs_open_candidates_examined(), tjs_open_budget_capped()")
        ex, cap = cur.fetchone()
        exams.append(ex)
        if cap:
            capped += 1
        o = set(oracle[q["x"]])
        recalls.append(len(o & set(ids)) / max(1, len(o)))
        # timed repeats (median of 3, client-clocked over TCP)
        reps = []
        for _ in range(3):
            t0 = time.perf_counter()
            cur.execute(
                f"SELECT t FROM tjs_open('entities', {K}, {tc}, 0, 0, 'id', %s, "
                f"(SELECT embedding FROM entities WHERE id = %s)) AS t",
                (filt, q["x"]),
            )
            cur.fetchall()
            reps.append((time.perf_counter() - t0) * 1000)
        lats.append(statistics.median(reps))
    return {
        "term_cond": tc,
        "max_scan_tuples": budget,
        "recall_at_10": round(statistics.mean(recalls), 4),
        "median_latency_ms": round(statistics.median(lats), 3),
        "median_examined": statistics.median(exams),
        "budget_capped_fraction": round(capped / len(queries), 3),
        "n_queries": len(queries),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--seed", type=int, default=1354)
    ap.add_argument(
        "--out", type=Path, default=Path("bench/results/wd_1m_sm4_seedless.json")
    )
    args = ap.parse_args()

    conn = connect(args.host, args.port)
    cur = conn.cursor()

    print("[sm4] sampling queries ...")
    queries = sample_queries(cur, args.queries, args.seed)
    print(f"[sm4] {len(queries)} queries; computing exact oracle ...")
    oracle: dict[int, list[int]] = {}
    t0 = time.time()
    for q in queries:
        with conn.transaction():
            oracle[q["x"]] = exact_oracle(cur, q)
    print(f"[sm4] oracle done in {time.time() - t0:.0f}s")

    points = []
    for tc in TERM_CONDS:
        for budget in BUDGETS:
            p = run_point(cur, queries, oracle, tc, budget)
            points.append(p)
            print(
                f"[sm4] tc={tc:>4} budget={budget:>6}: recall@10={p['recall_at_10']:.3f} "
                f"lat={p['median_latency_ms']:.1f}ms examined={p['median_examined']:.0f} "
                f"capped={p['budget_capped_fraction']:.0%}"
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "n": 1002331,
                "k": K,
                "queries": len(queries),
                "seed": args.seed,
                "grid": {"term_cond": TERM_CONDS, "max_scan_tuples": BUDGETS},
                "points": points,
                "note": (
                    "SEEDLESS filtered-ANN SM-4 curve on stock PG17 + pgvector + tjs_pg "
                    "(ADR-0019); recall is (term_cond, budget)-shaped per ADR-0015 E3.3; "
                    "budget_capped_fraction discloses stream-ended-on-budget points; "
                    "latency client-clocked over TCP."
                ),
            },
            indent=1,
        )
    )
    print(f"[sm4] -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

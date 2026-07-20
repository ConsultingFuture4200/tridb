"""All-Postgres baseline vs the fused TriDB statement, SAME database (plan: allpg bench).

The hostile launch question this measures: "TriDB is just three extensions on stock
Postgres — why not plain pgvector + a relational links table + plain SQL in ONE
Postgres, with NO TriDB extension in the query path?" Both contenders run in the SAME
stock-PG17 container that passed Gate B (docs/gate_b_spike_v0.1.0.md), same box, same
session boundary (psycopg over TCP, client-clocked), same 50 pinned oracle queries
(bench/results/wd_1m_oracle.json), graded against the committed exact oracle:

  A (fused)  — the EXACT Gate B fused filter-first statement: native typed BFS
               (graph_store.gph_traverse_bfs) -> relational P31 filter -> exact
               pgvector rank. Re-measured, not reused from the Gate B transcript.
  B (all-PG) — the same logical query with no TriDB extension in the path: a
               bounded-depth recursive CTE over a materialized relational
               links(src, dst, type_id) table (SAME edge data, parity-gated
               == graph_store.gph_edge_count()) -> same P31 filter -> same exact
               pgvector rank. Tuned like the gbrain bench's relational baseline
               (covering btree index, VACUUM ANALYZE, warm cache).

Both are exact ranks over their reached set, so recall MUST match if the reached
sets match — `run` verifies per-query reach-set equality (EXCEPT both ways) and
refuses silently divergent grading.

A separate `seedless` leg reuses bench/wikidata_sm4_seedless's query shape
(filtered ANN — the one leg that actually exercises pgvector's ANN scan):
tjs_open seedless vs a plain pgvector iterative-scan SQL statement, matched on
recall against a live exact oracle.

Subcommands (run ON the box holding the container; DSN via --host/--port):
    load-links  materialize links(src,dst,type_id) from the slice shards (additive)
    run         pinned-oracle leg: A vs B, reach parity, timings -> metrics JSON
    seedless    filtered-ANN leg: tjs_open sweep point vs pgvector iterative scan
    report      metrics JSON -> markdown report

Usage (Spark):
    PYTHONPATH=~/code/tridb python bench/wd_allpg_baseline.py load-links \
        --host 172.17.0.5 --slice ~/data/wikidata_slice_1m \
        --engine-manifest ~/code/tridb/bench/results/wd_1m_pg17_engine_load_manifest.json
    ... run --host 172.17.0.5 --oracle ... --emb ... --out wd_1m_allpg_metrics.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from tools.real_corpus import recall_at_k

EXPECTED_EDGES = 7_422_959
K = 10
HOPS = 2


def connect(args, autocommit: bool = True):
    import psycopg

    return psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.db,
        user=args.user,
        autocommit=autocommit,
    )


def vec_lit(v) -> str:
    """pgvector literal — same emission as wikidata_h2h.emit_tridb_sql (stock)."""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def load_query_vec(emb_path: Path, dense_id: int, dim: int = 384) -> list[float]:
    """One anchor row, float32 + L2-normalized — mirrors wikidata_h2h.load_emb."""
    mm = np.load(emb_path, mmap_mode="r")
    v = np.asarray(mm[dense_id], dtype=np.float32)
    v = v / (np.linalg.norm(v) + 1e-12)
    return [float(x) for x in v]


# ======================================================================================
# SQL text (pure) — contender A is the Gate B statement VERBATIM (emit_tridb_sql, stock
# dialect); contender B is the strongest plain-SQL formulation found by live EXPLAIN
# iteration (plan shape documented in docs/benchmark_allpg_baseline_v0.1.0.md).
# ======================================================================================
def sql_fused(
    x: int, type_id: int, t: int, qv: str, *, hops: int = HOPS, k: int = K
) -> str:
    return (
        f"SELECT e.id FROM graph_store.gph_traverse_bfs({x}, {hops}, {type_id}) "
        f"AS t(dst) JOIN entities e ON e.id = t.dst "
        f"WHERE e.P31 @> ARRAY[{t}] AND e.id <> {x} "
        f"ORDER BY e.embedding <-> '{qv}', e.id LIMIT {k}"
    )


def sql_allpg(
    x: int, type_id: int, t: int, qv: str, *, hops: int = HOPS, k: int = K
) -> str:
    """Bounded-depth recursive CTE over links + P31 filter + exact pgvector rank.

    UNION (not UNION ALL) dedups the frontier per (dst, depth); depth < hops bounds
    the recursion at the fused statement's hop count. The outer semi-join (IN) lets
    the planner drive entities by its PK for the small reached set.
    """
    return (
        f"WITH RECURSIVE reach(dst, depth) AS ("
        f"SELECT l.dst, 1 FROM links l WHERE l.src = {x} AND l.type_id = {type_id} "
        f"UNION "
        f"SELECT l.dst, r.depth + 1 FROM reach r "
        f"JOIN links l ON l.src = r.dst AND l.type_id = {type_id} "
        f"WHERE r.depth < {hops}) "
        f"SELECT e.id FROM entities e "
        f"WHERE e.id IN (SELECT dst FROM reach WHERE dst <> {x}) "
        f"AND e.P31 @> ARRAY[{t}] "
        f"ORDER BY e.embedding <-> '{qv}', e.id LIMIT {k}"
    )


def sql_reach_parity(x: int, type_id: int, *, hops: int = HOPS) -> str:
    """Per-query reach-set equality: |A\\B|, |B\\A|, |A|, |B| in one statement."""
    return (
        f"WITH RECURSIVE reach(dst, depth) AS ("
        f"SELECT l.dst, 1 FROM links l WHERE l.src = {x} AND l.type_id = {type_id} "
        f"UNION "
        f"SELECT l.dst, r.depth + 1 FROM reach r "
        f"JOIN links l ON l.src = r.dst AND l.type_id = {type_id} "
        f"WHERE r.depth < {hops}), "
        f"a AS (SELECT dst FROM graph_store.gph_traverse_bfs({x}, {hops}, {type_id}) "
        f"AS t(dst) WHERE dst <> {x}), "
        f"b AS (SELECT DISTINCT dst FROM reach WHERE dst <> {x}) "
        f"SELECT (SELECT count(*) FROM (SELECT dst FROM a EXCEPT SELECT dst FROM b) q), "
        f"(SELECT count(*) FROM (SELECT dst FROM b EXCEPT SELECT dst FROM a) q), "
        f"(SELECT count(*) FROM a), (SELECT count(*) FROM b)"
    )


# Session setup. A = the Gate B session settings verbatim (disclosed there); B = the
# tuned all-PG session (no TriDB GUC, no TriDB function). B tuning found by live
# EXPLAIN iteration: defaults win — forcing plans (enable_seqscan=off) did not help
# the CTE plan, and work_mem is irrelevant at reach ~50. Kept minimal and honest.
SESSION_A = [
    "SET enable_seqscan = off",
    "SET graph_store.assume_dense_open = on",
]
SESSION_B: list[str] = []


def median_p95(vals: list[float]) -> tuple[float, float]:
    return (
        float(statistics.median(vals)),
        float(np.percentile(np.asarray(vals), 95)),
    )


def timed_runs(cur, sql: str, runs: int) -> list[float]:
    out = []
    for _ in range(runs):
        t0 = time.perf_counter()
        cur.execute(sql)
        cur.fetchall()
        out.append((time.perf_counter() - t0) * 1e3)
    return out


def server_exec_ms(cur, sql: str) -> tuple[float, float]:
    """(execution_ms, planning_ms) from EXPLAIN (ANALYZE, TIMING OFF) — the in-server
    timing channel (the gbrain bench's convention), immune to client/TCP/clock-regime
    noise. TIMING OFF keeps per-node instrumentation overhead out of the number."""
    cur.execute("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql)
    doc = cur.fetchone()[0]
    if isinstance(doc, str):
        doc = json.loads(doc)
    return float(doc[0]["Execution Time"]), float(doc[0]["Planning Time"])


# ======================================================================================
# load-links — materialize the relational competitor's edge table (additive only)
# ======================================================================================
def cmd_load_links(args) -> int:
    # Reuse the engine loader's pure slice logic so the links table is derived from
    # the SAME edge data by the SAME kept-edge rule (both endpoints in-slice,
    # duplicates preserved) that populated the native AM.
    from tools.wikidata_engine_load import (
        build_dense_map,
        iter_kept_edges,
        load_slice_manifest,
    )

    etype = json.loads(Path(args.engine_manifest).read_text())["engine"][
        "edge_type_map"
    ]
    manifest = load_slice_manifest(args.slice)
    print("[load-links] building dense map ...")
    qmap, _ = build_dense_map(args.slice, manifest)

    conn = connect(args)
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('links')")
    if cur.fetchone()[0] is not None:
        if not args.force:
            raise SystemExit("links table already exists — pass --force to drop+reload")
        cur.execute("DROP TABLE links")
    cur.execute(
        "CREATE TABLE links (src bigint NOT NULL, dst bigint NOT NULL, type_id int NOT NULL)"
    )

    print("[load-links] COPY streaming edges ...")
    t0 = time.time()
    n = 0
    with cur.copy("COPY links (src, dst, type_id) FROM STDIN") as copy:
        for src, pid, dst in iter_kept_edges(args.slice, manifest, qmap):
            tid = etype.get(f"P{pid}")
            if tid is None:
                raise SystemExit(
                    f"P{pid} missing from engine edge_type_map — slice/engine mismatch"
                )
            copy.write_row((src, dst, tid))
            n += 1
    print(f"[load-links] {n} rows in {time.time() - t0:.0f}s; indexing ...")
    cur.execute("CREATE INDEX links_src_type_dst ON links (src, type_id, dst)")
    cur.execute("VACUUM ANALYZE links")

    # PARITY GATE (hard): links row count == staged count == the native AM's edge count.
    cur.execute("SELECT count(*) FROM links")
    n_links = cur.fetchone()[0]
    cur.execute("SELECT graph_store.gph_edge_count()")
    n_am = cur.fetchone()[0]
    if not (n_links == n_am == EXPECTED_EDGES == n):
        raise SystemExit(
            f"EDGE PARITY FAILED: links={n_links} native_am={n_am} "
            f"staged={n} expected={EXPECTED_EDGES}"
        )
    print(f"[load-links] PARITY OK: links == native AM == {EXPECTED_EDGES} edges")
    conn.close()
    return 0


# ======================================================================================
# run — the pinned-oracle head-to-head (A vs B)
# ======================================================================================
def cmd_run(args) -> int:
    oracle_doc = json.loads(Path(args.oracle).read_text())
    queries = oracle_doc["queries"]
    oracle = oracle_doc["oracle"]
    k = oracle_doc["k"]
    hops = oracle_doc["hops"]
    etype = json.loads(Path(args.engine_manifest).read_text())["engine"][
        "edge_type_map"
    ]

    # ONE connection == ONE backend == ONE core for BOTH contenders. The GB10 has
    # heterogeneous cores (10x Cortex-X925 + 10x Cortex-A725); two backends land on
    # different core classes and the ~2-3x class gap swamps the A/B difference
    # (observed live: contender medians flipped 0.24x..2.4x across passes on split
    # connections). Session settings are the Gate B pair (disclosed); B's plan is
    # byte-identical with and without them (verified via EXPLAIN), and
    # graph_store.assume_dense_open does not participate in B's execution.
    conn_a = connect(args)
    cur_a = cur_b = conn_a.cursor()
    for s in SESSION_A:
        cur_a.execute(s)

    per_query = []
    explain = {}
    print(f"[run] {len(queries)} queries, k={k}, hops={hops}, runs={args.runs}")
    # Warm pass first (both contenders, all queries) so timing sees a warm cache.
    plans = []
    for qi, qy in enumerate(queries):
        tid = etype[f"P{qy['p']}"]
        qv = vec_lit(load_query_vec(Path(args.emb), qy["x"]))
        plans.append((qi, qy, tid, qv))
        cur_a.execute(sql_fused(qy["x"], tid, qy["t"], qv, hops=hops, k=k))
        cur_a.fetchall()
        cur_b.execute(sql_allpg(qy["x"], tid, qy["t"], qv, hops=hops, k=k))
        cur_b.fetchall()

    for qi, qy, tid, qv in plans:
        sa = sql_fused(qy["x"], tid, qy["t"], qv, hops=hops, k=k)
        sb = sql_allpg(qy["x"], tid, qy["t"], qv, hops=hops, k=k)
        # reach parity (graded before any timing)
        cur_a.execute(sql_reach_parity(qy["x"], tid, hops=hops))
        a_not_b, b_not_a, n_a, n_b = cur_a.fetchone()
        # graded ids
        cur_a.execute(sa)
        ids_a = [r[0] for r in cur_a.fetchall()]
        cur_b.execute(sb)
        ids_b = [r[0] for r in cur_b.fetchall()]
        truth = oracle[str(qi)]
        rec_a = recall_at_k(ids_a, truth, k)
        rec_b = recall_at_k(ids_b, truth, k)
        # timings — same client boundary, contenders INTERLEAVED run-by-run with
        # alternating order so both sample the same scheduling/clock window (sub-ms
        # medians on a shared box are otherwise dominated by which burst caught a
        # busy core or a low CPU-governor state; observed live — the box drifts
        # between a ~0.05 ms and a ~0.4 ms floor regime).
        t_a, t_b = [], []
        for r in range(args.runs):
            pair = [(t_a, cur_a, sa), (t_b, cur_b, sb)]
            for acc, cur, s in pair if r % 2 == 0 else reversed(pair):
                acc.extend(timed_runs(cur, s, 1))
        # server-side channel (interleaved as well)
        se_a, se_b, pl_a, pl_b = [], [], [], []
        for r in range(args.runs):
            quad = [(se_a, pl_a, cur_a, sa), (se_b, pl_b, cur_b, sb)]
            for se, pl, cur, s in quad if r % 2 == 0 else reversed(quad):
                e, p = server_exec_ms(cur, s)
                se.append(e)
                pl.append(p)
        per_query.append(
            {
                "qi": qi,
                **qy,
                "type_id": tid,
                "reach_a": n_a,
                "reach_b": n_b,
                "reach_diff": [a_not_b, b_not_a],
                "reach_equal": a_not_b == 0 and b_not_a == 0,
                "ids_equal": ids_a == ids_b,
                "recall_a": rec_a,
                "recall_b": rec_b,
                "lat_a_ms": float(statistics.median(t_a)),
                "lat_b_ms": float(statistics.median(t_b)),
                "min_a_ms": float(min(t_a)),
                "min_b_ms": float(min(t_b)),
                "exec_a_ms": float(statistics.median(se_a)),
                "exec_b_ms": float(statistics.median(se_b)),
                "plan_a_ms": float(statistics.median(pl_a)),
                "plan_b_ms": float(statistics.median(pl_b)),
                "runs_a_ms": [round(v, 4) for v in t_a],
                "runs_b_ms": [round(v, 4) for v in t_b],
            }
        )
        if qi == args.explain_qi:
            for tag, cur, s in (("A_fused", cur_a, sa), ("B_allpg", cur_b, sb)):
                cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + s)
                explain[tag] = "\n".join(r[0] for r in cur.fetchall())
        print(
            f"[run] q{qi:02d} reach A={n_a} B={n_b} eq={a_not_b == 0 and b_not_a == 0} "
            f"recall A={rec_a:.2f} B={rec_b:.2f} "
            f"lat A={statistics.median(t_a):.3f}ms B={statistics.median(t_b):.3f}ms"
        )

    lat_a = [d["lat_a_ms"] for d in per_query]
    lat_b = [d["lat_b_ms"] for d in per_query]
    med_a, p95_a = median_p95(lat_a)
    med_b, p95_b = median_p95(lat_b)
    # Paired stats: with interleaved runs the per-query (B - A) difference cancels
    # box-level noise; a bare median ratio on a shared box does not.
    diffs = [d["lat_b_ms"] - d["lat_a_ms"] for d in per_query]
    summary = {
        "n_queries": len(per_query),
        "k": k,
        "hops": hops,
        "runs_per_query": args.runs,
        "reach_all_equal": all(d["reach_equal"] for d in per_query),
        "ids_all_equal": all(d["ids_equal"] for d in per_query),
        "recall_a": float(np.mean([d["recall_a"] for d in per_query])),
        "recall_b": float(np.mean([d["recall_b"] for d in per_query])),
        "a_fused": {"median_ms": med_a, "p95_ms": p95_a},
        "b_allpg": {"median_ms": med_b, "p95_ms": p95_b},
        "b_over_a": med_b / med_a if med_a else None,
        "paired": {
            "median_b_minus_a_ms": float(statistics.median(diffs)),
            "queries_b_faster": sum(1 for d in diffs if d < 0),
            "queries_a_faster": sum(1 for d in diffs if d > 0),
        },
        # min-of-runs = the contention-free floor; the most regime-robust estimator
        # on this shared box (medians can land in different clock regimes).
        "min_of_runs": {
            "a_median_ms": float(statistics.median([d["min_a_ms"] for d in per_query])),
            "b_median_ms": float(statistics.median([d["min_b_ms"] for d in per_query])),
            "a_p95_ms": float(np.percentile([d["min_a_ms"] for d in per_query], 95)),
            "b_p95_ms": float(np.percentile([d["min_b_ms"] for d in per_query], 95)),
        },
        # in-server channel: EXPLAIN (ANALYZE, TIMING OFF) Execution/Planning Time,
        # median of runs per query, median/p95 across queries.
        "server_side": {
            "a_exec_median_ms": float(
                statistics.median([d["exec_a_ms"] for d in per_query])
            ),
            "b_exec_median_ms": float(
                statistics.median([d["exec_b_ms"] for d in per_query])
            ),
            "a_exec_p95_ms": float(
                np.percentile([d["exec_a_ms"] for d in per_query], 95)
            ),
            "b_exec_p95_ms": float(
                np.percentile([d["exec_b_ms"] for d in per_query], 95)
            ),
            "a_plan_median_ms": float(
                statistics.median([d["plan_a_ms"] for d in per_query])
            ),
            "b_plan_median_ms": float(
                statistics.median([d["plan_b_ms"] for d in per_query])
            ),
        },
    }
    out = _merge_out(
        args.out,
        "pinned",
        {
            "created": datetime.now(timezone.utc).isoformat(),
            "boundary": "psycopg over TCP from the Spark host, client perf_counter, "
            "execute+fetchall; warm cache; identical for both contenders",
            "session_a": SESSION_A,
            "session_b": SESSION_B,
            "summary": summary,
            "explain": explain,
            "per_query": per_query,
        },
    )
    print(json.dumps(summary, indent=2))
    print(f"[run] -> {out}")
    conn_a.close()
    return 0


# ======================================================================================
# seedless — filtered-ANN leg (tjs_open vs plain pgvector iterative scan)
# ======================================================================================
TJS_POINTS = [(16, 80000), (64, 20000), (256, 20000)]  # (term_cond, max_scan_tuples)
PGV_EF = [40, 100, 200, 400, 800]  # hnsw.ef_search sweep, iterative relaxed_order


def cmd_seedless(args) -> int:
    from bench.wikidata_sm4_seedless import exact_oracle, sample_queries

    conn = connect(args)
    cur = conn.cursor()
    print("[seedless] sampling queries ...")
    queries = sample_queries(cur, args.queries, args.seed)
    print(f"[seedless] {len(queries)} queries; exact oracle ...")
    oracle: dict[int, list[int]] = {}
    for q in queries:
        with conn.transaction():
            oracle[q["x"]] = exact_oracle(cur, q)

    def measure(point_tag: str, setup: list[str], sql_of) -> dict:
        for s in setup:
            cur.execute(s)
        recalls, lats = [], []
        for q in queries:
            s = sql_of(q)
            cur.execute(s)  # warm-up + graded ids
            ids = [r[0] for r in cur.fetchall()]
            recalls.append(recall_at_k(ids, oracle[q["x"]], K))
            lats.append(float(statistics.median(timed_runs(cur, s, args.runs))))
        med, p95 = median_p95(lats)
        pt = {
            "point": point_tag,
            "recall_at_10": round(float(np.mean(recalls)), 4),
            "median_ms": round(med, 3),
            "p95_ms": round(p95, 3),
            "n_queries": len(queries),
        }
        print(f"[seedless] {pt}")
        return pt

    def tjs_sql(tc):
        def f(q):
            return (
                f"SELECT t FROM tjs_open('entities', {K}, {tc}, 0, 0, 'id', "
                f"'p31 @> ARRAY[{int(q['t'])}] AND id <> {int(q['x'])}', "
                f"(SELECT embedding FROM entities WHERE id = {int(q['x'])})) AS t"
            )

        return f

    def pgv_sql(q):
        return (
            f"SELECT id FROM entities WHERE p31 @> ARRAY[{int(q['t'])}] "
            f"AND id <> {int(q['x'])} ORDER BY embedding <-> "
            f"(SELECT embedding FROM entities WHERE id = {int(q['x'])}) LIMIT {K}"
        )

    # plan 102 (issue #30): optionally sweep tjs.vector_scan_budget across the tjs points.
    # Empty (default) = no SET at all — byte-identical to the pre-102 harness (and safe
    # against containers whose tjs_pg predates the GUC). 0 in the list = the GUC's
    # explicit disabled value (the negative-control row).
    vsb_sweep = (
        [int(b) for b in args.tjs_vector_scan_budgets.split(",")]
        if args.tjs_vector_scan_budgets
        else [None]
    )
    tjs_points = [
        measure(
            f"tjs_open tc={tc} budget={budget}"
            + (f" vsb={vsb}" if vsb is not None else ""),
            [
                "SET hnsw.iterative_scan = relaxed_order",
                f"SET hnsw.max_scan_tuples = {budget}",
            ]
            + ([f"SET tjs.vector_scan_budget = {vsb}"] if vsb is not None else []),
            tjs_sql(tc),
        )
        for tc, budget in TJS_POINTS
        for vsb in vsb_sweep
    ]
    pgv_points = [
        measure(
            f"pgvector ef_search={ef}",
            [
                "SET hnsw.iterative_scan = relaxed_order",
                "SET hnsw.max_scan_tuples = 80000",
                f"SET hnsw.ef_search = {ef}",
            ],
            pgv_sql,
        )
        for ef in PGV_EF
    ]
    out = _merge_out(
        args.out,
        "seedless",
        {
            "created": datetime.now(timezone.utc).isoformat(),
            "note": "filtered-ANN shape from bench/wikidata_sm4_seedless (live exact "
            "oracle, same seeded queries); tjs_open seedless vs plain pgvector "
            "iterative relaxed_order scan — NO TriDB extension in the pgvector leg",
            "seed": args.seed,
            "runs_per_query": args.runs,
            "tjs_open": tjs_points,
            "pgvector_sql": pgv_points,
        },
    )
    print(f"[seedless] -> {out}")
    conn.close()
    return 0


def _merge_out(out_path: Path, key: str, payload: dict) -> Path:
    doc = json.loads(out_path.read_text()) if out_path.exists() else {}
    doc[key] = payload
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1))
    return out_path


# ======================================================================================
# report — metrics JSON -> markdown
# ======================================================================================
def cmd_report(args) -> int:
    m = json.loads(Path(args.metrics).read_text())
    p = m["pinned"]
    s = p["summary"]
    L = [
        "# All-Postgres baseline vs fused tjs statement — 1M Wikidata (stock PG17)",
        "",
        "> Both contenders in the SAME database/container (`tridb-wikidata-pg17`), same",
        "> 50 pinned oracle queries as Gate B, client-clocked psycopg over TCP, warm,",
        f"> median of {s['runs_per_query']} runs/query. Full method + honesty notes:",
        "> `docs/benchmark_allpg_baseline_v0.1.0.md`.",
        "",
        "| Contender | recall@10 | median | p95 |",
        "|---|---:|---:|---:|",
        f"| A — fused (native BFS + P31 + pgvector rank) | {s['recall_a']:.3f} "
        f"| {s['a_fused']['median_ms']:.3f} ms | {s['a_fused']['p95_ms']:.3f} ms |",
        f"| B — all-PG SQL (recursive CTE over `links`) | {s['recall_b']:.3f} "
        f"| {s['b_allpg']['median_ms']:.3f} ms | {s['b_allpg']['p95_ms']:.3f} ms |",
        "",
        f"- B / A median latency ratio: **{s['b_over_a']:.2f}×** "
        f"(paired: A faster on {s['paired']['queries_a_faster']}/{s['n_queries']} "
        f"queries, median B-A = {s['paired']['median_b_minus_a_ms'] * 1e3:.1f} us)",
        f"- server-side (EXPLAIN ANALYZE) exec median: A "
        f"{s['server_side']['a_exec_median_ms']:.3f} ms vs B "
        f"{s['server_side']['b_exec_median_ms']:.3f} ms; planning A "
        f"{s['server_side']['a_plan_median_ms']:.3f} ms vs B "
        f"{s['server_side']['b_plan_median_ms']:.3f} ms",
        f"- reach sets equal on all queries: **{s['reach_all_equal']}**; "
        f"returned ids identical: **{s['ids_all_equal']}**",
        "- C — multi-store (Milvus+Neo4j+pg): 3.34 ms at recall 0.986 "
        "(cited from Gate B, not re-run)",
        "",
    ]
    if "seedless" in m:
        sl = m["seedless"]
        L += [
            "## Seedless (filtered-ANN) leg",
            "",
            "| Point | recall@10 | median | p95 |",
            "|---|---:|---:|---:|",
        ]
        for pt in sl["tjs_open"] + sl["pgvector_sql"]:
            L.append(
                f"| {pt['point']} | {pt['recall_at_10']:.3f} | "
                f"{pt['median_ms']:.3f} ms | {pt['p95_ms']:.3f} ms |"
            )
        L.append("")
    Path(args.out).write_text("\n".join(L) + "\n")
    print(f"[report] -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="All-Postgres baseline benchmark (1M Wikidata)."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("load-links", "run", "seedless", "report"):
        p = sub.add_parser(name)
        p.add_argument("--host", default="172.17.0.5")
        p.add_argument("--port", type=int, default=5432)
        p.add_argument("--db", default="postgres")
        p.add_argument("--user", default="postgres")
        p.add_argument("--slice", type=Path, default=Path("data/wikidata_slice"))
        p.add_argument(
            "--engine-manifest",
            type=Path,
            default=Path("bench/results/wd_1m_pg17_engine_load_manifest.json"),
        )
        p.add_argument(
            "--oracle", type=Path, default=Path("bench/results/wd_1m_oracle.json")
        )
        p.add_argument(
            "--emb",
            type=Path,
            default=Path("data/wikidata_slice/emb/dense_id_aligned.npy"),
        )
        p.add_argument("--runs", type=int, default=9)
        p.add_argument("--queries", type=int, default=50)
        p.add_argument("--seed", type=int, default=1354)
        p.add_argument("--explain-qi", type=int, default=0)
        p.add_argument(
            "--tjs-vector-scan-budgets",
            default="",
            help="seedless only (plan 102 / issue #30): comma-separated "
            "tjs.vector_scan_budget sweep for the tjs_open points (e.g. '0,2000,5000'); "
            "empty = no SET (pre-102 behavior)",
        )
        p.add_argument("--force", action="store_true")
        p.add_argument(
            "--metrics",
            type=Path,
            default=Path("bench/results/wd_1m_allpg_metrics.json"),
        )
        p.add_argument(
            "--out", type=Path, default=Path("bench/results/wd_1m_allpg_metrics.json")
        )
    args = ap.parse_args(argv)
    if args.cmd == "load-links":
        return cmd_load_links(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "seedless":
        return cmd_seedless(args)
    if args.cmd == "report":
        args.out = (
            Path("bench/results/wd_1m_allpg_report.md")
            if args.out == Path("bench/results/wd_1m_allpg_metrics.json")
            else args.out
        )
        return cmd_report(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

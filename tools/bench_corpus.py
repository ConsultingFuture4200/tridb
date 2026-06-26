"""Generate the LIVE-engine benchmark SQL for the TriDB canonical query (DEV-1172/1173).

Emits ONE self-contained .sql file that, run inside the tridb/msvbase:dev image
against a throwaway cluster (see scripts/bench_live.sh), does the whole live run:

  1. Builds a real corpus: an `entities` table (id, chunk, ts int, embedding
     float8[D]) with all rows inserted BEFORE the HNSW index (the MSVBASE fork's
     HNSW AM cannot take incremental inserts into a built index — see
     test/canonical_e2e_test.sql), plus a native adjacency graph (graph_store.add_edge)
     of hub -> dst edges.
  2. For each query (a pinned src hub + a query vector + a timestamp window):
       a. runs the canonical query LIVE via tjs(...) and prints the result ids/chunks,
       b. prints tjs_candidates_examined() (SM-3 surface) in a SEPARATE statement,
       c. prints per-query latency from EXPLAIN (ANALYZE) "Execution Time",
       d. computes EXACT ground truth via a seqscan oracle (reachable-from-src dst,
          passing the ts filter, ordered by true L2 to the query vector) — the SM-4
          parity reference, and the peak-intermediate / corpus-examined counts the
          baseline materialization model is graded against.
  3. Prints everything as machine-parseable `#BENCH ...` lines that
     scripts/bench_live.sh -> bench/live_report.py turn into the bench JSON schema.

FORK SAFETY (test/canonical_e2e_test.sql:130-139): a tjs() scan and a second
scan of its own target table MUST NOT share one plpgsql block (segfaults the
Fagin-merge lifecycle). So every tjs() call, its tjs_candidates_examined() read,
and the oracle query are issued as SEPARATE top-level statements.

Deterministic: a single --seed drives numpy, so the same corpus/queries every run.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np


def _vec_literal(v) -> str:
    """Postgres float8[] / vector literal: '{a,b,c}' with full repr precision."""
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def build(args) -> tuple[str, dict]:
    rng = np.random.default_rng(args.seed)
    n = args.entities
    dim = args.dim

    # --- entities: random unit embeddings, a chunk, and a timestamp ---
    emb = rng.standard_normal((n, dim)).astype(np.float64)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    ts = rng.integers(args.time_min, args.time_max + 1, size=n)

    # --- graph: `hubs` source vertices, each fanning out to a TOPICAL neighbourhood.
    # Real Omni-RAG locality: a hub's graph neighbours are topically related, so their
    # embeddings cluster. We give each hub a centroid and draw its `fanout` dst entities
    # preferentially from the entities NEAREST that centroid. Consequence: the qualifying
    # (reachable + time-filtered) dst are DENSE in the similarity stream, so the live tjs
    # early-termination (consecutive_drops bound, ADR-0007) reaches them before firing —
    # i.e. SM-4 measures the engine's real recall on a realistic corpus, not a pathological
    # sparse-graph artifact where answers are scattered uniformly through 2000 rows.
    hubs = list(range(args.hubs))  # hub vertex ids 0..hubs-1 (also entity ids)
    edges: list[tuple[int, int]] = []
    hub_dsts: dict[int, list[int]] = {}
    hub_centroids: dict[int, list[float]] = {}
    for h in hubs:
        centroid = rng.standard_normal(dim).astype(np.float64)
        centroid /= np.linalg.norm(centroid)
        hub_centroids[h] = centroid.tolist()
        # rank all entities by closeness to this hub's centroid; take the nearest pool,
        # then sample `fanout` from it (pool > fanout so neighbourhoods overlap but differ).
        d2 = np.sum((emb - centroid) ** 2, axis=1)
        pool = np.argsort(d2)[: max(args.fanout * 3, args.fanout + 1)]
        dsts = rng.choice(pool, size=min(args.fanout, len(pool)), replace=False)
        dsts = [int(d) for d in dsts if int(d) != h]
        hub_dsts[h] = dsts
        for d in dsts:
            edges.append((h, d))

    # --- queries: each pins a hub; the query vector is drawn NEAR that hub's centroid
    # (a user asking about the hub's topic), with a small jitter; a contiguous ts window
    # selective enough to drop a real fraction of the neighbourhood (relational leg
    # load-bearing) yet leave >= k qualifying answers.
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

    # Manifest the harness uses to cross-check (and to know counts without re-parsing SQL).
    manifest = {
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
    }

    # ----------------------------------------------------------------- SQL --
    lines: list[str] = []
    w = lines.append
    w(
        "-- AUTO-GENERATED by tools/bench_corpus.py — LIVE TriDB benchmark (DEV-1172/1173)."
    )
    w("-- Do not edit by hand; regenerate via scripts/bench_live.sh.")
    w(
        f"-- corpus={n} dim={dim} hubs={args.hubs} fanout={args.fanout} "
        f"queries={len(queries)} edges={len(edges)} k={args.k}"
    )
    w("\\set ON_ERROR_STOP on")
    w("\\timing off")
    w("CREATE EXTENSION vectordb;")
    w("CREATE EXTENSION graph_store;")
    w("CREATE TABLE entities (")
    w("    id        bigint PRIMARY KEY,")
    w("    chunk     text,")
    w("    ts        int,")
    w(f"    embedding float8[{dim}]")
    w(");")

    # Bulk insert via COPY-like multi-row VALUES, chunked to keep statements sane.
    w(
        "-- all rows BEFORE the index (HNSW fork limitation: no incremental insert post-build)."
    )
    batch = 500
    for i in range(0, n, batch):
        vals = []
        for j in range(i, min(i + batch, n)):
            vals.append(
                f"({j},'chunk {j}',{int(ts[j])},'{_vec_literal(emb[j])}'::float8[])"
            )
        w(
            "INSERT INTO entities (id,chunk,ts,embedding) VALUES\n"
            + ",\n".join(vals)
            + ";"
        )

    w("CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)")
    w(f"    WITH (dimension = {dim}, distmethod = l2_distance);")

    # Graph edges.
    w("-- native adjacency graph: hub -> dst (graph_store.add_edge).")
    for s, d in edges:
        w(f"SELECT graph_store.add_edge({s}, {d});")

    # ----------------------------------------------------------------------- #
    # PHASE A — EXACT ground-truth ORACLE for every query, run FIRST on a CLEAN
    # backend (before any tjs() scan touches `entities`).
    #
    # WHY FIRST: the MSVBASE fork has a known defect where a tjs()/topk Fagin-merge
    # scan leaves the backend in a state that CORRUPTS a SUBSEQUENT plain scan of
    # the same table in the same session (docs/fork_segfault_double_scan.md). The
    # oracle is a plain seqscan of `entities`; if it runs AFTER a tjs() call it
    # returns wrong rows. So we compute ALL oracles up front (no tjs has run yet),
    # then PHASE B runs the tjs() measurements. Each oracle is exact: reachable
    # -from-src dst that pass the ts filter, ordered by TRUE L2 to the query vector.
    # ----------------------------------------------------------------------- #
    w("-- PHASE A: exact oracle (clean backend, before any tjs scan) ----------")
    w("SET enable_seqscan = on;   -- the oracle is a plain seqscan; no HNSW needed")
    for q in queries:
        qid = q["qid"]
        src = q["src"]
        qvec = _vec_literal(q["embedding"])
        win = ",".join(str(t) for t in q["window"])
        k = args.k

        # EXACT ground truth: dst reachable from src, passing the ts filter,
        # ordered by TRUE L2 to the query vector (computed in SQL over float8[]).
        #
        # We rank in `ranked` (a row_number over the d2 ordering, a PLAIN integer)
        # and aggregate ordered by THAT rank. Do NOT write
        # `array_agg(id ORDER BY d2, ...)`: re-referencing the correlated-subquery
        # column `d2` inside an aggregate's ORDER BY makes the MSVBASE fork
        # re-evaluate it incorrectly and return a WRONG ordering (reproduced:
        # array_agg-with-ORDER-BY-on-d2 diverged from the exact top-k that the inner
        # `ORDER BY d2` had already produced). Ordering by the materialised integer
        # `rn` is correct and stable.
        w("WITH reach AS (")
        w(f"  SELECT dst FROM graph_store.neighbors({src}::bigint) AS dst")
        w("), oracle AS (")
        w("  SELECT e.id,")
        w("         (SELECT sum((e.embedding[i] - q[i]) * (e.embedding[i] - q[i]))")
        w("          FROM generate_subscripts(e.embedding, 1) AS i,")
        w(f"               (SELECT '{qvec}'::float8[] AS q) AS qq")
        w("         ) AS d2")
        w("  FROM entities e JOIN reach r ON r.dst = e.id")
        w(f"  WHERE e.ts IN ({win})")
        w("), ranked AS (")
        w("  SELECT id, row_number() OVER (ORDER BY d2, id) AS rn FROM oracle")
        w(")")
        w(
            f"SELECT '#BENCH ORACLE qid={qid} ids=' || "
            f"COALESCE(string_agg(id::text, ',' ORDER BY rn), '') AS line "
            f"FROM ranked WHERE rn <= {k};"
        )

        # oracle support counts for the baseline materialization model: how many
        # dst the hub reaches (graph leg), and how many of those pass the filter.
        w(
            f"SELECT '#BENCH ORACLE_COUNTS qid={qid}'"
            f" || ' reached=' || (SELECT count(*) FROM graph_store.neighbors({src}::bigint))"
            f" || ' filtered=' || (SELECT count(*) FROM entities e "
            f"JOIN graph_store.neighbors({src}::bigint) n ON n = e.id "
            f"WHERE e.ts IN ({win})) AS line;"
        )

    # ----------------------------------------------------------------------- #
    # PHASE B — LIVE tjs() measurements per query (answer set, candidates
    # examined, EXPLAIN ANALYZE latency). Runs AFTER all oracles.
    #
    # FORK SAFETY (canonical_e2e_test.sql:130-139): a tjs() scan and a second scan
    # of its own target table MUST NOT share one plpgsql block. Each tjs() call,
    # its tjs_candidates_examined() read, and the EXPLAIN are SEPARATE top-level
    # statements (no plpgsql, no co-issued second scan).
    # ----------------------------------------------------------------------- #
    w("-- PHASE B: live tjs() measurements -----------------------------------")
    w(
        "SET enable_seqscan = off;   -- force the HNSW ANN index scan for tjs's vector leg"
    )
    # tjs() early-termination depth (VBASE consecutive past-frontier drops). 0 -> the operator's
    # built-in default (50). This is the recall/latency knob (the ANN-bench ef_search analogue):
    # at high dim / large corpus a wider window is needed for high recall@k. BENCH_TERMCOND lets the
    # harness sweep it without rebuilding the engine.
    termcond = int(os.environ.get("BENCH_TERMCOND", "0") or "0")
    for q in queries:
        qid = q["qid"]
        src = q["src"]
        qvec = _vec_literal(q["embedding"])
        win = ",".join(str(t) for t in q["window"])
        k = args.k

        w(f"\\echo #BENCH QSTART qid={qid} src={src} k={k}")

        # (a) LIVE canonical query via tjs(): print result ids in rank order.
        # array_agg over the tjs() output preserves the operator's emission order
        # (top-k already ordered by the HNSW rank authority).
        w(
            "SELECT '#BENCH TRIDB_RESULT qid=' || %d || ' ids=' || "
            "COALESCE(array_to_string(array_agg(id), ','), '') AS line FROM (" % qid
        )
        w(
            f"  SELECT t.id FROM tjs('entities', {k}, {termcond}, {src}::bigint, 'id, chunk', "
            f"'ts IN ({win})', 'embedding <-> ''{qvec}''') AS t(id bigint, chunk text)"
        )
        w(") s;")

        # (b) candidates examined by that LAST tjs scan — SEPARATE statement (fork-safe).
        w(
            f"SELECT '#BENCH TRIDB_EXAMINED qid={qid} examined=' || "
            "tjs_candidates_examined() AS line;"
        )

        # (c) latency: EXPLAIN (ANALYZE) the SAME tjs() call, scrape Execution Time.
        #     A fresh statement; EXPLAIN runs the plan once and reports the wall time.
        w(f"\\echo #BENCH EXPLAIN_BEGIN qid={qid}")
        w(
            f"EXPLAIN (ANALYZE, TIMING ON) SELECT t.id FROM tjs('entities', {k}, {termcond}, "
            f"{src}::bigint, 'id, chunk', 'ts IN ({win})', "
            f"'embedding <-> ''{qvec}''') AS t(id bigint, chunk text);"
        )
        w(f"\\echo #BENCH EXPLAIN_END qid={qid}")

        w(f"\\echo #BENCH QEND qid={qid}")

    w("\\echo #BENCH DONE")
    return "\n".join(lines) + "\n", manifest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--entities", type=int, default=2000)
    p.add_argument("--dim", type=int, default=32)
    p.add_argument("--hubs", type=int, default=12)
    p.add_argument("--fanout", type=int, default=150)
    p.add_argument("--queries", type=int, default=12)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--window", type=int, default=600)
    p.add_argument("--time-min", type=int, default=19000)
    p.add_argument("--time-max", type=int, default=20000)
    p.add_argument(
        "--query-jitter",
        type=float,
        default=0.35,
        help="stddev of the noise added to a hub centroid to form its query vector",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sql-out", required=True)
    p.add_argument("--manifest-out", required=True)
    args = p.parse_args(argv)

    sql, manifest = build(args)
    with open(args.sql_out, "w") as f:
        f.write(sql)
    with open(args.manifest_out, "w") as f:
        json.dump(manifest, f)
    print(
        f"[bench_corpus] wrote {args.sql_out} "
        f"(corpus={args.entities} dim={args.dim} hubs={args.hubs} "
        f"queries={args.queries} k={args.k}) + manifest {args.manifest_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

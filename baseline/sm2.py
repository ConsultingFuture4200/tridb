"""DEV-1171 SM-2 head-to-head: LIVE multi-system baseline vs the same corpus the
LIVE TriDB engine runs.

This module completes the baseline harness for the FAIR SM-2 comparison. It:

  1. Loads the EXACT corpus that the TriDB side runs, driven from the SAME
     ``tools/bench_corpus.py`` manifest (same seed/params => identical entity ids,
     embeddings, edges, timestamps, and the same per-query src/vector/window/k).
  2. Executes the REALIZED canonical query the live ``tjs()`` engine actually runs
     (see bench/live_report.py:baseline_query_canonical for the matching model):

        canonical (spec §5), realized on the MSVBASE fork
        -------------------------------------------------
        for one pinned src hub per query:
          graph (Neo4j)   : 1-hop (src)-[:related_to]->(dst)         -> reachable dst
          vector (Milvus) : ANN top-(k*overfetch) on the QUESTION vector over the
                            WHOLE corpus, ranked by DST embedding distance (the
                            fork's sole rank authority is the dst HNSW scan)
          relational (PG) : timestamp-window filter on the reached dst
          merge (Python)  : dst surviving all three legs, ordered by dst distance,
                            LIMIT k

     This is the SAME semantics the live ``tjs()`` engine executes, so the SM-2
     latency and the SM-4 answer-parity are like-for-like.
  3. Measures END-TO-END client wall-clock per query over WARM connections
     (connections + collection.load() + index build are one-time and excluded),
     taking the MEDIAN of >=N runs per query. This mirrors exactly how the TriDB
     side is measured (warm psql connection, median of >=N ``\\timing`` runs of the
     same ``tjs()`` query — see tools/bench_sm2_corpus.py / scripts/bench_sm2.sh).
  4. Records each system's intermediate-result sizes (the SM-1 cross-check surface).

Heavy clients (pymilvus / neo4j / psycopg) are imported lazily so a box without
the stack can still import this module for ``--help`` and unit inspection.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Reuse the connection params + lazy connect helpers from the skeleton harness.
from harness import (  # noqa: E402  (sys.path is set by the caller / __main__ block)
    Conn,
    connect_milvus,
    connect_neo4j,
    connect_postgres,
)

# Over-fetch on the ANN leg: the baseline cannot push the graph/time predicates
# into the vector scan, so it must pull k*fanout ANN candidates and prune app-side.
# Matches bench.harness.BASELINE_ANN_FANOUT (the in-process model's fanout) so the
# live baseline and the documented model agree on the over-fetch cost.
BASELINE_ANN_FANOUT = 32


# --------------------------------------------------------------------------- #
# Per-query result record
# --------------------------------------------------------------------------- #


@dataclass
class SM2QueryResult:
    """Per-query baseline measurement for the realized canonical query."""

    qid: int
    src: int
    k: int

    # latency over warm connections (milliseconds): median + all samples
    latency_total_ms: float = 0.0
    latency_samples_ms: list[float] = field(default_factory=list)
    # median per-leg latency (from the same measured runs)
    latency_graph_ms: float = 0.0
    latency_vector_ms: float = 0.0
    latency_relational_ms: float = 0.0
    latency_merge_ms: float = 0.0

    # intermediate-result set sizes (SM-1 cross-check) — from a representative run
    graph_reached_dst: int = 0  # dst reached by the 1-hop expansion from src
    vector_candidates: int = 0  # rows returned by the over-fetched Milvus ANN
    relational_filtered: int = 0  # reached dst surviving the timestamp window
    merged_candidates: int = 0  # dst surviving ALL three legs (pre top-k)
    final_results: int = 0  # rows in the final top-k answer

    result_ids: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Corpus rebuild (IDENTICAL to the TriDB side) — driven from the manifest
# --------------------------------------------------------------------------- #


def rebuild_corpus(manifest: dict, seed: int) -> dict:
    """Rebuild the EXACT corpus the live TriDB SQL ran on, from the manifest.

    Byte-for-byte mirror of tools/bench_corpus.py's numpy generation (same seed =>
    same draws) AND bench/live_report.py:rebuild_corpus, so all three systems load
    the identical embeddings / timestamps / edges / queries the live engine saw.
    Returns {"entities": {id: {timestamp, chunk, embedding}}, "edges":[(s,d)],
    "queries":[...], "dim": int}.
    """
    import numpy as np

    n = manifest["entities"]
    dim = manifest["dim"]
    rng = np.random.default_rng(seed)
    emb = rng.standard_normal((n, dim)).astype(np.float64)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    ts = rng.integers(manifest["time_min"], manifest["time_max"] + 1, size=n)

    entities = {
        i: {
            "timestamp": int(ts[i]),
            "chunk": f"chunk {i}",
            "embedding": emb[i].tolist(),
        }
        for i in range(n)
    }
    edges: list[tuple[int, int]] = []
    for h, dsts in manifest["hub_dsts"].items():
        for d in dsts:
            edges.append((int(h), int(d)))
    return {
        "entities": entities,
        "edges": edges,
        "queries": manifest["queries"],
        "dim": dim,
    }


# --------------------------------------------------------------------------- #
# One-time load into all three systems (NOT part of the measured run)
# --------------------------------------------------------------------------- #


MILVUS_INDEX = {
    "index_type": "IVF_FLAT",
    "metric_type": "L2",
    "params": {"nlist": 128},
}
MILVUS_SEARCH_PARAM = {"metric_type": "L2", "params": {"nprobe": 64}}


def load_milvus(conn: Conn, corpus: dict) -> None:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        utility,
    )

    name = conn.milvus_collection
    dim = corpus["dim"]
    if utility.has_collection(name):
        utility.drop_collection(name)
    schema = CollectionSchema(
        [
            FieldSchema("id", DataType.INT64, is_primary=True),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=dim),
        ]
    )
    col = Collection(name, schema)
    ids = sorted(corpus["entities"].keys())
    vecs = [corpus["entities"][i]["embedding"] for i in ids]
    # insert in batches (Milvus has a per-RPC size cap)
    batch = 1000
    for i in range(0, len(ids), batch):
        col.insert([ids[i : i + batch], vecs[i : i + batch]])
    col.flush()
    col.create_index("embedding", MILVUS_INDEX)
    col.load()
    print(f"[load] milvus: {len(ids)} vectors (dim={dim}) + IVF_FLAT index, loaded")


def load_neo4j(conn: Conn, corpus: dict) -> None:
    driver = connect_neo4j(conn)
    try:
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            s.run(
                "CREATE CONSTRAINT IF NOT EXISTS FOR (e:entity) REQUIRE e.id IS UNIQUE"
            )
            rows = [
                {"id": i, "ts": corpus["entities"][i]["timestamp"]}
                for i in corpus["entities"]
            ]
            s.run(
                "UNWIND $rows AS r CREATE (:entity {id: r.id, timestamp: r.ts})",
                rows=rows,
            )
            erows = [{"s": s_, "d": d_} for (s_, d_) in corpus["edges"]]
            s.run(
                "UNWIND $erows AS e "
                "MATCH (a:entity {id: e.s}), (b:entity {id: e.d}) "
                "CREATE (a)-[:related_to]->(b)",
                erows=erows,
            )
    finally:
        driver.close()
    print(
        f"[load] neo4j: {len(corpus['entities'])} :entity nodes + "
        f"{len(corpus['edges'])} :related_to edges"
    )


def load_postgres(conn: Conn, corpus: dict) -> None:
    pg = connect_postgres(conn)
    try:
        with pg.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS entity")
            cur.execute(
                "CREATE TABLE entity (id INT PRIMARY KEY, timestamp INT, chunk TEXT)"
            )
            # PGlite (the baseline PG image) does NOT support COPY FROM STDIN, so
            # load with batched multi-row INSERTs.
            ids = sorted(corpus["entities"].keys())
            batch = 500
            for i in range(0, len(ids), batch):
                chunk_ids = ids[i : i + batch]
                vals: list = []
                ph: list[str] = []
                for eid in chunk_ids:
                    e = corpus["entities"][eid]
                    ph.append("(%s,%s,%s)")
                    vals.extend([eid, e["timestamp"], e["chunk"]])
                cur.execute(
                    "INSERT INTO entity (id,timestamp,chunk) VALUES " + ",".join(ph),
                    vals,
                )
            cur.execute("CREATE INDEX entity_ts_idx ON entity (timestamp)")
        pg.commit()
    finally:
        pg.close()
    print(f"[load] postgres: {len(corpus['entities'])} entity rows + ts index")


def load_all(conn: Conn, corpus: dict) -> None:
    load_milvus(conn, corpus)
    load_neo4j(conn, corpus)
    load_postgres(conn, corpus)


# --------------------------------------------------------------------------- #
# Per-leg retrieval for the REALIZED canonical query (pinned src)
# --------------------------------------------------------------------------- #


def graph_reach(driver, src: int) -> tuple[list[int], float]:
    """1-hop :related_to expansion from the single pinned src (Neo4j).

    Returns (reached_dst_ids, latency_ms).
    """
    t0 = time.perf_counter()
    with driver.session() as s:
        res = s.run(
            "MATCH (src:entity {id: $src})-[:related_to]->(dst:entity) "
            "RETURN dst.id AS dst",
            src=src,
        )
        dst = [int(r["dst"]) for r in res]
    return dst, (time.perf_counter() - t0) * 1000.0


def vector_rank(alias, conn: Conn, question_embedding: list[float], k: int):
    """ANN over the WHOLE corpus, over-fetched k*fanout, ranked by dst distance.

    Returns ({entity_id: distance}, latency_ms). The over-fetch is exactly the
    intermediate blowup SM-1 measures: the baseline cannot push the graph/time
    predicates into the ANN scan.
    """
    from pymilvus import Collection

    t0 = time.perf_counter()
    col = Collection(conn.milvus_collection, using=alias)
    limit = k * BASELINE_ANN_FANOUT
    res = col.search(
        data=[question_embedding],
        anns_field="embedding",
        param=MILVUS_SEARCH_PARAM,
        limit=limit,
        output_fields=["id"],
    )
    hits = {int(h.id): float(h.distance) for h in res[0]}
    return hits, (time.perf_counter() - t0) * 1000.0


def relational_filter(pg, dst_ids: list[int], time_range: list[int]):
    """Timestamp-window filter on the reached dst (Postgres).

    Returns ({dst_id: chunk}, latency_ms).
    """
    t0 = time.perf_counter()
    kept: dict[int, str] = {}
    if dst_ids and time_range:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT id, chunk FROM entity "
                "WHERE id = ANY(%s) AND timestamp = ANY(%s)",
                (dst_ids, time_range),
            )
            for row in cur.fetchall():
                kept[int(row[0])] = row[1]
    return kept, (time.perf_counter() - t0) * 1000.0


def merge_canonical(
    reached_dst: list[int],
    vector_dist: dict[int, float],
    kept_dst: dict[int, str],
    k: int,
):
    """Merge the three legs into the final top-k (realized canonical semantics).

    ANN-pruned merge: models the real Milvus over-fetch (k*32); the exact-oracle
    variant lives only in bench/harness.py:baseline_query_inprocess's spec-model.

    A dst qualifies iff it is (a) reached from src, (b) survived the time filter,
    and (c) appeared in the over-fetched ANN candidate set (has a distance). The
    survivors are ordered by DST embedding distance to the question vector (the
    fork's rank authority) and the top-k ids returned.

    Returns (result_ids, merged_count, latency_ms).
    """
    t0 = time.perf_counter()
    reached = set(reached_dst)
    survivors: list[tuple[float, int]] = []
    for dst in kept_dst:
        if dst in reached and dst in vector_dist:
            survivors.append((vector_dist[dst], dst))
    survivors.sort(key=lambda x: x[0])  # ascending L2 = most similar
    result_ids = [dst for _, dst in survivors[:k]]
    return result_ids, len(survivors), (time.perf_counter() - t0) * 1000.0


# --------------------------------------------------------------------------- #
# One measured run of one query (end-to-end, warm connections)
# --------------------------------------------------------------------------- #


def _run_once(query: dict, k: int, drivers: dict, conn: Conn) -> dict:
    """Execute the realized canonical query once across the three systems.

    Returns a dict of per-leg latencies + intermediate sizes + result ids +
    total end-to-end wall-clock (sum of the measured legs, the client-visible
    cost of the merged query).
    """
    src = int(query["src"])
    q_emb = query["embedding"]
    window = query["window"]

    t_start = time.perf_counter()
    reached_dst, lat_graph = graph_reach(drivers["neo4j"], src)
    vector_dist, lat_vec = vector_rank(drivers["milvus"], conn, q_emb, k)
    kept_dst, lat_rel = relational_filter(drivers["postgres"], reached_dst, window)
    result_ids, merged, lat_merge = merge_canonical(
        reached_dst, vector_dist, kept_dst, k
    )
    total = (time.perf_counter() - t_start) * 1000.0

    return {
        "result_ids": result_ids,
        "graph_reached_dst": len(reached_dst),
        "vector_candidates": len(vector_dist),
        "relational_filtered": len(kept_dst),
        "merged_candidates": merged,
        "final_results": len(result_ids),
        "latency_total_ms": total,
        "latency_graph_ms": lat_graph,
        "latency_vector_ms": lat_vec,
        "latency_relational_ms": lat_rel,
        "latency_merge_ms": lat_merge,
    }


def run_query(
    query: dict, k: int, drivers: dict, conn: Conn, runs: int
) -> SM2QueryResult:
    """Run one query `runs` times over warm connections; take the MEDIAN latency.

    A single warm-up run primes any per-query caches before the measured runs, so
    the median reflects steady-state warm latency (the same way the TriDB side
    discards its first run). Intermediate sizes + result ids are deterministic
    across runs, so they are taken from the last measured run.
    """
    qid = int(query["qid"])
    src = int(query["src"])

    # warm-up (not measured)
    _run_once(query, k, drivers, conn)

    samples: list[dict] = [_run_once(query, k, drivers, conn) for _ in range(runs)]
    totals = [s["latency_total_ms"] for s in samples]
    last = samples[-1]

    def med(key: str) -> float:
        return statistics.median(s[key] for s in samples)

    return SM2QueryResult(
        qid=qid,
        src=src,
        k=k,
        latency_total_ms=statistics.median(totals),
        latency_samples_ms=[round(t, 4) for t in totals],
        latency_graph_ms=round(med("latency_graph_ms"), 4),
        latency_vector_ms=round(med("latency_vector_ms"), 4),
        latency_relational_ms=round(med("latency_relational_ms"), 4),
        latency_merge_ms=round(med("latency_merge_ms"), 4),
        graph_reached_dst=last["graph_reached_dst"],
        vector_candidates=last["vector_candidates"],
        relational_filtered=last["relational_filtered"],
        merged_candidates=last["merged_candidates"],
        final_results=last["final_results"],
        result_ids=last["result_ids"],
    )


# --------------------------------------------------------------------------- #
# Driver: load + run over the whole corpus
# --------------------------------------------------------------------------- #


def run_baseline(
    manifest: dict, seed: int, k: int, runs: int, conn: Conn, do_load: bool
) -> dict:
    """Load (optional) then run every query, returning the baseline SM-2 payload."""
    corpus = rebuild_corpus(manifest, seed)
    print(
        f"[sm2-baseline] corpus: {len(corpus['entities'])} entities, dim={corpus['dim']}, "
        f"{len(corpus['edges'])} edges, {len(manifest['queries'])} queries, k={k}, runs={runs}"
    )

    # Establish connections FIRST (Milvus load/query need an active alias), then
    # do the one-time load. These warm connections are reused for the measured
    # runs — the connect cost is never inside the timed path.
    milvus_alias = connect_milvus(conn)
    neo4j_driver = connect_neo4j(conn)
    pg = connect_postgres(conn)

    if do_load:
        load_all(conn, corpus)
    # ensure the Milvus collection is loaded into memory before timing
    try:
        from pymilvus import Collection

        Collection(conn.milvus_collection, using=milvus_alias).load()
    except Exception as exc:  # noqa: BLE001
        print(f"[sm2-baseline] WARNING: collection.load() pre-warm failed: {exc}")

    drivers = {"neo4j": neo4j_driver, "milvus": milvus_alias, "postgres": pg}

    results: list[SM2QueryResult] = []
    try:
        for q in manifest["queries"]:
            r = run_query(q, k, drivers, conn, runs)
            results.append(r)
            print(
                f"[sm2-baseline] qid={r.qid} src={r.src} "
                f"median={r.latency_total_ms:.2f}ms "
                f"(g={r.latency_graph_ms:.2f} v={r.latency_vector_ms:.2f} "
                f"r={r.latency_relational_ms:.2f} m={r.latency_merge_ms:.3f}) "
                f"reached={r.graph_reached_dst} vec={r.vector_candidates} "
                f"filt={r.relational_filtered} merged={r.merged_candidates} "
                f"final={r.final_results} ids={r.result_ids}"
            )
    finally:
        try:
            neo4j_driver.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pg.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            from pymilvus import connections

            connections.disconnect(milvus_alias)
        except Exception:  # noqa: BLE001
            pass

    return {
        "baseline": "akasicdb-scenario-2-live-multisystem",
        "methodology": (
            "end-to-end client wall-clock per query over WARM connections "
            "(one warm-up discarded); MEDIAN of N measured runs; one-time "
            "load+index build EXCLUDED; realized canonical semantics (pinned src, "
            "1-hop graph reach, ANN over-fetch k*32 ranked by dst distance, "
            "timestamp-window filter, app-side merge top-k)"
        ),
        "k": k,
        "seed": seed,
        "runs": runs,
        "num_queries": len(results),
        "ann_overfetch": BASELINE_ANN_FANOUT,
        "queries": [asdict(r) for r in results],
    }


def main(argv=None) -> int:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument(
        "--no-load",
        action="store_true",
        help="skip the one-time load (systems already populated)",
    )
    args = p.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    conn = Conn()
    payload = run_baseline(
        manifest, args.seed, args.k, args.runs, conn, do_load=not args.no_load
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"[sm2-baseline] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

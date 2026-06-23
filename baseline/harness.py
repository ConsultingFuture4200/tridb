"""DEV-1171 multi-system baseline harness (AkasicDB Scenario 2).

Executes the same Omni-RAG retrieval the canonical TriDB query expresses, but
across THREE separate systems merged in Python at the app layer:

    canonical query
    ----------------
    SELECT chunk
    FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
      COLUMNS ( src.embedding AS src_embedding,
                dst.chunk     AS chunk,
                dst.timestamp AS timestamp ) )
    WHERE timestamp IN :selected_time_range
    ORDER BY src_embedding <-> :question_embedding
    LIMIT 5;

baseline decomposition (merged app-side)
----------------------------------------
    graph (Neo4j)     : 1-hop :related_to expansion from seed entities
                        -> candidate (src, dst) pairs
    vector (Milvus)   : ANN top-k on src embedding vs the question embedding
                        -> ranks src entities by similarity
    relational (PG)   : timestamp range filter on dst entities
                        -> keeps only dst rows whose timestamp is in range
    merge (Python)    : join the three on the (src -> dst) pairs and take top-k
                        by src similarity -> final chunks

This is the structure SM-1 (>=5x intermediate-result reduction) is measured
against: the baseline must materialize large intermediate sets and merge them
app-side, whereas TriDB fuses the operators in-process with early termination.
The harness therefore records, per query, the size of EVERY intermediate set
plus end-to-end latency.

This is a skeleton. The merge logic and instrumentation are real; anything that
needs the live systems running is marked TODO. Connection params come from env
vars with localhost defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Clients are imported lazily inside the connection helpers so that --help and
# unit-level inspection work without the systems (or driver wheels) present.


# --------------------------------------------------------------------------- #
# Connection params (env vars, localhost defaults)
# --------------------------------------------------------------------------- #


@dataclass
class Conn:
    """Connection params for the three baseline systems."""

    neo4j_uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.environ.get("NEO4J_USER", "neo4j")
    neo4j_password: str = os.environ.get("NEO4J_PASSWORD", "testpassword")

    milvus_host: str = os.environ.get("MILVUS_HOST", "localhost")
    milvus_port: str = os.environ.get("MILVUS_PORT", "19530")
    milvus_collection: str = os.environ.get("MILVUS_COLLECTION", "entity_embeddings")

    pg_host: str = os.environ.get("PGHOST", "localhost")
    pg_port: str = os.environ.get("PGPORT", "5432")
    pg_user: str = os.environ.get("PGUSER", "postgres")
    pg_password: str = os.environ.get("PGPASSWORD", "postgres")
    pg_db: str = os.environ.get("PGDATABASE", "tridb_baseline")


# --------------------------------------------------------------------------- #
# Per-query metrics
# --------------------------------------------------------------------------- #


@dataclass
class QueryMetrics:
    """Latency + intermediate-result sizes for a single query.

    The intermediate sizes are the SM-1 measurement surface: the baseline pays
    for every row it materializes and ships across system boundaries.
    """

    qid: int
    k: int

    # latency (milliseconds)
    latency_total_ms: float = 0.0
    latency_graph_ms: float = 0.0
    latency_vector_ms: float = 0.0
    latency_relational_ms: float = 0.0
    latency_merge_ms: float = 0.0

    # intermediate-result set sizes (row counts)
    graph_pairs: int = 0            # candidate (src, dst) pairs from Neo4j
    graph_distinct_src: int = 0     # distinct src entities expanded
    graph_distinct_dst: int = 0     # distinct dst entities reached
    vector_candidates: int = 0      # rows returned by Milvus ANN top-k
    relational_filtered: int = 0    # dst rows surviving the timestamp filter
    merged_candidates: int = 0      # rows after the app-side join
    final_results: int = 0          # rows in the final top-k answer

    result_chunks: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #


def connect_neo4j(conn: Conn):
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        conn.neo4j_uri, auth=(conn.neo4j_user, conn.neo4j_password)
    )


def connect_milvus(conn: Conn):
    from pymilvus import connections

    connections.connect(alias="default", host=conn.milvus_host, port=conn.milvus_port)
    return "default"


def connect_postgres(conn: Conn):
    import psycopg

    return psycopg.connect(
        host=conn.pg_host,
        port=conn.pg_port,
        user=conn.pg_user,
        password=conn.pg_password,
        dbname=conn.pg_db,
    )


# --------------------------------------------------------------------------- #
# Corpus loading
# --------------------------------------------------------------------------- #


def _read_entities(seed_dir: Path) -> list[dict]:
    """Parse entities.csv -> [{id, timestamp, chunk, embedding}].

    embedding is stored as a Postgres array literal '{a,b,c}' by
    tools/seed_corpus.py; we parse it back into a list[float].
    """
    import csv

    rows: list[dict] = []
    with open(seed_dir / "entities.csv", newline="") as f:
        for r in csv.DictReader(f):
            emb = [float(x) for x in r["embedding"].strip("{}").split(",")]
            rows.append(
                {
                    "id": int(r["id"]),
                    "timestamp": int(r["timestamp"]),
                    "chunk": r["chunk"],
                    "embedding": emb,
                }
            )
    return rows


def _read_edges(seed_dir: Path) -> list[tuple[int, int]]:
    import csv

    out: list[tuple[int, int]] = []
    with open(seed_dir / "edges.csv", newline="") as f:
        for r in csv.DictReader(f):
            out.append((int(r["src"]), int(r["dst"])))
    return out


def _read_queries(seed_dir: Path) -> list[dict]:
    out: list[dict] = []
    with open(seed_dir / "queries.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load(seed_dir: Path, conn: Conn) -> None:
    """Load the seed corpus into all three systems.

    Reads entities.csv / edges.csv from seed_dir and populates Neo4j (nodes +
    :related_to edges), Milvus (embedding collection), and Postgres (entity
    table). Loading is one-time setup; it is not part of the measured run().
    """
    entities = _read_entities(seed_dir)
    edges = _read_edges(seed_dir)
    print(f"[load] parsed {len(entities)} entities, {len(edges)} edges")

    # --- Neo4j: nodes + :related_to edges -----------------------------------
    driver = connect_neo4j(conn)
    with driver.session() as session:
        # TODO(live): batch with UNWIND for real corpus sizes; add an index on
        # :entity(id) before bulk insert.
        session.run("MATCH (n) DETACH DELETE n")
        for e in entities:
            session.run(
                "CREATE (:entity {id: $id, timestamp: $ts})",
                id=e["id"],
                ts=e["timestamp"],
            )
        for src, dst in edges:
            session.run(
                "MATCH (a:entity {id: $s}), (b:entity {id: $d}) "
                "CREATE (a)-[:related_to]->(b)",
                s=src,
                d=dst,
            )
    driver.close()
    print("[load] neo4j: nodes + :related_to edges written")

    # --- Milvus: embedding collection ---------------------------------------
    connect_milvus(conn)
    # TODO(live): create_collection(id INT64 pk, embedding FLOAT_VECTOR[dim]),
    # build an HNSW/IVF_FLAT index, insert [ids], [embeddings], then load().
    # Dim comes from len(entities[0]["embedding"]).
    print("[load] milvus: TODO create collection + index + insert embeddings")

    # --- Postgres: entity table ---------------------------------------------
    pg = connect_postgres(conn)
    with pg.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS entity ("
            "id INT PRIMARY KEY, timestamp INT, chunk TEXT)"
        )
        cur.execute("TRUNCATE entity")
        with cur.copy("COPY entity (id, timestamp, chunk) FROM STDIN") as cp:
            for e in entities:
                cp.write_row((e["id"], e["timestamp"], e["chunk"]))
        cur.execute("CREATE INDEX IF NOT EXISTS entity_ts_idx ON entity (timestamp)")
    pg.commit()
    pg.close()
    print("[load] postgres: entity table loaded")


# --------------------------------------------------------------------------- #
# Per-system retrieval steps (return rows + record intermediate sizes)
# --------------------------------------------------------------------------- #


def graph_expand(driver, seeds: list[int], m: QueryMetrics) -> list[tuple[int, int]]:
    """1-hop :related_to expansion from seed entities (Neo4j).

    Returns candidate (src, dst) pairs and records their size as the graph
    intermediate result.
    """
    t0 = time.perf_counter()
    pairs: list[tuple[int, int]] = []
    with driver.session() as session:
        result = session.run(
            "MATCH (src:entity)-[:related_to]->(dst:entity) "
            "WHERE src.id IN $seeds "
            "RETURN src.id AS src, dst.id AS dst",
            seeds=seeds,
        )
        for rec in result:
            pairs.append((rec["src"], rec["dst"]))

    m.latency_graph_ms = (time.perf_counter() - t0) * 1000.0
    m.graph_pairs = len(pairs)
    m.graph_distinct_src = len({s for s, _ in pairs})
    m.graph_distinct_dst = len({d for _, d in pairs})
    return pairs


def vector_topk(alias, question_embedding: list[float], k: int, m: QueryMetrics,
                conn: Conn) -> list[tuple[int, float]]:
    """ANN top-k on src embedding (Milvus).

    Returns [(entity_id, distance)] ranked by similarity to the question
    embedding and records the candidate-set size.
    """
    t0 = time.perf_counter()
    from pymilvus import Collection

    collection = Collection(conn.milvus_collection, using=alias)
    # NOTE: the baseline over-fetches (k * fanout) because it cannot push the
    # graph/time predicates down into the ANN scan -- this over-fetch is exactly
    # the intermediate-result blowup SM-1 measures. Tune the multiplier once the
    # live recall numbers are known.
    search_limit = max(k * 32, k)
    res = collection.search(
        data=[question_embedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"ef": 256}},
        limit=search_limit,
        output_fields=["id"],
    )
    hits = [(int(h.id), float(h.distance)) for h in res[0]]

    m.latency_vector_ms = (time.perf_counter() - t0) * 1000.0
    m.vector_candidates = len(hits)
    return hits


def relational_filter(pg, dst_ids: list[int], time_range: list[int],
                      m: QueryMetrics) -> dict[int, str]:
    """Timestamp range filter on dst entities (Postgres).

    Returns {dst_id: chunk} for dst rows whose timestamp is in the selected
    range and records the surviving-row count.
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

    m.latency_relational_ms = (time.perf_counter() - t0) * 1000.0
    m.relational_filtered = len(kept)
    return kept


# --------------------------------------------------------------------------- #
# App-side merge (the baseline's defining cost)
# --------------------------------------------------------------------------- #


def merge(pairs: list[tuple[int, int]], vector_hits: list[tuple[int, float]],
          kept_dst: dict[int, str], k: int, m: QueryMetrics) -> list[str]:
    """Merge the three intermediate sets into the final top-k.

    Mirrors the canonical query semantics:
      - pairs            : (src)-[:related_to]->(dst) candidates from the graph
      - vector_hits      : src ranked by embedding distance to the question
      - kept_dst         : dst rows surviving the timestamp filter (id -> chunk)

    A pair qualifies iff its src has an ANN distance AND its dst survived the
    time filter. Qualifying pairs are ordered by src distance (the canonical
    ORDER BY src_embedding <-> :question_embedding) and the top-k dst chunks are
    returned, de-duplicated while preserving order.
    """
    t0 = time.perf_counter()
    src_dist = dict(vector_hits)

    candidates: list[tuple[float, int]] = []  # (distance, dst_id)
    for src, dst in pairs:
        if src in src_dist and dst in kept_dst:
            candidates.append((src_dist[src], dst))

    m.merged_candidates = len(candidates)
    candidates.sort(key=lambda x: x[0])  # ascending L2 distance = most similar

    chunks: list[str] = []
    seen: set[int] = set()
    for _, dst in candidates:
        if dst not in seen:
            seen.add(dst)
            chunks.append(kept_dst[dst])
        if len(chunks) >= k:
            break

    m.latency_merge_ms = (time.perf_counter() - t0) * 1000.0
    m.final_results = len(chunks)
    m.result_chunks = chunks
    return chunks


# --------------------------------------------------------------------------- #
# Per-query orchestration
# --------------------------------------------------------------------------- #


def run_query(query: dict, k: int, drivers: dict, conn: Conn) -> QueryMetrics:
    """Run one Omni-RAG query across the three systems and merge app-side."""
    qid = int(query["qid"])
    question_embedding = query["embedding"]
    time_range = query["selected_time_range"]
    m = QueryMetrics(qid=qid, k=k)

    t_start = time.perf_counter()

    # 1) Vector ANN top-k -> ranked src entities. These seed the graph hop.
    vector_hits = vector_topk(
        drivers["milvus"], question_embedding, k, m, conn
    )
    seeds = [eid for eid, _ in vector_hits]

    # 2) Graph 1-hop expansion from the ANN-ranked seeds.
    pairs = graph_expand(drivers["neo4j"], seeds, m)

    # 3) Relational timestamp filter on the reached dst entities.
    dst_ids = sorted({d for _, d in pairs})
    kept_dst = relational_filter(drivers["postgres"], dst_ids, time_range, m)

    # 4) App-side merge -> final top-k chunks.
    merge(pairs, vector_hits, kept_dst, k, m)

    m.latency_total_ms = (time.perf_counter() - t_start) * 1000.0
    return m


def run(seed_dir: Path, k: int, out_path: Path, conn: Conn) -> None:
    """Run every query in the corpus and write per-query metrics to JSON."""
    queries = _read_queries(seed_dir)
    print(f"[run] {len(queries)} queries, k={k}")

    drivers = {
        "neo4j": connect_neo4j(conn),
        "milvus": connect_milvus(conn),
        "postgres": connect_postgres(conn),
    }

    metrics: list[dict] = []
    try:
        for q in queries:
            m = run_query(q, k, drivers, conn)
            metrics.append(asdict(m))
            print(
                f"[run] qid={m.qid} total={m.latency_total_ms:.1f}ms "
                f"graph_pairs={m.graph_pairs} vec={m.vector_candidates} "
                f"rel={m.relational_filtered} merged={m.merged_candidates} "
                f"final={m.final_results}"
            )
    finally:
        drivers["neo4j"].close()
        drivers["postgres"].close()

    payload = {
        "baseline": "akasicdb-scenario-2-out-of-db",
        "k": k,
        "seed_dir": str(seed_dir),
        "num_queries": len(metrics),
        "queries": metrics,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[run] wrote metrics -> {out_path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_load = sub.add_parser("load", help="load a seed corpus into all 3 systems")
    p_load.add_argument("--seed-dir", required=True, type=Path)

    p_run = sub.add_parser("run", help="run queries and record metrics")
    p_run.add_argument("--seed-dir", required=True, type=Path)
    p_run.add_argument("--k", type=int, default=5)
    p_run.add_argument(
        "--out", type=Path, default=Path("baseline_metrics.json")
    )

    args = parser.parse_args()
    conn = Conn()

    if args.cmd == "load":
        load(args.seed_dir, conn)
    elif args.cmd == "run":
        run(args.seed_dir, args.k, args.out, conn)


if __name__ == "__main__":
    main()

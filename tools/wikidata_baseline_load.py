"""Load an ingested+embedded Wikidata slice into the ISOLATED multi-store baseline.

The baseline half of the two ADR-0018 loaders (the engine half is
tools/wikidata_engine_load). Loads the SAME slice, in the SAME dense-id space, into the
three stores bench/wikidata_h2h.WCfg expects, so the harness's baseline leg
(Milvus ANN + Neo4j typed traversal + Postgres type filter, fused app-side) traverses
exactly the graph the engine loaded:

    Milvus   localhost:19531  collection "wikidata_entities"  — id INT64 pk (dense id)
             + FLOAT_VECTOR dim 384, HNSW COSINE (vectors L2-normalized at write, the
             same rows the engine stores — ADR-0017 B4-interim). Not in the KBQA loop
             (run_baseline's fairness choice) but loaded per WCfg for Harness A.
    Neo4j    bolt://localhost:7688 (neo4j / "wikipassword", the WH_NEO4J_* convention of
             bench/wiki_h2h.py) — one (:Entity {id, qid}) node per entity, one
             relationship per kept typed edge. ``id`` is a STRING property — the
             run_baseline loader contract (`a.id IN $ids` with int ids silently
             matches nothing).
    Postgres localhost:5434 db "tridb_wikidata" table "wd_entity" (the pgvector-side
             isolated pg) — id bigint PK (dense), qid bigint, p31 bigint[] (DENSE type
             ids; run_baseline casts its probe ``ARRAY[t]::bigint[]``) + GIN index,
             PLUS embedding vector(dim): run_baseline's default rank leg is the exact
             pgvector ``<=>`` rerank over the small surviving candidate set.

NEO4J EDGE CONVENTION (the documented choice): one relationship of type ``P<m>`` per
kept edge, additionally carrying the property id as ``{p: m}``. Per-TYPE relationship
types (not a property-only encoding) are what bench/wikidata_h2h.run_baseline traverses
(``-[:P<m>*1..h]->``, mirroring bench/wiki_h2h.py's neo4j_hop with the type inlined) —
the fair, index-free-adjacency Cypher a competent operator would write. Property-filtered
variable-length patterns (``-[r*1..h {p: 279}]->``) cannot use the relationship-type
store and would strawman the baseline. The ``p`` property preserves a uniform way to
audit/count edges per property.

EDGE-COUNT PARITY DEFINITION (identical to the engine loader, the harness gate needs
engine_edges == neo4j_edges): an edge counts iff BOTH endpoints are in-slice (present in
the dense map); duplicates preserved (relationships are CREATEd, not MERGEd — the ingest
already de-duplicates per (src, p, dst)). Both loaders print the same
"edges kept (both endpoints in-slice)" line and stamp the count in their manifests.

Store clients (pymilvus / neo4j / psycopg) are imported LAZILY inside the per-store
loaders, so the pure logic (dense remap, row/statement generation) is importable and
host-tested without live stores (tests/test_wikidata_baseline_load.py).

Idempotent re-run: --force drops + recreates the collection / :Entity subgraph / table.
Without --force, an already-populated store aborts loudly.

CLI:
    python -m tools.wikidata_baseline_load --slice data/wikidata_slice [--force] \\
        [--out baseline_load_manifest.json]
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

# Pure slice logic is shared with the engine loader so the two dense-id remaps can
# never drift (single implementation, pinned to bench/wikidata_h2h by the host tests).
from tools.wikidata_engine_load import (
    build_dense_map,
    int_array_literal,
    iter_kept_edges,
    load_p31_dense,
    load_slice_manifest,
    norm_vec,
)

# Defaults mirror bench/wikidata_h2h.WCfg (ports/collection/db/table) and the
# WH_NEO4J_* auth convention of bench/wiki_h2h.py.
DEFAULT_MILVUS_HOST = "localhost"
DEFAULT_MILVUS_PORT = "19531"
DEFAULT_MILVUS_COLLECTION = "wikidata_entities"
DEFAULT_NEO4J_URI = "bolt://localhost:7688"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "wikipassword"
DEFAULT_PG_HOST = "localhost"
DEFAULT_PG_PORT = "5434"
DEFAULT_PG_DB = "tridb_wikidata"
DEFAULT_PG_TABLE = "wd_entity"
DEFAULT_PG_USER = "postgres"
DEFAULT_PG_PASSWORD = "postgres"
DEFAULT_DIM = 384
BATCH = 10_000  # UNWIND/COPY batch (the wiki_neo4j_load discipline)
MILVUS_BATCH = 1_000  # per-RPC size cap (the baseline/sm2.py discipline)

NODE_LABEL = "Entity"
CONSTRAINT_CYPHER = (
    f"CREATE CONSTRAINT wd_entity_id IF NOT EXISTS "
    f"FOR (e:{NODE_LABEL}) REQUIRE e.id IS UNIQUE"
)
NODE_CREATE_CYPHER = (
    f"UNWIND $rows AS r CREATE (:{NODE_LABEL} {{id: r.id, qid: r.qid}})"
)


def edge_cypher(pid: int) -> str:
    """Per-property relationship CREATE: type P<m>, property p = m (see docstring)."""
    pid = int(pid)
    return (
        f"UNWIND $rows AS r "
        f"MATCH (a:{NODE_LABEL} {{id: r.src}}), (b:{NODE_LABEL} {{id: r.dst}}) "
        f"CREATE (a)-[:P{pid} {{p: {pid}}}]->(b)"
    )


# ======================================================================================
# Pure row/statement generation (host-testable)
# ======================================================================================
def pg_rows(
    dense_to_qid: list[int], p31: dict[int, list[int]]
) -> Iterator[tuple[int, int, list[int]]]:
    """(dense id, qid, dense P31 type ids) per entity, in dense order."""
    for dense_id, qid in enumerate(dense_to_qid):
        yield dense_id, qid, p31.get(dense_id, [])


def node_rows(dense_to_qid: list[int]) -> Iterator[dict]:
    """(:Entity) rows. `id` is a STRING — the run_baseline loader contract (its
    traversal binds `a.id IN $ids` with stringified ids and int()s the results)."""
    for dense_id, qid in enumerate(dense_to_qid):
        yield {"id": str(dense_id), "qid": qid}


def edges_by_pid(
    edges: Iterator[tuple[int, int, int]],
) -> dict[int, list[dict]]:
    """Group kept edges by property id for the per-type relationship batches.

    src/dst are stringified to MATCH the string `id` node property (see node_rows).
    """
    out: dict[int, list[dict]] = {}
    for src, pid, dst in edges:
        out.setdefault(pid, []).append({"src": str(src), "dst": str(dst)})
    return out


def pgvector_literal(v: list[float]) -> str:
    # pgvector input literal (the shape run_baseline's rerank binds as %s::vector)
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def pg_create_statements(table: str, dim: int) -> list[str]:
    return [
        # the 5434 store is the pgvector-side pg (bench/wiki_consistency.py layout)
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"CREATE TABLE {table} ("
        "id bigint PRIMARY KEY, qid bigint NOT NULL, "
        "p31 bigint[] NOT NULL DEFAULT '{}', "  # run_baseline casts ::bigint[]
        f"embedding vector({dim}))",  # exact '<=>' rerank leg (run_baseline default)
        # GIN backs the P31-contains type filter (the baseline's relational leg);
        # without it the pg leg would strawman the baseline at scale.
        f"CREATE INDEX {table}_p31_gin ON {table} USING gin (p31)",
    ]


def _batched(rows: Iterator | list, size: int) -> Iterator[list]:
    batch: list = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ======================================================================================
# Store loaders (lazy client imports; need the live baseline stack)
# ======================================================================================
def load_postgres(args, dense_to_qid, p31, emb) -> dict:
    import psycopg

    # ensure the database exists (the isolated baseline pg ships only 'postgres')
    admin = psycopg.connect(
        host=args.pg_host,
        port=args.pg_port,
        dbname="postgres",
        user=args.pg_user,
        password=args.pg_password,
        autocommit=True,
    )
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.pg_db,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{args.pg_db}"')
    finally:
        admin.close()

    pg = psycopg.connect(
        host=args.pg_host,
        port=args.pg_port,
        dbname=args.pg_db,
        user=args.pg_user,
        password=args.pg_password,
    )
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (args.pg_table,))
            if cur.fetchone()[0] is not None:
                if not args.force:
                    raise SystemExit(
                        f"pg table {args.pg_table} already exists — re-run with --force"
                    )
                cur.execute(f"DROP TABLE {args.pg_table}")
            for stmt in pg_create_statements(args.pg_table, args.dim):
                cur.execute(stmt)
            with cur.copy(
                f"COPY {args.pg_table} (id, qid, p31, embedding) FROM STDIN"
            ) as copy:
                for dense_id, qid, types in pg_rows(dense_to_qid, p31):
                    copy.write_row(
                        (
                            dense_id,
                            qid,
                            int_array_literal(types),
                            pgvector_literal(norm_vec(emb[dense_id])),
                        )
                    )
            cur.execute(f"SELECT count(*) FROM {args.pg_table}")
            n = cur.fetchone()[0]
        pg.commit()
    finally:
        pg.close()
    print(
        f"[load] postgres: {n} {args.pg_table} rows + p31 GIN index + "
        f"vector({args.dim}) embeddings (COPY)"
    )
    return {"table": args.pg_table, "rows": n}


def load_neo4j(args, dense_to_qid, edge_groups) -> dict:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        driver.verify_connectivity()
        with driver.session() as s:
            existing = s.run(f"MATCH (n:{NODE_LABEL}) RETURN count(n) AS c").single()[
                "c"
            ]
            if existing:
                if not args.force:
                    raise SystemExit(
                        f"neo4j already holds {existing} :{NODE_LABEL} nodes — "
                        "re-run with --force"
                    )
                # slice-sized wipe (the baseline/sm2.py discipline); a 10M+ wipe
                # would want batched deletes, but that scale is GX10-gated anyway.
                s.run(f"MATCH (n:{NODE_LABEL}) DETACH DELETE n").consume()
            s.run(CONSTRAINT_CYPHER).consume()
            for batch in _batched(node_rows(dense_to_qid), BATCH):
                s.run(NODE_CREATE_CYPHER, rows=batch).consume()
            for pid in sorted(edge_groups):
                cy = edge_cypher(pid)
                for batch in _batched(edge_groups[pid], BATCH):
                    s.run(cy, rows=batch).consume()
            node_count = s.run(f"MATCH (n:{NODE_LABEL}) RETURN count(n) AS c").single()[
                "c"
            ]
            rel_count = s.run(
                f"MATCH (:{NODE_LABEL})-[r]->(:{NODE_LABEL}) RETURN count(r) AS c"
            ).single()["c"]
    finally:
        driver.close()
    print(
        f"[load] neo4j: {node_count} :{NODE_LABEL} nodes, {rel_count} typed "
        f"relationships across {len(edge_groups)} P-types"
    )
    return {
        "label": NODE_LABEL,
        "nodes": node_count,
        "relationships": rel_count,
        "distinct_properties": len(edge_groups),
    }


def load_milvus(args, emb, n: int) -> dict:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )

    connections.connect(alias="wdl", host=args.milvus_host, port=args.milvus_port)
    name = args.milvus_collection
    if utility.has_collection(name, using="wdl"):
        if not args.force:
            raise SystemExit(
                f"milvus collection {name} already exists — re-run with --force"
            )
        utility.drop_collection(name, using="wdl")
    schema = CollectionSchema(
        [
            FieldSchema("id", DataType.INT64, is_primary=True),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=args.dim),
        ]
    )
    col = Collection(name, schema, using="wdl")
    for lo in range(0, n, MILVUS_BATCH):
        hi = min(lo + MILVUS_BATCH, n)
        ids = list(range(lo, hi))
        vecs = [norm_vec(emb[i]) for i in ids]
        col.insert([ids, vecs])
    col.flush()
    # HNSW is what a competent operator runs (baseline/sm2.py plan-030 rationale);
    # COSINE per WCfg — order-identical to the engine's l2-on-normalized rows.
    col.create_index(
        "embedding",
        {
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        },
    )
    col.load()
    print(f"[load] milvus: {n} vectors (dim={args.dim}) + HNSW COSINE, loaded")
    return {"collection": name, "vectors": n, "metric": "COSINE", "index": "HNSW"}


# ======================================================================================
# CLI
# ======================================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Load a Wikidata slice into the Milvus+Neo4j+pg baseline."
    )
    ap.add_argument(
        "--slice", type=Path, required=True, help="wikidata_ingest manifest dir"
    )
    ap.add_argument(
        "--emb",
        type=Path,
        default=None,
        help="id-aligned embeddings .npy (default <slice>/emb/dense_id_aligned.npy)",
    )
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument("--milvus-host", default=DEFAULT_MILVUS_HOST)
    ap.add_argument("--milvus-port", default=DEFAULT_MILVUS_PORT)
    ap.add_argument("--milvus-collection", default=DEFAULT_MILVUS_COLLECTION)
    ap.add_argument("--neo4j-uri", default=DEFAULT_NEO4J_URI)
    ap.add_argument("--neo4j-user", default=DEFAULT_NEO4J_USER)
    ap.add_argument("--neo4j-password", default=DEFAULT_NEO4J_PASSWORD)
    ap.add_argument("--pg-host", default=DEFAULT_PG_HOST)
    ap.add_argument("--pg-port", default=DEFAULT_PG_PORT)
    ap.add_argument("--pg-db", default=DEFAULT_PG_DB)
    ap.add_argument("--pg-table", default=DEFAULT_PG_TABLE)
    ap.add_argument("--pg-user", default=DEFAULT_PG_USER)
    ap.add_argument("--pg-password", default=DEFAULT_PG_PASSWORD)
    ap.add_argument(
        "--force", action="store_true", help="drop + recreate each store's slice"
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="load-manifest JSON (default <slice>/baseline_load_manifest.json)",
    )
    args = ap.parse_args(argv)

    import numpy as np  # local: keep module import light for the pure-logic tests

    manifest = load_slice_manifest(args.slice)
    emb_path = args.emb or (args.slice / "emb" / "dense_id_aligned.npy")
    emb = np.load(emb_path, mmap_mode="r")

    qmap, dense_to_qid = build_dense_map(args.slice, manifest)
    n = len(dense_to_qid)
    if n == 0:
        raise SystemExit("empty slice: no entities in the manifest shards")
    if emb.shape[0] < n:
        raise SystemExit(
            f"embeddings have {emb.shape[0]} rows < N={n}; cannot load the slice"
        )
    if emb.shape[1] != args.dim:
        raise SystemExit(f"embeddings dim {emb.shape[1]} != --dim {args.dim}")
    p31 = load_p31_dense(args.slice, manifest, qmap)
    stats = {"kept": 0, "dropped": 0}
    edge_groups = edges_by_pid(iter_kept_edges(args.slice, manifest, qmap, stats))

    durations: dict[str, float] = {}
    t = time.time()
    pg_res = load_postgres(args, dense_to_qid, p31, emb)
    durations["postgres_secs"] = round(time.time() - t, 2)
    t = time.time()
    neo_res = load_neo4j(args, dense_to_qid, edge_groups)
    durations["neo4j_secs"] = round(time.time() - t, 2)
    t = time.time()
    mil_res = load_milvus(args, emb, n)
    durations["milvus_secs"] = round(time.time() - t, 2)

    out = args.out or (args.slice / "baseline_load_manifest.json")
    load_manifest = {
        "tool": "tools/wikidata_baseline_load.py",
        "created": datetime.now(timezone.utc).isoformat(),
        "slice_dir": str(args.slice),
        "emb_path": str(emb_path),
        "dim": args.dim,
        "force": args.force,
        "counts": {
            "entities": n,
            "edges_kept": stats["kept"],
            "edges_dropped_dangling": stats["dropped"],
            "distinct_properties": len(edge_groups),
        },
        "stores": {"postgres": pg_res, "neo4j": neo_res, "milvus": mil_res},
        "durations": durations,
        # the WH_NEO4J_EDGES analogue for bench/wikidata_h2h's publication gate
        "gate_env": {"WD_NEO4J_EDGES": neo_res["relationships"]},
    }
    out.write_text(json.dumps(load_manifest, indent=2))
    print(
        f"[wikidata_baseline_load] entities={n} "
        f"edges kept (both endpoints in-slice)={stats['kept']} "
        f"(dropped dangling={stats['dropped']}, "
        f"properties={len(edge_groups)}) -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

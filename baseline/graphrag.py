"""Live multi-store GraphRAG baseline — latency leg of the head-to-head (Plan 015).

The out-of-DB-integration baseline (AkasicDB Scenario 2) for graph-constrained
retrieval: Milvus ANN seed -> Neo4j mention-edge hop expansion -> app-side
re-rank -> top-k, timed end-to-end over WARM clients (the same like-for-like
methodology as baseline/sm2.py). Its accuracy is identical to the host
graph-constrained retriever BY CONSTRUCTION (same algorithm) — this module exists
for the LATENCY comparison against the live tjs() leg, which is GX10/engine-gated
(scripts/bench_graphrag.sh). Reuses baseline/harness.py connection helpers.

Run standalone (stack must be up; baseline Postgres on PGPORT=5433 on this box):
    python -m baseline.graphrag --manifest data/hotpot/manifest.json --k 10
This loads the dev-slice corpus into the live stack and reports per-query baseline
latency; it does NOT measure TriDB (that is the engine-gated side).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from baseline.harness import Conn, connect_milvus, connect_neo4j

MILVUS_COLLECTION = "hotpot_graphrag"


def _load_milvus(conn: Conn, emb: np.ndarray) -> str:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        utility,
    )

    alias = connect_milvus(conn)
    if utility.has_collection(MILVUS_COLLECTION):
        utility.drop_collection(MILVUS_COLLECTION)
    dim = emb.shape[1]
    schema = CollectionSchema(
        [
            FieldSchema("id", DataType.INT64, is_primary=True),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=dim),
        ]
    )
    col = Collection(MILVUS_COLLECTION, schema)
    ids = list(range(emb.shape[0]))
    batch = 1000
    for i in range(0, len(ids), batch):
        col.insert([ids[i : i + batch], emb[i : i + batch].tolist()])
    col.flush()
    col.create_index(
        "embedding",
        {"index_type": "IVF_FLAT", "metric_type": "IP", "params": {"nlist": 128}},
    )
    col.load()
    return alias


def _load_neo4j(conn: Conn, edges: list[tuple[int, int]]) -> None:
    driver = connect_neo4j(conn)
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        s.run("CREATE INDEX para_id IF NOT EXISTS FOR (p:para) ON (p.id)")
        # batch with UNWIND (real corpus sizes)
        s.run(
            "UNWIND $rows AS r MERGE (a:para {id:r.s}) MERGE (b:para {id:r.d}) MERGE (a)-[:mentions]->(b)",
            rows=[{"s": int(x), "d": int(y)} for x, y in edges],
        )
    driver.close()


def run_baseline(manifest: dict, conn: Conn, *, k: int, seeds: int, hops: int) -> dict:
    corpus_emb = np.load(manifest["corpus_emb_path"]).astype(np.float32)
    query_emb = np.load(manifest["query_emb_path"]).astype(np.float32)
    edges = [(int(s), int(d)) for s, d in manifest["_edges"]]

    print(
        f"[graphrag-baseline] loading {corpus_emb.shape[0]} vecs + {len(edges)} edges into live stack"
    )
    from pymilvus import Collection

    alias = _load_milvus(conn, corpus_emb)
    _load_neo4j(conn, edges)
    col = Collection(MILVUS_COLLECTION, using=alias)
    driver = connect_neo4j(conn)

    per_query = []
    for q in manifest["questions"]:
        qv = query_emb[q["qid"]].tolist()
        t0 = time.perf_counter()
        # 1) Milvus ANN seed
        res = col.search(
            [qv],
            "embedding",
            {"metric_type": "IP", "params": {"nprobe": 16}},
            limit=seeds,
            output_fields=["id"],
        )
        seed_ids = [h.id for h in res[0]]
        t1 = time.perf_counter()
        # 2) Neo4j hop expansion over mention edges
        with driver.session() as s:
            rows = s.run(
                f"MATCH (a:para)-[:mentions*1..{hops}]->(b:para) WHERE a.id IN $ids RETURN DISTINCT b.id AS id",
                ids=seed_ids,
            )
            reach = {r["id"] for r in rows} | set(seed_ids)
        t2 = time.perf_counter()
        # 3) app-side re-rank by cosine
        cand = np.fromiter(reach, dtype=np.int64, count=len(reach))
        scores = corpus_emb[cand] @ query_emb[q["qid"]]
        top = [int(x) for x in cand[np.argsort(-scores)][:k]]
        t3 = time.perf_counter()
        per_query.append(
            {
                "qid": q["qid"],
                "total_ms": (t3 - t0) * 1e3,
                "milvus_ms": (t1 - t0) * 1e3,
                "neo4j_ms": (t2 - t1) * 1e3,
                "rerank_ms": (t3 - t2) * 1e3,
                "topk": top,
            }
        )
    driver.close()
    med = float(np.median([p["total_ms"] for p in per_query]))
    print(
        f"[graphrag-baseline] median end-to-end {med:.2f} ms/query over {len(per_query)} queries"
    )
    return {
        "k": k,
        "seeds": seeds,
        "hops": hops,
        "median_ms": med,
        "per_query": per_query,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Live multi-store GraphRAG latency baseline."
    )
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument(
        "--out", type=Path, default=Path("bench/results/graphrag_baseline.json")
    )
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    payload = run_baseline(manifest, Conn(), k=args.k, seeds=args.seeds, hops=args.hops)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"[graphrag-baseline] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

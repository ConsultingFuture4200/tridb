"""Load a wiki_extract manifest directory into Neo4j (DEV-1354 visual track).

Builds the graph half of the offline-wiki corpus inside the baseline Neo4j
(baseline/docker-compose.yml, image neo4j:5.20 COMMUNITY):

    (:Article {id, title, ts})
    (:Article)-[:LINKS_TO]->(:Article)   -- from edges-*.tsv (redirect-resolved ns0)

This is the graph-viz counterpart to the relational/vector legs; it is NOT the
TriDB engine and makes no claim about the in-process access method. Neo4j here is
a Community-compatible way to *look at* the topology (see tools/wiki_subgraph.py).

Efficiency contract:
  - a uniqueness constraint on :Article(id) is created FIRST (it also backs the
    id lookup index the edge MATCH relies on), then
  - nodes and edges are streamed shard-by-shard and pushed in UNWIND batches
    (default 10k). The 3.9M-edge TSV is never materialised in RAM — each batch is
    a bounded list drained from a generator.

Idempotent-ish: MERGE on Article.id and MERGE on the LINKS_TO relationship, so a
re-run converges instead of duplicating.

Usage:
    python -m tools.wiki_neo4j_load --manifest-dir data/wiki/simplewiki_full
    python -m tools.wiki_neo4j_load --manifest-dir data/wiki/simplewiki_full --limit 50000
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

from neo4j import GraphDatabase

CONSTRAINT_CYPHER = (
    "CREATE CONSTRAINT article_id IF NOT EXISTS FOR (a:Article) REQUIRE a.id IS UNIQUE"
)

NODE_MERGE_CYPHER = """
UNWIND $rows AS r
MERGE (a:Article {id: r.id})
SET a.title = r.title, a.ts = r.ts
"""

# MATCH (not MERGE) the endpoints: every edge id is an ns0 article that the node
# pass already loaded, so this avoids re-creating title-less stub nodes. Missing
# endpoints (only possible under an article cap) are silently skipped.
EDGE_MERGE_CYPHER = """
UNWIND $rows AS r
MATCH (s:Article {id: r.src})
MATCH (d:Article {id: r.dst})
MERGE (s)-[:LINKS_TO]->(d)
"""


def _load_manifest(manifest_dir: Path) -> dict:
    return json.loads((manifest_dir / "manifest.json").read_text())


def _iter_articles(
    manifest_dir: Path, manifest: dict, limit: int | None
) -> Iterator[dict]:
    """Stream {id,title,ts} records across the article shards, capped by ``limit``."""
    n = 0
    for shard in manifest["shards"]["articles"]["files"]:
        with (manifest_dir / shard["path"]).open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                yield {
                    "id": int(rec["id"]),
                    "title": rec["title"],
                    "ts": rec.get("ts", ""),
                }
                n += 1
                if limit is not None and n >= limit:
                    return


def _iter_edges(
    manifest_dir: Path, manifest: dict, limit: int | None
) -> Iterator[dict]:
    """Stream {src,dst} edges across the TSV shards, capped by ``limit``."""
    n = 0
    for shard in manifest["shards"]["edges"]["files"]:
        with (manifest_dir / shard["path"]).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                src, dst = line.split("\t")
                yield {"src": int(src), "dst": int(dst)}
                n += 1
                if limit is not None and n >= limit:
                    return


def _batched(it: Iterator[dict], size: int) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for row in it:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _run_batches(
    session, cypher: str, rows: Iterator[dict], batch_size: int, label: str
) -> int:
    total = 0
    for batch in _batched(rows, batch_size):
        session.run(cypher, rows=batch).consume()
        total += len(batch)
        print(f"  [{label}] {total:,}", end="\r", flush=True)
    print(f"  [{label}] {total:,}   ")
    return total


def load(
    manifest_dir: Path,
    uri: str,
    user: str,
    password: str,
    database: str,
    batch_size: int,
    article_limit: int | None,
    edge_limit: int | None,
) -> dict:
    manifest = _load_manifest(manifest_dir)
    m_articles = manifest["counts"]["articles"]
    m_edges = manifest["counts"]["edges"]

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            print(f"constraint: {CONSTRAINT_CYPHER}")
            session.run(CONSTRAINT_CYPHER).consume()

            print(
                f"nodes: streaming articles "
                f"(limit={article_limit if article_limit is not None else 'full'})"
            )
            loaded_nodes = _run_batches(
                session,
                NODE_MERGE_CYPHER,
                _iter_articles(manifest_dir, manifest, article_limit),
                batch_size,
                "nodes",
            )

            print(
                f"edges: streaming LINKS_TO "
                f"(limit={edge_limit if edge_limit is not None else 'full'})"
            )
            processed_edges = _run_batches(
                session,
                EDGE_MERGE_CYPHER,
                _iter_edges(manifest_dir, manifest, edge_limit),
                batch_size,
                "edges",
            )

            node_count = session.run("MATCH (a:Article) RETURN count(a) AS c").single()[
                "c"
            ]
            rel_count = session.run(
                "MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS c"
            ).single()["c"]
    finally:
        driver.close()

    return {
        "manifest_articles": m_articles,
        "manifest_edges": m_edges,
        "loaded_nodes": loaded_nodes,
        "processed_edges": processed_edges,
        "node_count": node_count,
        "rel_count": rel_count,
        "full_load": article_limit is None and edge_limit is None,
    }


def _reconcile(res: dict) -> int:
    """Print a reconciliation table and return a process exit code."""
    print("\n=== reconciliation ===")
    print(f"manifest articles : {res['manifest_articles']:,}")
    print(f"manifest edges    : {res['manifest_edges']:,}")
    print(f"graph :Article     : {res['node_count']:,}")
    print(f"graph :LINKS_TO    : {res['rel_count']:,}")
    print(f"edges processed    : {res['processed_edges']:,}")

    rc = 0
    if res["full_load"]:
        # Nodes are deterministic (MERGE on unique id) -> must match exactly.
        if res["node_count"] != res["manifest_articles"]:
            print(
                f"MISMATCH nodes: graph {res['node_count']:,} "
                f"!= manifest {res['manifest_articles']:,}"
            )
            rc = 1
        else:
            print("OK nodes reconcile with manifest.")
        # Relationships: MERGE collapses any duplicate (src,dst) rows, so the graph
        # count is <= edge rows. Report the dedup delta rather than false-failing.
        deduped = res["processed_edges"] - res["rel_count"]
        if res["rel_count"] > res["processed_edges"]:
            print(
                f"MISMATCH rels: graph {res['rel_count']:,} "
                f"> processed {res['processed_edges']:,}"
            )
            rc = 1
        else:
            print(
                f"OK rels reconcile ({deduped:,} duplicate edge rows collapsed by MERGE)."
            )
    else:
        print(
            "(bounded --limit load: counts are of the loaded slice, not the manifest)"
        )
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path("data/wiki/simplewiki_full"),
        help="wiki_extract manifest directory (contains manifest.json + shards)",
    )
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="testpassword")
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--batch-size", type=int, default=10000)
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap EDGES loaded (quick smoke load). Default: full corpus.",
    )
    ap.add_argument(
        "--article-limit",
        type=int,
        default=None,
        help="optionally cap ARTICLE nodes loaded (default: all articles).",
    )
    args = ap.parse_args(argv)

    if not (args.manifest_dir / "manifest.json").exists():
        print(f"manifest {args.manifest_dir}/manifest.json missing")
        return 1

    res = load(
        manifest_dir=args.manifest_dir,
        uri=args.uri,
        user=args.user,
        password=args.password,
        database=args.database,
        batch_size=args.batch_size,
        article_limit=args.article_limit,
        edge_limit=args.limit,
    )
    return _reconcile(res)


if __name__ == "__main__":
    raise SystemExit(main())

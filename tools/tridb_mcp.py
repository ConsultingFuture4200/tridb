"""TriDB agent-memory MCP server (advisor plan 098) — the first-user surface.

Exposes TriDB (the stock release image, tridb/postgres-trimodal:pg16|pg17) as
agent memory over the Model Context Protocol: any MCP-capable agent gets
store/connect/recall with zero integration code. Five tools, stdio transport:

  store_memory(text, kind?, embedding?)          -> {id}
  connect(src_id, dst_id, rel)                   -> {edge}
  recall(query_text|embedding, k, mode, anchor_id?) -> {results, graph_censored, ...}
  neighbors(id, rel?, hops?)                     -> [{id, text, kind}]
  memory_stats()                                 -> {memories, edges, ...}

The one-WAL story IS the demo: store_memory writes the relational row, the
vector, and the graph vertex in ONE transaction — a single Postgres process,
one transaction manager, no cross-system consistency dance.

Dense-id scheme (THE documented scheme, tools/wiki_engine_load.py's identity
lever): memory ids are allocated by this server as 0, 1, 2, ... in insertion
order, and the graph vertex is upserted in the same transaction, so
ext_id == vid holds for every memory. tjs_open's graph leg joins native vids
straight against memories.id (see test/release_stock_smoke.sql), so this
equality is a CORRECTNESS precondition, not an optimization — store_memory
asserts it and refuses to continue on drift. Guaranteed by the v1
single-writer contract (one server process writes the graph).

Recall modes:
  'fused'  — seedless public.tjs_open under the tjs.graph_scoring='ppr'
             default (ADR-0021): vector similarity fused with bounded
             forward-push PPR reserve, i.e. connection-weighted recall.
             With anchor_id it becomes the filter-first path (bounded pull
             traversal from the anchor). Honesty travels: the response
             carries tjs_open_graph_censored() / termination reason.
  'vector' — plain HNSW ORDER BY embedding <-> query.

Embeddings: fastembed (BGE-small, onnx, CPU — no GPU dependency) when the
caller sends bare text; callers can always bring their own vectors via the
`embedding` parameter, which is also the fallback when fastembed or its model
download is unavailable.

Config (env): TRIDB_DSN, TRIDB_MCP_MODEL, TRIDB_MCP_DIM, and the recall
knobs TRIDB_MCP_TERM_COND / TRIDB_MCP_SEEDS / TRIDB_MCP_HOPS.

Run: python -m tools.tridb_mcp --init   (idempotent schema bootstrap)
     python -m tools.tridb_mcp          (stdio server; needs `pip install
                                         -r requirements-mcp.txt`)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

DEFAULT_DSN = "postgresql://postgres:tridb@localhost:5432/postgres"
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"  # 384-d, onnx/CPU via fastembed
DEFAULT_DIM = 384

# Seedless-recall knobs (see ADR-0012/0021): term_cond is THE recall knob
# (consecutive-drops early termination); m_seeds/hops bound the PPR push.
DEFAULT_TERM_COND = 64
DEFAULT_M_SEEDS = 4
DEFAULT_HOPS = 2

RECALL_MODES = ("fused", "vector")


def _vec_literal(embedding: list[float]) -> str:
    """pgvector input literal: '[x,y,...]'."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class Embedder:
    """Thin wrapper around fastembed (onnx, CPU). Swappable for tests."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from fastembed import TextEmbedding

        self.model_name = model_name
        self._m = TextEmbedding(model_name=model_name)

    def encode(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self._m.embed([text])))]


class MemoryStore:
    """The five memory operations over one psycopg connection (autocommit;
    multi-statement writes run inside explicit conn.transaction() blocks)."""

    def __init__(
        self,
        conn: Any,
        *,
        dim: int = DEFAULT_DIM,
        embedder: Embedder | None = None,
        term_cond: int = DEFAULT_TERM_COND,
        m_seeds: int = DEFAULT_M_SEEDS,
        hops: int = DEFAULT_HOPS,
    ):
        self.conn = conn
        self.dim = dim
        self.embedder = embedder
        self.term_cond = term_cond
        self.m_seeds = m_seeds
        self.hops = hops

    # -- embedding ---------------------------------------------------------

    def _resolve_vector(self, text: str | None, embedding: list[float] | None) -> str:
        if embedding is not None:
            if len(embedding) != self.dim:
                raise ValueError(
                    f"embedding has {len(embedding)} dims, store is vector({self.dim})"
                )
            return _vec_literal(embedding)
        if text is None:
            raise ValueError("need text or embedding")
        if self.embedder is None:
            raise RuntimeError(
                "no embedder configured (fastembed unavailable?) — "
                "pass a caller-supplied `embedding` instead"
            )
        vec = self.embedder.encode(text)
        if len(vec) != self.dim:
            raise RuntimeError(
                f"model emits {len(vec)} dims but store is vector({self.dim}) — "
                "set TRIDB_MCP_DIM to match TRIDB_MCP_MODEL"
            )
        return _vec_literal(vec)

    # -- schema ------------------------------------------------------------

    def init_schema(self) -> dict:
        """Idempotent bootstrap: extensions in dependency order, memories
        table + HNSW. Refuses a pre-existing table with a different dim."""
        for ext in ("vector", "graph_store_am", "tjs_pg"):
            self.conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        row = self.conn.execute(
            "SELECT atttypmod FROM pg_attribute"
            " WHERE attrelid = to_regclass('public.memories')"
            " AND attname = 'embedding'"
        ).fetchone()
        if row is not None and row[0] != self.dim:
            raise RuntimeError(
                f"existing memories table is vector({row[0]}), configured dim "
                f"is {self.dim} — set TRIDB_MCP_DIM={row[0]} or drop the table"
            )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            " id bigint PRIMARY KEY,"
            " kind text NOT NULL DEFAULT 'note',"
            " text text NOT NULL,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            f" embedding vector({self.dim}) NOT NULL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS memories_hnsw ON memories"
            " USING hnsw (embedding vector_l2_ops)"
        )
        return {"ok": True, "dim": self.dim}

    # -- tools -------------------------------------------------------------

    def store_memory(
        self,
        text: str,
        kind: str = "note",
        embedding: list[float] | None = None,
    ) -> dict:
        """Insert row + vector + graph vertex ATOMICALLY (one txn, one WAL)."""
        vec = self._resolve_vector(text, embedding)
        with self.conn.transaction():
            mid = self.conn.execute(
                "SELECT COALESCE(max(id) + 1, 0) FROM memories"
            ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO memories (id, kind, text, embedding)"
                " VALUES (%s, %s, %s, %s::vector)",
                (mid, kind, text, vec),
            )
            vid = self.conn.execute(
                "SELECT graph_store.gph_upsert_vertex(%s)", (mid,)
            ).fetchone()[0]
            if vid != mid:
                # ext_id == vid is what lets tjs_open join graph vids against
                # memories.id — drift means another writer broke the dense
                # scheme. Abort (the txn rolls back) rather than store a
                # memory the graph leg would silently mis-address.
                raise RuntimeError(
                    f"dense-id drift: memory id {mid} mapped to vid {vid} — "
                    "single-writer contract violated"
                )
        return {"id": mid}

    def connect(self, src_id: int, dst_id: int, rel: str) -> dict:
        """Typed directed edge src -> dst; rel names auto-register."""
        with self.conn.transaction():
            known = [
                r[0]
                for r in self.conn.execute(
                    "SELECT id FROM memories WHERE id = ANY(%s)",
                    ([src_id, dst_id],),
                ).fetchall()
            ]
            for mid in (src_id, dst_id):
                if mid not in known:
                    raise ValueError(f"unknown memory id {mid}")
            type_id = self.conn.execute(
                "SELECT graph_store.register_edge_type(%s)", (rel,)
            ).fetchone()[0]
            self.conn.execute(
                "SELECT graph_store.gph_insert_edge(%s, %s, %s)",
                (src_id, dst_id, type_id),
            )
        return {"edge": {"src": src_id, "dst": dst_id, "rel": rel, "type_id": type_id}}

    def recall(
        self,
        query_text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 8,
        mode: str = "fused",
        anchor_id: int | None = None,
    ) -> dict:
        """Top-k memories. 'fused' = tjs_open (PPR-graded seedless, or
        filter-first from anchor_id); 'vector' = plain HNSW. Row order is the
        operator's ranking; `score` is -L2 distance, for reference only."""
        if mode not in RECALL_MODES:
            raise ValueError(f"mode must be one of {RECALL_MODES}")
        vec = self._resolve_vector(query_text, embedding)
        if mode == "vector":
            rows = self.conn.execute(
                "SELECT id, text, kind, embedding <-> %s::vector AS dist"
                " FROM memories ORDER BY embedding <-> %s::vector LIMIT %s",
                (vec, vec, k),
            ).fetchall()
            censored = None
            reason = None
        else:
            # Vector-first/seedless REQUIRES relaxed-order iterative scan
            # (pgvector >= 0.8); harmless for the anchor/filter-first path.
            self.conn.execute("SET hnsw.iterative_scan = relaxed_order")
            rows = self.conn.execute(
                "SELECT m.id, m.text, m.kind, m.embedding <-> %s::vector AS dist"
                " FROM public.tjs_open('memories', %s, %s, %s, %s, 'id', '',"
                "                      %s::vector, %s, 0)"
                "      WITH ORDINALITY AS x(t, ord)"
                " JOIN memories m ON m.id = x.t ORDER BY x.ord",
                (vec, k, self.term_cond, self.m_seeds, self.hops, vec, anchor_id),
            ).fetchall()
            censored = self.conn.execute(
                "SELECT public.tjs_open_graph_censored()"
            ).fetchone()[0]
            reason = self.conn.execute(
                "SELECT public.tjs_open_termination_reason()"
            ).fetchone()[0]
        return {
            "mode": mode,
            "results": [
                {"id": r[0], "text": r[1], "kind": r[2], "score": -float(r[3])}
                for r in rows
            ],
            "graph_censored": censored,
            "termination_reason": reason,
        }

    def neighbors(self, id: int, rel: str | None = None, hops: int = 1) -> list[dict]:
        """Direct graph read: 1-hop typed out-neighbors (gph_traverse_typed)
        or the multi-hop reach (gph_traverse_bfs)."""
        if rel is None:
            type_id = 0  # GPH_EDGE_TYPE_ANY
        else:
            row = self.conn.execute(
                "SELECT id FROM graph_store.edge_type WHERE name = %s", (rel,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown edge type {rel!r}")
            type_id = row[0]
        if hops == 1:
            # target-list position (ProjectSet), per the extension's contract;
            # the outer query only unpacks the composite's dst field.
            ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT (e).dst FROM (SELECT graph_store.gph_traverse_typed("
                    "%s, %s, 0, -1) AS e) s",
                    (id, type_id),
                ).fetchall()
            ]
        else:
            ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT graph_store.gph_traverse_bfs(%s, %s, %s)",
                    (id, hops, type_id),  # (seed, max_depth, type_id)
                ).fetchall()
            ]
        if not ids:
            return []
        detail = {
            r[0]: r
            for r in self.conn.execute(
                "SELECT id, text, kind FROM memories WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        }
        out = []
        seen: set[int] = set()
        for i in ids:  # preserve the engine's emission order
            if i in seen or i not in detail:
                continue
            seen.add(i)
            out.append({"id": i, "text": detail[i][1], "kind": detail[i][2]})
        return out

    def memory_stats(self) -> dict:
        n = self.conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        vertices = self.conn.execute(
            "SELECT graph_store.gph_vertex_count()"
        ).fetchone()[0]
        edges = self.conn.execute(
            "SELECT graph_store.gph_visible_edge_count()"
        ).fetchone()[0]
        types = [
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM graph_store.edge_type ORDER BY id"
            ).fetchall()
        ]
        exts = dict(
            self.conn.execute(
                "SELECT extname, extversion FROM pg_extension"
                " WHERE extname IN ('vector', 'graph_store_am', 'tjs_pg')"
            ).fetchall()
        )
        version = self.conn.execute("SHOW server_version").fetchone()[0]
        return {
            "memories": n,
            "vertices": vertices,
            "edges": edges,
            "edge_types": types,
            "engine": {"server_version": version, "extensions": exts},
        }


# -- MCP layer (thin: every tool is a MemoryStore method) --------------------


def build_server(store: MemoryStore):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("tridb-memory", log_level="WARNING")

    @mcp.tool()
    def store_memory(
        text: str, kind: str = "note", embedding: list[float] | None = None
    ) -> dict:
        """Store one memory: relational row + embedding + graph vertex,
        atomically (one transaction). Returns {"id": <dense memory id>}."""
        return store.store_memory(text, kind=kind, embedding=embedding)

    @mcp.tool()
    def connect(src_id: int, dst_id: int, rel: str) -> dict:
        """Create a typed directed link src_id -> dst_id (rel auto-registers)."""
        return store.connect(src_id, dst_id, rel)

    @mcp.tool()
    def recall(
        query_text: str | None = None,
        embedding: list[float] | None = None,
        k: int = 8,
        mode: str = "fused",
        anchor_id: int | None = None,
    ) -> dict:
        """Recall top-k memories. mode='fused' is connection-weighted
        (tjs_open, PPR-graded); mode='vector' is pure similarity. anchor_id
        scopes fused recall to the anchor's graph neighborhood."""
        return store.recall(
            query_text=query_text,
            embedding=embedding,
            k=k,
            mode=mode,
            anchor_id=anchor_id,
        )

    @mcp.tool()
    def neighbors(id: int, rel: str | None = None, hops: int = 1) -> list[dict]:
        """Linked memories: typed out-neighbors (hops=1) or multi-hop reach."""
        return store.neighbors(id, rel=rel, hops=hops)

    @mcp.tool()
    def memory_stats() -> dict:
        """Counts (memories / vertices / edges / edge types) + engine identity."""
        return store.memory_stats()

    return mcp


def make_store(dsn: str) -> MemoryStore:
    import psycopg

    conn = psycopg.connect(dsn, autocommit=True)
    try:
        embedder: Embedder | None = Embedder(
            os.environ.get("TRIDB_MCP_MODEL", DEFAULT_MODEL)
        )
    except Exception as exc:  # fastembed missing or model undownloadable
        print(
            f"tridb_mcp: no local embedder ({exc}); "
            "callers must supply `embedding` vectors",
            file=sys.stderr,
        )
        embedder = None
    return MemoryStore(
        conn,
        dim=int(os.environ.get("TRIDB_MCP_DIM", DEFAULT_DIM)),
        embedder=embedder,
        term_cond=int(os.environ.get("TRIDB_MCP_TERM_COND", DEFAULT_TERM_COND)),
        m_seeds=int(os.environ.get("TRIDB_MCP_SEEDS", DEFAULT_M_SEEDS)),
        hops=int(os.environ.get("TRIDB_MCP_HOPS", DEFAULT_HOPS)),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--dsn",
        default=os.environ.get("TRIDB_DSN", DEFAULT_DSN),
        help="Postgres DSN (default: $TRIDB_DSN or the release-image local DSN)",
    )
    ap.add_argument(
        "--init",
        action="store_true",
        help="bootstrap the schema (idempotent) and exit",
    )
    args = ap.parse_args(argv)

    store = make_store(args.dsn)
    if args.init:
        info = store.init_schema()
        print(f"tridb_mcp: schema ready (vector({info['dim']}))")
        return 0
    build_server(store).run()  # stdio transport
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

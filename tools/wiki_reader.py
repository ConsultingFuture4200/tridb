"""Browsable offline Wikipedia reader over the enwiki corpus (DEV-1354 action #3).

A single-user personal tool: REVIEW + SEARCH + RELATED + ASK (RAG).

Two subcommands:

  build   Build a SQLite index (`reader.db`) over the article shards plus two
          numpy sidecars, so `serve` can fetch any article body in O(1) and
          answer "related" queries without re-scanning 30 GB of JSONL.

  serve   A stdlib http.server app (bind 127.0.0.1) that serves a single-page
          HTML reader with title search, article view, two "Related" columns
          (semantic neighbours via cuVS CAGRA over the 6.9M BGE vectors, reusing
          tools/wiki_linkpredict.build_cuvs_index; and out-going hyperlinks from
          a CSR adjacency), and an "Ask" box: retrieval-augmented Q&A that embeds
          the question with the same BGE model, retrieves top-k passages over the
          already-loaded CAGRA index, and answers with a small local LLM served
          by ollama (default qwen2.5:7b-instruct), citing the retrieved sources.

Artifacts written by `build`, all next to the corpus (data/wiki/enwiki/):
  reader.db            sqlite: articles(id, title, shard, byte_offset) + titles FTS5
  id2row.i32.npy       reverse map article-id -> embedding row (from emb/ids.i64.npy)
  edges_csr_dst.i32.npy   out-edge dst ids, grouped by src (CSR values)
  edges_csr_off.i64.npy   CSR offsets: out-edges of id = dst[off[id]:off[id+1]]

The corpus files, emb/ files, and baseline docker stack are READ-ONLY here.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

DB_NAME = "reader.db"
ID2ROW_NAME = "id2row.i32.npy"
CSR_DST_NAME = "edges_csr_dst.i32.npy"
CSR_OFF_NAME = "edges_csr_off.i64.npy"
# Undirected CSR (both link directions), for the bidirectional-BFS connection
# finder. Derived from the directed CSR above — a link either direction connects
# the two topics — so a full re-scan of the 224M edge shards is NOT needed.
UNDIR_DST_NAME = "edges_undir_dst.i32.npy"
UNDIR_OFF_NAME = "edges_undir_off.i64.npy"

# --- Ask (RAG) config -------------------------------------------------------- #
# The LLM is a SMALL quantized instruct model served locally by ollama; it must
# stay well under the GPU budget so a later heavy job has the unified pool free.
ASK_MODEL = os.environ.get("WIKI_ASK_MODEL", "qwen2.5:7b-instruct")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
ASK_K = 8  # passages fed to the LLM
PASSAGE_CHARS = 1500  # per-passage body budget so k passages fit the context


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def _article_files(corpus: Path) -> list[str]:
    """Ordered article shard file names from the manifest (shard index == list pos)."""
    manifest = json.loads((corpus / "manifest.json").read_text())
    return [s["path"] for s in manifest["shards"]["articles"]["files"]]


def _edge_files(corpus: Path) -> list[str]:
    manifest = json.loads((corpus / "manifest.json").read_text())
    return [s["path"] for s in manifest["shards"]["edges"]["files"]]


def build_articles(conn: sqlite3.Connection, corpus: Path) -> int:
    """Scan every article shard, recording (id, title, shard, byte_offset).

    Files are read in BINARY so byte_offset is the exact seek position of the
    line start — robust to the few clobbered shards (we index whatever lines are
    actually present, not the manifest's intended row counts)."""
    conn.execute("DROP TABLE IF EXISTS articles")
    conn.execute(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, "
        "shard INTEGER, byte_offset INTEGER)"
    )
    n = 0
    batch: list[tuple] = []
    for shard, name in enumerate(_article_files(corpus)):
        path = corpus / name
        if not path.exists():
            continue
        with path.open("rb") as fh:
            off = 0
            for raw in fh:
                ln = len(raw)
                try:
                    obj = json.loads(raw)
                    batch.append((int(obj["id"]), obj["title"], shard, off))
                except (json.JSONDecodeError, KeyError):
                    pass  # tolerate a truncated final line in a clobbered shard
                off += ln
                if len(batch) >= 50000:
                    conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?)", batch)
                    n += len(batch)
                    batch.clear()
        print(f"[build] articles: shard {shard} ({name}) done, {n + len(batch)} rows so far")
    if batch:
        conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?)", batch)
        n += len(batch)
    conn.commit()
    return n


def build_title_fts(conn: sqlite3.Connection) -> None:
    """FTS5 over titles only (fast, small). Full-body FTS is DEFERRED for v1."""
    conn.execute("DROP TABLE IF EXISTS titles_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE titles_fts USING fts5("
        "title, content='articles', content_rowid='id')"
    )
    conn.execute("INSERT INTO titles_fts(rowid, title) SELECT id, title FROM articles")
    conn.commit()


def build_id2row(corpus: Path) -> None:
    """Reverse map article-id -> embedding row from emb/ids.i64.npy (row -> id)."""
    ids = np.load(corpus / "emb" / "ids.i64.npy")  # row -> article id (sparse)
    max_id = int(ids.max())
    id2row = np.full(max_id + 1, -1, dtype=np.int32)
    id2row[ids] = np.arange(len(ids), dtype=np.int32)
    np.save(corpus / ID2ROW_NAME, id2row)
    print(f"[build] id2row: {len(ids)} rows, max id {max_id}")


def build_edge_csr(corpus: Path) -> None:
    """Build a CSR adjacency (dst grouped by src) from the edge TSV shards.

    out-edges of id == dst[off[id] : off[id+1]]. Uses pandas' C TSV parser to read
    the ~224M edges, then a single argsort to group by src."""
    import pandas as pd

    srcs: list[np.ndarray] = []
    dsts: list[np.ndarray] = []
    for name in _edge_files(corpus):
        path = corpus / name
        if not path.exists() or path.stat().st_size == 0:
            continue
        df = pd.read_csv(
            path, sep="\t", header=None, names=["src", "dst"],
            dtype={"src": np.int32, "dst": np.int32}, engine="c",
        )
        srcs.append(df["src"].to_numpy())
        dsts.append(df["dst"].to_numpy())
        print(f"[build] edges: {name} -> {len(df)} rows")
    src = np.concatenate(srcs)
    dst = np.concatenate(dsts)
    del srcs, dsts
    order = np.argsort(src, kind="stable")
    src_sorted = src[order]
    dst_sorted = np.ascontiguousarray(dst[order])
    max_id = int(src_sorted[-1]) if len(src_sorted) else 0
    # off[i] = first index in src_sorted whose src == i (searchsorted left)
    off = np.searchsorted(src_sorted, np.arange(max_id + 2), side="left").astype(np.int64)
    np.save(corpus / CSR_DST_NAME, dst_sorted)
    np.save(corpus / CSR_OFF_NAME, off)
    print(f"[build] edges CSR: {len(dst_sorted)} edges, max src id {max_id}")


def build_undirected_csr(corpus: Path) -> None:
    """Build an UNDIRECTED CSR from the directed out-edge CSR sidecars.

    The connection finder treats a hyperlink as a symmetric connection (a link in
    either direction connects the two topics), so it needs both A->B and B->A. We
    reconstruct that from the directed CSR already on disk — no second pass over the
    224M-edge TSV shards. For every directed edge (u,v) we emit both (u,v) and
    (v,u), then re-group by src. Duplicates (when both u->v and v->u existed) are
    harmless for BFS and left in.

    undir out-edges of id == dst[off[id] : off[id+1]]."""
    dst = np.load(corpus / CSR_DST_NAME)  # directed out-edge dsts, grouped by src
    off = np.load(corpus / CSR_OFF_NAME)  # off[i]..off[i+1] = out-edges of node i
    n_nodes = len(off) - 1
    counts = np.diff(off).astype(np.int64)
    # reconstruct the src of every directed edge (src i repeated deg(i) times)
    src = np.repeat(np.arange(n_nodes, dtype=np.int32), counts)
    # symmetric edge set: (src,dst) + (dst,src)
    u = np.concatenate([src, dst])
    v = np.concatenate([dst, src.astype(dst.dtype)])
    del src, counts
    max_id = int(max(int(u.max()), int(v.max()))) if u.size else 0
    order = np.argsort(u, kind="stable")
    u_sorted = u[order]
    v_sorted = np.ascontiguousarray(v[order])
    del u, v, order
    new_off = np.searchsorted(
        u_sorted, np.arange(max_id + 2), side="left"
    ).astype(np.int64)
    np.save(corpus / UNDIR_DST_NAME, v_sorted)
    np.save(corpus / UNDIR_OFF_NAME, new_off)
    mb = (
        (corpus / UNDIR_DST_NAME).stat().st_size
        + (corpus / UNDIR_OFF_NAME).stat().st_size
    ) / 1e6
    print(
        f"[build] undirected CSR: {len(v_sorted):,} directed half-edges "
        f"(2x {len(dst):,}), max node id {max_id}, sidecars {mb:.0f} MB"
    )


def cmd_build(corpus: Path) -> None:
    t0 = time.time()
    db_path = corpus / DB_NAME
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    print("[build] indexing article shards (id/title/shard/offset)...")
    n_art = build_articles(conn, corpus)
    print(f"[build] {n_art} articles indexed in {time.time() - t0:.1f}s")

    print("[build] building title FTS5...")
    build_title_fts(conn)
    conn.close()

    print("[build] building id->row reverse map...")
    build_id2row(corpus)

    print("[build] building edge CSR adjacency...")
    build_edge_csr(corpus)

    print("[build] building undirected CSR (for connection finder)...")
    build_undirected_csr(corpus)

    db_mb = db_path.stat().st_size / 1e6
    csr_mb = (
        (corpus / CSR_DST_NAME).stat().st_size + (corpus / CSR_OFF_NAME).stat().st_size
    ) / 1e6
    print(
        f"\n[build] DONE in {time.time() - t0:.1f}s | reader.db {db_mb:.0f} MB | "
        f"CSR sidecars {csr_mb:.0f} MB | id2row "
        f"{(corpus / ID2ROW_NAME).stat().st_size / 1e6:.0f} MB"
    )


# --------------------------------------------------------------------------- #
# Serve — shared state loaded once at startup
# --------------------------------------------------------------------------- #
class Reader:
    def __init__(self, corpus: Path):
        self.corpus = corpus
        self.art_files = _article_files(corpus)
        meta = json.loads((corpus / "emb" / "meta.json").read_text())
        self.n, self.dim = int(meta["N"]), int(meta["dim"])

        print(f"[serve] opening {DB_NAME} ...")
        self.db = sqlite3.connect(corpus / DB_NAME, check_same_thread=False)
        self.db_lock = threading.Lock()

        print("[serve] loading id<->row maps and CSR adjacency ...")
        self.row2id = np.load(corpus / "emb" / "ids.i64.npy")  # row -> article id
        self.id2row = np.load(corpus / ID2ROW_NAME)  # article id -> row (-1 if none)
        self.csr_dst = np.load(corpus / CSR_DST_NAME)
        self.csr_off = np.load(corpus / CSR_OFF_NAME)

        # Undirected CSR for the connection finder (bidirectional BFS). mmap'd so
        # the ~1.8 GB of dst values page in on demand rather than eagerly. Optional:
        # if it was not built, /path degrades to a clear error instead of crashing.
        undir_dst = corpus / UNDIR_DST_NAME
        undir_off = corpus / UNDIR_OFF_NAME
        if undir_dst.exists() and undir_off.exists():
            self.undir_dst = np.load(undir_dst, mmap_mode="r")
            self.undir_off = np.load(undir_off, mmap_mode="r")
            print(f"[serve] undirected CSR loaded ({len(self.undir_dst):,} half-edges)")
        else:
            self.undir_dst = None
            self.undir_off = None
            print("[serve] undirected CSR absent — connection finder disabled")

        print(f"[serve] memmapping {self.n}x{self.dim} vectors ...")
        self.vecs = np.memmap(
            corpus / "emb" / "vectors.f32", dtype=np.float32, mode="r",
            shape=(self.n, self.dim),
        )

        print("[serve] building cuVS CAGRA index (~49s over 6.9M vectors) ...")
        from wiki_linkpredict import build_cuvs_index

        vecs_ram = np.ascontiguousarray(self.vecs)  # real RAM copy for the GPU build
        self.index = build_cuvs_index(vecs_ram, self.row2id, graph_degree=32)
        self.index_lock = threading.Lock()

        # Ask (RAG): the question embedder is lazy — fastembed loads on the first
        # /ask so startup stays ~49s. Same BGE model the corpus was embedded with.
        self._embedder = None
        self._embedder_lock = threading.Lock()
        print("[serve] ready.")

    # -- queries ----------------------------------------------------------- #
    def search(self, q: str, limit: int = 40) -> list[dict]:
        tokens = re.findall(r"\w+", q)
        if not tokens:
            return []
        # quote each token; prefix-match the last so partial words still hit
        parts = [f'"{t}"' for t in tokens[:-1]] + [f'"{tokens[-1]}"*']
        match = " ".join(parts)
        with self.db_lock:
            try:
                rows = self.db.execute(
                    "SELECT rowid, title FROM titles_fts WHERE titles_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{"id": r[0], "title": r[1]} for r in rows]

    def _titles(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        qmarks = ",".join("?" * len(ids))
        with self.db_lock:
            rows = self.db.execute(
                f"SELECT id, title FROM articles WHERE id IN ({qmarks})", ids
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def article(self, aid: int) -> dict | None:
        with self.db_lock:
            row = self.db.execute(
                "SELECT title, shard, byte_offset FROM articles WHERE id=?", (aid,)
            ).fetchone()
        if row is None:
            return None
        title, shard, off = row
        path = self.corpus / self.art_files[shard]
        with path.open("rb") as fh:
            fh.seek(off)
            obj = json.loads(fh.readline())
        return {"id": aid, "title": title, "body": _clean_body(obj.get("text", ""))}

    def semantic(self, aid: int, k: int = 10) -> list[dict]:
        if aid >= len(self.id2row):
            return []
        row = int(self.id2row[aid])
        if row < 0:
            return []
        q = np.ascontiguousarray(self.vecs[row : row + 1])
        with self.index_lock:
            labels, dists = self.index.knn_query(q, k=k + 1)
        neigh_ids, scores = [], {}
        for lab, d in zip(labels[0], dists[0]):
            lab = int(lab)
            if lab == aid:
                continue
            neigh_ids.append(lab)
            scores[lab] = round(1.0 - float(d), 4)  # 1 - cosine distance -> cosine
            if len(neigh_ids) >= k:
                break
        titles = self._titles(neigh_ids)
        return [
            {"id": i, "title": titles[i], "score": scores[i]}
            for i in neigh_ids
            if i in titles
        ]

    def hyperlinks(self, aid: int, cap: int = 25) -> list[dict]:
        if aid + 1 >= len(self.csr_off):
            return []
        lo, hi = int(self.csr_off[aid]), int(self.csr_off[aid + 1])
        dst_ids = [int(x) for x in self.csr_dst[lo:hi]]
        titles = self._titles(dst_ids)  # some dsts may be missing (clobbered shards)
        out = [{"id": i, "title": titles[i]} for i in dst_ids if i in titles]
        return out[:cap]

    # -- Connection finder (bidirectional BFS over the undirected graph) ---- #
    def resolve(self, s: str) -> dict | None:
        """Resolve a user token (a numeric article id, an exact title, or a free-
        text title query) to {id, title}. Numeric ids win; then exact title; then
        the best FTS title hit — so the two /path fields can reuse title search."""
        s = (s or "").strip()
        if not s:
            return None
        if s.isdigit():
            aid = int(s)
            with self.db_lock:
                row = self.db.execute(
                    "SELECT title FROM articles WHERE id=?", (aid,)
                ).fetchone()
            return {"id": aid, "title": row[0]} if row else None
        with self.db_lock:
            row = self.db.execute(
                "SELECT id, title FROM articles WHERE title=? LIMIT 1", (s,)
            ).fetchone()
        if row:
            return {"id": int(row[0]), "title": row[1]}
        hits = self.search(s, limit=1)
        return hits[0] if hits else None

    def _undir_neighbors(self, node: int) -> list[int]:
        """Undirected out-neighbours of `node` as a python list (fast C tolist)."""
        off = self.undir_off
        if node < 0 or node + 1 >= len(off):
            return []
        lo, hi = int(off[node]), int(off[node + 1])
        return self.undir_dst[lo:hi].tolist()

    def connection_path(
        self, a: int, b: int, max_hops: int = 6, max_expand: int = 400_000
    ) -> list[int] | None:
        """Shortest undirected hyperlink path a..b via bounded bidirectional BFS.

        Two frontiers grow alternately (always the smaller one) until they meet.
        Bounded two ways so a query on hub-heavy neighbourhoods stays fast: at most
        `max_hops` total levels and at most `max_expand` nodes touched across both
        sides. Returns the ordered node-id list (inclusive of a and b), [a] if a==b,
        or None if no path within the bounds."""
        if self.undir_off is None:
            return None
        if a == b:
            return [a]
        n = len(self.undir_off) - 1
        if not (0 <= a < n) or not (0 <= b < n):
            return None
        # parent maps double as visited sets; sentinel -1 marks a frontier root.
        pa: dict[int, int] = {a: -1}
        pb: dict[int, int] = {b: -1}
        fa, fb = [a], [b]
        meet: int | None = None
        for _ in range(max_hops):
            if not fa or not fb:
                break
            # expand the smaller frontier (keeps the search balanced + cheap)
            if len(fa) <= len(fb):
                frontier, parent, other = fa, pa, pb
                is_a = True
            else:
                frontier, parent, other = fb, pb, pa
                is_a = False
            nxt: list[int] = []
            for node in frontier:
                for nb in self._undir_neighbors(node):
                    if nb in parent:
                        continue
                    parent[nb] = node
                    if nb in other:
                        meet = nb
                        break
                    nxt.append(nb)
                if meet is not None:
                    break
                if len(pa) + len(pb) > max_expand:
                    break
            if meet is not None:
                break
            if len(pa) + len(pb) > max_expand:
                break
            if is_a:
                fa = nxt
            else:
                fb = nxt
        if meet is None:
            return None
        # a .. meet  (walk pa back from meet), then meet .. b (walk pb forward)
        left: list[int] = []
        x: int = meet
        while x != -1:
            left.append(x)
            x = pa[x]
        left.reverse()
        right: list[int] = []
        x = pb[meet]
        while x != -1:
            right.append(x)
            x = pb[x]
        return left + right

    def path(self, a: int, b: int, max_hops: int = 6) -> dict:
        """/path payload: resolved endpoints + the hop chain (or a clear miss)."""
        node_ids = self.connection_path(a, b, max_hops=max_hops)
        endpoints = self._titles([a, b])
        frm = {"id": a, "title": endpoints.get(a, "?")}
        to = {"id": b, "title": endpoints.get(b, "?")}
        if node_ids is None:
            reason = (
                "connection finder disabled (undirected index not built)"
                if self.undir_off is None
                else f"no path found within {max_hops} hops"
            )
            return {"from": frm, "to": to, "found": False, "reason": reason}
        titles = self._titles(node_ids)
        chain = [{"id": i, "title": titles.get(i, f"[{i}]")} for i in node_ids]
        return {
            "from": frm,
            "to": to,
            "found": True,
            "hops": len(node_ids) - 1,
            "path": chain,
        }

    def narrate_path(self, chain: list[dict]) -> str:
        """Optional one-paragraph LLM narration of a resolved chain (reuses the
        same local ollama backend as /ask). Best-effort — the chain is the real
        deliverable, so any failure returns an explicit, non-fabricated note."""
        titles = " -> ".join(c["title"] for c in chain)
        system = (
            "You explain how two Wikipedia topics are connected, given an ordered "
            "chain of intermediate articles linked by hyperlinks. Write ONE short "
            "paragraph tracing the connection along the chain. Be concise and do "
            "not invent links not implied by the chain order."
        )
        user = f"Connection chain:\n{titles}\n\nExplain how the first connects to the last:"
        payload = {
            "model": ASK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.3, "num_ctx": 2048, "num_predict": 256},
        }
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
            return resp["message"]["content"].strip()
        except Exception as e:
            return f"[LLM unavailable: {e!r}. The hyperlink chain above still holds.]"

    # -- Fused related (meaning x topology) -------------------------------- #
    def related_fused(
        self, aid: int, k: int = 15, pool: int = 50, rrf_k: int = 60
    ) -> dict:
        """Primary 'Related (fused)' ranking: reciprocal-rank fusion of the cosine
        (semantic) ranking and a bounded graph-proximity ranking, so articles that
        are BOTH semantically near AND topologically close rank highest.

        Mirrors the RRF method in tools/wiki_linkpredict_fused.py (fuse the cosine
        rank with a topology rank), adapted for serve-time: the graph ranking is
        1-hop out-neighbours (directly linked) followed by 2-hop neighbours ranked
        by co-citation count (how many of A's out-links also point at them). Both
        hops are bounded so the call stays fast on hub pages.

        Provenance per result: 'meaning + linked' (in both), 'meaning only' (cosine
        only), 'linked (1-hop)' / 'linked (2-hop)' (graph only)."""
        from collections import Counter

        # -- semantic (cosine) ranking --
        sem = self.semantic(aid, k=pool)
        cos_rank = {d["id"]: i for i, d in enumerate(sem)}
        cos_score = {d["id"]: d["score"] for d in sem}

        # -- graph-proximity ranking (1-hop then 2-hop by co-citation) --
        direct_ids = [int(x) for x in self.csr_dst[
            int(self.csr_off[aid]): int(self.csr_off[aid + 1])
        ]] if aid + 1 < len(self.csr_off) else []
        direct_set = set(direct_ids)
        cocite: Counter = Counter()
        cap_direct, cap_out = 300, 64  # bound the 2-hop fan-out
        for nb in direct_ids[:cap_direct]:
            if nb + 1 >= len(self.csr_off):
                continue
            lo, hi = int(self.csr_off[nb]), int(self.csr_off[nb + 1])
            for x in self.csr_dst[lo:hi][:cap_out].tolist():
                if x == aid or x in direct_set:
                    continue
                cocite[x] += 1
        two_hop = [i for i, _ in cocite.most_common(pool)]
        # graph order: direct links first (highest proximity), then 2-hop by count
        graph_order = direct_ids + two_hop
        graph_rank = {i: r for r, i in enumerate(graph_order)}
        hop = {i: 1 for i in direct_ids}
        for i in two_hop:
            hop.setdefault(i, 2)

        # -- RRF fuse: a missing rank in a modality contributes 0 --
        ids = set(cos_rank) | set(graph_rank)
        ids.discard(aid)
        scored = []
        for i in ids:
            s = 0.0
            if i in cos_rank:
                s += 1.0 / (rrf_k + cos_rank[i])
            if i in graph_rank:
                s += 1.0 / (rrf_k + graph_rank[i])
            scored.append((s, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        top = [i for _, i in scored[: k * 2]]  # over-fetch; some titles may be missing
        titles = self._titles(top)

        fused = []
        for s, i in scored:
            if i not in titles:
                continue
            in_cos = i in cos_rank
            in_dir = i in direct_set
            if in_dir and in_cos:
                prov = "meaning + linked"
            elif in_cos:
                prov = "meaning only"
            elif hop.get(i) == 1:
                prov = "linked (1-hop)"
            else:
                prov = "linked (2-hop)"
            fused.append({
                "id": i,
                "title": titles[i],
                "rrf": round(s, 5),
                "prov": prov,
                "cos": cos_score.get(i),  # cosine similarity if semantically ranked
                "cocite": cocite.get(i) or None,  # 2-hop co-citation count if any
            })
            if len(fused) >= k:
                break
        return {
            "fused": fused,
            "semantic": sem[:12],  # component breakdown (existing panel)
            "hyperlinks": self.hyperlinks(aid),  # component breakdown (existing panel)
        }

    # -- Ask (RAG) --------------------------------------------------------- #
    def _get_embedder(self):
        if self._embedder is None:
            with self._embedder_lock:
                if self._embedder is None:
                    from wiki_linkpredict import DEFAULT_DIM, DEFAULT_MODEL, Embedder

                    print("[serve] loading question embedder (fastembed BGE, CPU) ...")
                    self._embedder = Embedder(DEFAULT_MODEL, DEFAULT_DIM)
        return self._embedder

    def retrieve(self, q: str, k: int = ASK_K) -> list[dict]:
        """Embed the question with the SAME BGE model as the corpus, then pull the
        top-k passages from the already-loaded CAGRA index. Missing articles (the
        ~4% clobbered) are skipped, so we over-fetch a few to still land k."""
        emb = self._get_embedder()
        v = emb.encode([q])
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
        v = np.ascontiguousarray(v, dtype=np.float32)
        with self.index_lock:
            labels, dists = self.index.knn_query(v, k=k + 4)
        passages: list[dict] = []
        for lab, d in zip(labels[0], dists[0]):
            aid = int(lab)
            art = self.article(aid)  # None if the article shard was clobbered
            if art is None:
                continue
            passages.append(
                {
                    "id": aid,
                    "title": art["title"],
                    "score": round(1.0 - float(d), 4),
                    "body": art["body"][:PASSAGE_CHARS],
                }
            )
            if len(passages) >= k:
                break
        return passages

    def _llm_answer(self, q: str, passages: list[dict]) -> str:
        """Grounded generation via the local ollama HTTP API. The model is told to
        answer ONLY from the numbered passages and to cite them; num_ctx is set
        explicitly because ollama otherwise defaults to 2048 and would truncate."""
        ctx = "\n\n".join(f"[{i + 1}] {p['title']}\n{p['body']}" for i, p in enumerate(passages))
        system = (
            "You answer questions using ONLY the provided Wikipedia passages. "
            "Cite the passage numbers you rely on inline, like [1] or [2]. "
            "If the passages do not contain the answer, say so plainly. "
            "Do not use any outside knowledge."
        )
        user = f"Passages:\n\n{ctx}\n\nQuestion: {q}\n\nAnswer (cite passages):"
        payload = {
            "model": ASK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 512},
        }
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read())
            return resp["message"]["content"].strip()
        except Exception as e:  # surface the failure honestly rather than fabricate
            return (
                f"[LLM unavailable: {e!r}. Is ollama serving '{ASK_MODEL}' at "
                f"{OLLAMA_URL}? The retrieved sources below are still valid.]"
            )

    def ask(self, q: str, k: int = ASK_K) -> dict:
        q = (q or "").strip()
        if not q:
            return {"answer": "Ask a question.", "sources": []}
        passages = self.retrieve(q, k=k)
        if not passages:
            return {
                "answer": "No matching Wikipedia articles were found for this question.",
                "sources": [],
            }
        answer = self._llm_answer(q, passages)
        sources = [
            {"n": i + 1, "id": p["id"], "title": p["title"], "score": p["score"]}
            for i, p in enumerate(passages)
        ]
        return {"answer": answer, "sources": sources}


def _clean_body(text: str) -> str:
    """Light wikitext-residue cleanup — the extractor already stripped most markup.

    Drops image/thumb caption fragments and collapses blank runs. Intentionally
    minimal; not a full renderer."""
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            lines.append("")
            continue
        # thumb/file captions look like "thumb|..." or "left|300px|..."
        if re.match(r"^(thumb|left|right|upright|\d+px)\|", s):
            s = s.split("|")[-1].strip()
        if s.startswith(("File:", "Image:")):
            continue
        lines.append(s)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Offline Wikipedia</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  color:#1a1a1a; background:#fafafa; }
header { padding:10px 16px; background:#36c; color:#fff; display:flex; gap:12px;
  align-items:center; position:sticky; top:0; z-index:10; }
header h1 { font-size:16px; margin:0; font-weight:600; white-space:nowrap; }
#q { flex:1; padding:8px 12px; font-size:15px; border:0; border-radius:4px; }
#ask { flex:1.4; padding:8px 12px; font-size:15px; border:0; border-radius:4px;
  background:#fff8e1; }
.answer { line-height:1.7; font-size:15px; white-space:pre-wrap; margin-bottom:8px;
  background:#fffbea; border:1px solid #f0e2b0; border-radius:6px; padding:14px 16px; }
main { display:grid; grid-template-columns:260px 1fr 300px; gap:0; height:calc(100vh - 96px); }
#results, #related { overflow-y:auto; padding:8px; border-right:1px solid #ddd; background:#fff; }
#related { border-right:0; border-left:1px solid #ddd; }
#article { overflow-y:auto; padding:20px 32px; max-width:820px; }
#article h2 { margin-top:0; }
.item { padding:6px 8px; cursor:pointer; border-radius:4px; font-size:14px; line-height:1.3; }
.item:hover { background:#eef3ff; }
.secttl { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:#888;
  margin:12px 8px 4px; font-weight:600; }
p { line-height:1.6; }
.hint { color:#999; padding:16px; }
.hd { color:#555; font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  margin:16px 8px 2px; font-weight:600; }
.legend { color:#aaa; font-size:10.5px; font-style:italic; margin:0 8px 6px;
  line-height:1.3; }
.rtitle { line-height:1.3; }
.relbarwrap { margin-top:4px; display:flex; align-items:center; gap:6px; }
.relbar { flex:1; height:6px; background:#eceef2; border-radius:3px; overflow:hidden; }
.relfill { height:100%; background:#36c; border-radius:3px; }
.bucket { font-size:11px; color:#555; white-space:nowrap; }
.num { font-size:10px; color:#bbb; white-space:nowrap; font-variant-numeric:tabular-nums; }
.foot { color:#aaa; font-size:10.5px; padding:10px 8px 4px; border-top:1px solid #eee;
  margin-top:14px; line-height:1.45; }
#connectbar { display:flex; gap:8px; align-items:center; padding:6px 16px;
  background:#eef1f6; border-bottom:1px solid #dcdfe6; position:sticky; top:52px;
  z-index:9; font-size:13px; }
#connectbar input { padding:6px 10px; border:1px solid #ccd2dc; border-radius:4px;
  font-size:13px; background:#fff; }
#from, #to { flex:1; min-width:0; max-width:300px; }
#cbtn { padding:6px 16px; border:0; border-radius:4px; background:#36c; color:#fff;
  cursor:pointer; font-size:13px; }
#cbtn:hover { background:#25b; }
.cblabel { color:#555; font-weight:600; white-space:nowrap; }
.cbarrow { color:#999; }
.subtle { color:#888; font-size:12px; margin:2px 0 12px; }
.chain { display:flex; flex-wrap:wrap; align-items:center; gap:7px; line-height:2.1; }
.chip { background:#eef3ff; border:1px solid #cdddff; color:#25b; cursor:pointer;
  padding:4px 11px; border-radius:14px; font-size:14px; }
.chip:hover { background:#dfeaff; }
.arr { color:#aaa; font-size:15px; }
.lbtn { padding:5px 12px; border:1px solid #ccd2dc; border-radius:4px; background:#fff;
  cursor:pointer; font-size:12px; color:#444; }
.lbtn:hover { background:#f2f4f8; }
.pv { font-size:9.5px; text-transform:uppercase; letter-spacing:.03em; padding:1px 5px;
  border-radius:8px; white-space:nowrap; font-weight:600; }
.pv-both { background:#e3f5e6; color:#1a7a2e; }
.pv-mean { background:#e8eefc; color:#2554c7; }
.pv-link { background:#f4ecf9; color:#7a4aa0; }
</style></head>
<body>
<header>
  <h1>Offline Wikipedia</h1>
  <input id="q" placeholder="Search titles (e.g. Ada Lovelace) — Enter" autofocus>
  <input id="ask" placeholder="Ask a question (RAG over 6.9M articles) — Enter">
</header>
<div id="connectbar">
  <span class="cblabel">How are these connected?</span>
  <input id="from" placeholder="From (e.g. Ada Lovelace)">
  <span class="cbarrow">&rarr;</span>
  <input id="to" placeholder="To (e.g. Charles Babbage)">
  <button id="cbtn" onclick="connect()">Connect</button>
</div>
<main>
  <div id="results"><div class="hint">Type a query and press Enter.</div></div>
  <div id="article"><div class="hint">Select an article.</div></div>
  <div id="related"></div>
</main>
<script>
const $ = s => document.querySelector(s);
async function j(u){ const r = await fetch(u); return r.json(); }
function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
// score is cosine similarity (0..1, higher = more related).
function bucketWord(s){
  if(s>=0.85) return 'near-identical';
  if(s>=0.75) return 'very related';
  if(s>=0.60) return 'related';
  return 'loosely related';
}
function relInd(s){
  const pct = Math.max(0, Math.min(100, Math.round(s*100)));
  return '<div class="relbarwrap" title="embedding cosine similarity '+s.toFixed(2)+
    ' (higher = more related)">'+
    '<div class="relbar"><div class="relfill" style="width:'+pct+'%"></div></div>'+
    '<span class="bucket">'+bucketWord(s)+'</span>'+
    '<span class="num">'+s.toFixed(2)+'</span></div>';
}
const RELFOOT = '<div class="foot">&ldquo;Related (fused)&rdquo; blends both signals '+
  '(reciprocal-rank fusion): &ldquo;Related by meaning&rdquo; uses AI embeddings; '+
  '&ldquo;Linked articles&rdquo; uses Wikipedia&rsquo;s own hyperlinks. An item that '+
  'is both semantically near and directly linked ranks highest.</div>';
function provTag(p){
  const cls = p.indexOf('meaning + linked')===0 ? 'pv-both'
    : (p.indexOf('meaning')===0 ? 'pv-mean' : 'pv-link');
  return ' <span class="pv '+cls+'">'+esc(p)+'</span>';
}
function fusedRow(a){
  let ind = (a.cos!=null)
    ? relInd(a.cos)
    : (a.cocite ? '<div class="relbarwrap" title="reached via '+a.cocite+
        ' of this page&rsquo;s own links (co-citation)"><span class="bucket">co-cited &times;'+
        a.cocite+'</span></div>' : '');
  return '<div class="item" onclick="open_('+a.id+')"><div class="rtitle">'+
    esc(a.title)+provTag(a.prov)+'</div>'+ind+'</div>';
}

async function search(){
  const q = $('#q').value.trim();
  if(!q) return;
  const items = await j('/search?q='+encodeURIComponent(q));
  const el = $('#results');
  if(!items.length){ el.innerHTML = '<div class="hint">No title matches.</div>'; return; }
  el.innerHTML = '<div class="secttl">'+items.length+' results</div>' +
    items.map(a => `<div class="item" onclick="open_(${a.id})">${esc(a.title)}</div>`).join('');
}
async function open_(id){
  const a = await j('/article/'+id);
  const art = $('#article');
  if(!a){ art.innerHTML = '<div class="hint">Not found.</div>'; return; }
  const paras = a.body.split(/\\n\\n+/).map(p => '<p>'+esc(p).replace(/\\n/g,'<br>')+'</p>').join('');
  art.innerHTML = '<h2>'+esc(a.title)+'</h2>'+paras;
  art.scrollTop = 0;
  loadRelated(id);
}
async function loadRelated(id){
  const rel = $('#related');
  rel.innerHTML = '<div class="hd">Loading…</div>';
  const r = await j('/related_fused/'+id);
  let h = '<div class="hd">Related (fused)</div>' +
    '<div class="legend">meaning &times; links combined — both signals = strongest</div>';
  h += r.fused.length
    ? r.fused.map(fusedRow).join('')
    : '<div class="hint">none</div>';
  h += '<div class="hd">Related by meaning</div>' +
    '<div class="legend">how closely the topics match (embedding similarity)</div>';
  h += r.semantic.length
    ? r.semantic.map(a => `<div class="item" onclick="open_(${a.id})"><div class="rtitle">${esc(a.title)}</div>${relInd(a.score)}</div>`).join('')
    : '<div class="hint">none</div>';
  h += '<div class="hd">Linked articles</div>' +
    '<div class="legend">articles this page links to</div>';
  h += r.hyperlinks.length
    ? r.hyperlinks.map(a => `<div class="item" onclick="open_(${a.id})">${esc(a.title)}</div>`).join('')
    : '<div class="hint">none</div>';
  h += RELFOOT;
  rel.innerHTML = h;
}
let _lastPath = null;
function renderConnection(r){
  let h = '<h2>How are these connected?</h2>';
  if(!r.found){
    return h + '<div class="hint">' + esc(r.reason || 'no path found') + '</div>';
  }
  h += '<div class="subtle">' + r.hops + ' hop' + (r.hops===1?'':'s') +
    ' &middot; shortest undirected hyperlink path</div>';
  h += '<div class="chain">' + r.path.map((n,i) =>
    (i ? '<span class="arr">&rarr;</span>' : '') +
    '<span class="chip" onclick="open_(' + n.id + ')">' + esc(n.title) + '</span>'
  ).join('') + '</div>';
  h += '<div style="margin-top:14px"><button class="lbtn" onclick="narrate()">'+
    'Explain this connection</button> <span id="narr"></span></div>';
  return h;
}
async function connect(){
  const f = $('#from').value.trim(), t = $('#to').value.trim();
  if(!f || !t) return;
  const art = $('#article');
  art.innerHTML = '<div class="hint">Finding the shortest link path…</div>';
  let r;
  try { r = await j('/path?from='+encodeURIComponent(f)+'&to='+encodeURIComponent(t)); }
  catch(e){ art.innerHTML = '<div class="hint">Connect failed: '+esc(String(e))+'</div>'; return; }
  _lastPath = {f, t};
  art.innerHTML = renderConnection(r);
  art.scrollTop = 0;
}
async function narrate(){
  if(!_lastPath) return;
  const nd = $('#narr');
  nd.innerHTML = '<span class="hint">thinking (local LLM)…</span>';
  let r;
  try { r = await j('/path?from='+encodeURIComponent(_lastPath.f)+
    '&to='+encodeURIComponent(_lastPath.t)+'&narrate=1'); }
  catch(e){ nd.innerHTML = '<span class="hint">narration failed</span>'; return; }
  nd.innerHTML = r.narration
    ? '<div class="answer" style="margin-top:10px">'+esc(r.narration)+'</div>'
    : '<span class="hint">no narration</span>';
}
async function ask(){
  const q = $('#ask').value.trim();
  if(!q) return;
  const art = $('#article');
  art.innerHTML = '<div class="hint">Thinking — retrieving passages + local LLM…</div>';
  let r;
  try { r = await j('/ask?q='+encodeURIComponent(q)); }
  catch(e){ art.innerHTML = '<div class="hint">Ask failed: '+esc(String(e))+'</div>'; return; }
  let h = '<h2>Ask</h2><div class="answer">'+esc(r.answer)+'</div>';
  if(r.sources && r.sources.length){
    h += '<div class="hd">Sources (click to open)</div>' +
      '<div class="legend">how closely each source matches your question (embedding similarity)</div>';
    h += r.sources.map(s => `<div class="item" onclick="open_(${s.id})"><div class="rtitle">[${s.n}] ${esc(s.title)}</div>${relInd(s.score)}</div>`).join('');
  } else {
    h += '<div class="hint">No sources retrieved.</div>';
  }
  art.innerHTML = h;
  art.scrollTop = 0;
}
$('#q').addEventListener('keydown', e => { if(e.key==='Enter') search(); });
$('#ask').addEventListener('keydown', e => { if(e.key==='Enter') ask(); });
$('#from').addEventListener('keydown', e => { if(e.key==='Enter') connect(); });
$('#to').addEventListener('keydown', e => { if(e.key==='Enter') connect(); });
</script>
</body></html>"""


def make_handler(reader: Reader):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(200, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            try:
                if path == "/":
                    self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/search":
                    q = parse_qs(u.query).get("q", [""])[0]
                    self._json(reader.search(q))
                elif path == "/ask":
                    q = parse_qs(u.query).get("q", [""])[0]
                    self._json(reader.ask(q))
                elif path.startswith("/article/"):
                    aid = int(unquote(path.split("/")[-1]))
                    art = reader.article(aid)
                    if art is None:
                        self._json({"error": "not found"})
                    else:
                        self._json(art)
                elif path.startswith("/related/"):
                    aid = int(path.split("/")[-1])
                    self._json(
                        {"semantic": reader.semantic(aid), "hyperlinks": reader.hyperlinks(aid)}
                    )
                elif path.startswith("/related_fused/"):
                    aid = int(path.split("/")[-1])
                    self._json(reader.related_fused(aid))
                elif path == "/path":
                    qs = parse_qs(u.query)
                    frm = reader.resolve(qs.get("from", [""])[0])
                    to = reader.resolve(qs.get("to", [""])[0])
                    if frm is None or to is None:
                        self._json({
                            "found": False,
                            "reason": "could not resolve one or both articles",
                            "from": frm, "to": to,
                        })
                    else:
                        res = reader.path(frm["id"], to["id"])
                        if res.get("found") and qs.get("narrate", ["0"])[0] == "1":
                            res["narration"] = reader.narrate_path(res["path"])
                        self._json(res)
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # never take the server down on one bad request
                self._send(500, html.escape(repr(e)).encode(), "text/plain")

    return Handler


def cmd_serve(corpus: Path, host: str, port: int) -> None:
    reader = Reader(corpus)
    httpd = ThreadingHTTPServer((host, port), make_handler(reader))
    print(f"[serve] listening on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=Path("data/wiki/enwiki"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="build reader.db + sidecars")
    sub.add_parser(
        "build-undirected",
        help="build ONLY the undirected CSR from the existing directed CSR (fast)",
    )
    sp = sub.add_parser("serve", help="serve the reader")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)

    if not (args.corpus / "manifest.json").exists():
        ap.error(f"no manifest.json under {args.corpus}")

    if args.cmd == "build":
        cmd_build(args.corpus)
    elif args.cmd == "build-undirected":
        t0 = time.time()
        build_undirected_csr(args.corpus)
        print(f"[build] undirected CSR done in {time.time() - t0:.1f}s")
    else:
        cmd_serve(args.corpus, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

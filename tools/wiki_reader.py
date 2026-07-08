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
import random
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

# --- Enrich (on-demand, reviewed, CITED article enrichment) ----------------- #
# Proposes facts the current article LACKS, each grounded in and citing a specific
# related article, which the maintainer accepts into a persistent OVERLAY. The
# overlay lives OUTSIDE reader.db (its own DB file next to the corpus dir) so a
# future reader.db rebuild from the planned clean re-extract does NOT wipe the
# maintainer's curated facts. Anti-hallucination gate: every proposed fact carries
# a verbatim snippet from the cited source and is dropped unless that snippet is
# actually found in the source text we fed the model.
ENRICH_OVERLAY_NAME = "enrich_overlay.db"  # sits at <corpus>/../enrich_overlay.db
ENRICH_MAX_SOURCES = 10  # cap on candidate source articles fed to the LLM
ENRICH_SOURCE_CHARS = 900  # per-source text budget (lead + a little body)
ENRICH_TARGET_CHARS = 2000  # target-article text shown to the model (dedupe context)
ENRICH_SECTION_MIN = 2  # a section must appear in >= N same-type neighbours to report
ENRICH_SNIPPET_MIN = 12  # min verbatim-snippet length the grounding gate accepts

# --- Inline auto-linking config --------------------------------------------- #
# Comprehensive linking is the DEFAULT: every word/phrase that resolves to an
# article is linked. This is safe because links are styled INVISIBLE (identical to
# body text, affordance on :hover only) — so density is not visually disruptive.
# Precedence: a phrase that is one of the page's REAL out-edge targets (Layer A) is
# linked to that precise/disambiguated target; every other matchable phrase links to
# the best global title match (highest inbound-degree article for that surface form).
# Longest-match wins (multi-word entities beat their component words). We skip only a
# tiny set of pure function words — the goal is maximal coverage.
LINK_TARGET_CAP = 4000  # max out-edge targets considered for Layer A per article
MAX_PHRASE_WORDS = 6  # longest multi-word entity attempted per position
_SKIP_WORDS = frozenset("the of a an and to is in on for as".split())

_WORD_RE = re.compile(r"[0-9A-Za-z]+")


def _norm_ws(s: str) -> str:
    """Collapse internal whitespace to single spaces (title/alias normal form)."""
    return " ".join(s.split())


def _is_heading(s: str) -> bool:
    """Heuristic: is this stripped line a section heading recoverable from plain text?

    Section titles survive extraction as short, standalone, title-cased lines with no
    terminal sentence punctuation (e.g. "Biography", "Post-training quantization").
    Conservative on purpose — a comma or trailing '.'/':' or >6 words means prose or a
    caption, not a heading, so we don't mangle body text."""
    if not (2 <= len(s) <= 50):
        return False
    if not (s[0].isalpha() and s[0].isupper()):
        return False
    if s[-1] in ".,:;!?" or "," in s:
        return False
    return len(s.split()) <= 6


def _is_bullet(s: str) -> bool:
    """A surviving bullet list item (rare — most list markup was stripped). Only
    unambiguous '*'/'•' markers count; '-' is left as prose to avoid false positives."""
    return bool(re.match(r"^[\*•]\s+\S", s))


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


def _category_files(corpus: Path) -> list[str]:
    manifest = json.loads((corpus / "manifest.json").read_text())
    return [s["path"] for s in manifest["shards"]["categories"]["files"]]


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


def build_redirects(conn: sqlite3.Connection, corpus: Path) -> int:
    """Index redirect aliases → target article id, for extra Layer-A surface forms.

    redirects.tsv is `alias_title \\t canonical_title` (both title strings). For inline
    linking we want, per real article, the alternate PROSE names that redirect to it
    (e.g. "Autism spectrum" → Autism, "Al Gore" ← "Albert Gore"). We keep only
    prose-like aliases (contain a space, no '/', bounded length) and resolve the
    canonical title to its article id via the already-built `articles` table, storing
    (target_id, alias) rows indexed by target_id. Serve-time Layer A then pulls only
    the aliases whose target is an out-edge of the current page — precise, no overlink.

    Junk aliases (camelCase link tokens like "AccessibleComputing", slashed subpage
    redirects) are dropped: they never appear in running prose so they add only noise."""
    path = corpus / "redirects.tsv"
    if not path.exists():
        print("[build] redirects.tsv absent — skipping redirect alias index")
        return 0
    # title -> id from the articles table (canonical titles are unique-ish)
    print("[build] loading title->id map for redirect resolution ...")
    title2id: dict[str, int] = {}
    for aid, title in conn.execute("SELECT id, title FROM articles"):
        title2id[title] = aid
    print(f"[build] {len(title2id):,} titles loaded")

    conn.execute("DROP TABLE IF EXISTS redir")
    conn.execute("CREATE TABLE redir (target_id INTEGER, alias TEXT)")
    n = 0
    kept = 0
    batch: list[tuple] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n += 1
            tab = line.find("\t")
            if tab < 0:
                continue
            alias = line[:tab]
            canon = line[tab + 1 :].rstrip("\n")
            # prose-like filter: multi-word, no subpage slash, bounded length
            if " " not in alias or "/" in alias or len(alias) > 60 or len(alias) < 3:
                continue
            tid = title2id.get(canon)
            if tid is None:
                continue
            batch.append((tid, _norm_ws(alias)))
            kept += 1
            if len(batch) >= 50000:
                conn.executemany("INSERT INTO redir VALUES (?,?)", batch)
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO redir VALUES (?,?)", batch)
    conn.execute("CREATE INDEX idx_redir_target ON redir(target_id)")
    conn.commit()
    print(f"[build] redirects: scanned {n:,}, kept {kept:,} prose aliases")
    return kept


def cmd_build_redirects(corpus: Path) -> None:
    t0 = time.time()
    conn = sqlite3.connect(corpus / DB_NAME)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    build_redirects(conn, corpus)
    conn.close()
    print(f"[build] redirect alias index done in {time.time() - t0:.1f}s")


def build_categories(conn: sqlite3.Connection, corpus: Path) -> int:
    """Index (article_id, category) into a `cats` table indexed by article_id.

    This is the RELATIONAL leg of filtered semantic search: after a cosine top-N
    the category filter is a bounded index join (article_id IN <candidates> AND
    category LIKE ...). Reads the already-extracted categories-*.tsv sidecars
    (`article_id \\t category`) — no corpus re-scan. Only the article_id index is
    built: we always constrain by candidate id first, so a category LIKE over the
    handful of survivors is cheap and needs no per-category index."""
    conn.execute("DROP TABLE IF EXISTS cats")
    conn.execute("CREATE TABLE cats (article_id INTEGER, category TEXT)")
    n = 0
    batch: list[tuple] = []
    for name in _category_files(corpus):
        path = corpus / name
        if not path.exists() or path.stat().st_size == 0:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                tab = line.find("\t")
                if tab < 0:
                    continue
                try:
                    aid = int(line[:tab])
                except ValueError:
                    continue
                cat = line[tab + 1 :].rstrip("\n")
                if not cat:
                    continue
                batch.append((aid, cat))
                if len(batch) >= 100000:
                    conn.executemany("INSERT INTO cats VALUES (?,?)", batch)
                    n += len(batch)
                    batch.clear()
        print(f"[build] categories: {name} done, {n + len(batch)} rows so far")
    if batch:
        conn.executemany("INSERT INTO cats VALUES (?,?)", batch)
        n += len(batch)
    print(f"[build] categories: {n:,} rows inserted, building index on article_id ...")
    conn.execute("CREATE INDEX idx_cats_article ON cats(article_id)")
    conn.commit()
    return n


def cmd_build_categories(corpus: Path) -> None:
    t0 = time.time()
    conn = sqlite3.connect(corpus / DB_NAME)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    n = build_categories(conn, corpus)
    conn.close()
    print(f"[build] category index: {n:,} rows in {time.time() - t0:.1f}s")


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

    print("[build] building redirect alias index (Layer-A surface forms)...")
    conn2 = sqlite3.connect(db_path)
    conn2.execute("PRAGMA journal_mode=OFF")
    conn2.execute("PRAGMA synchronous=OFF")
    build_redirects(conn2, corpus)
    conn2.close()

    print("[build] building category index (relational filter leg)...")
    conn3 = sqlite3.connect(db_path)
    conn3.execute("PRAGMA journal_mode=OFF")
    conn3.execute("PRAGMA synchronous=OFF")
    build_categories(conn3, corpus)
    conn3.close()

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

        # Inline auto-linking. Redirect aliases (Layer A extra surface forms) are
        # queried on demand from the optional `redir` table. The Layer-B notable
        # matcher is built lazily on the first "denser links" request so normal
        # startup stays ~49s.
        with self.db_lock:
            self._has_redir = bool(
                self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='redir'"
                ).fetchone()
            )
        print(f"[serve] redirect alias index: {'present' if self._has_redir else 'absent'}")
        with self.db_lock:
            self._has_cats = bool(
                self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='cats'"
                ).fetchone()
            )
        print(f"[serve] category index: {'present' if self._has_cats else 'absent'}")
        self._global: dict[str, int] | None = None
        self._global_lock = threading.Lock()
        # Inbound-degree per article id — the importance signal shared by the link
        # matcher and filtered semantic search. Built at most once (lazy bincount).
        self._indeg: np.ndarray | None = None
        self._indeg_lock = threading.Lock()
        # Max article id (sparse PK), cached for the random-article landing page.
        self._max_id: int | None = None

        # Enrichment overlay: accepted cited facts, in a SEPARATE DB file (not
        # reader.db) so a reader.db rebuild from a future clean re-extract preserves
        # the maintainer's curation. Keyed by article (subject) id.
        self.overlay_path = corpus.parent / ENRICH_OVERLAY_NAME
        self.overlay = sqlite3.connect(self.overlay_path, check_same_thread=False)
        self.overlay_lock = threading.Lock()
        self._init_overlay()
        print(f"[serve] enrichment overlay: {self.overlay_path}")
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

    def random_article(self) -> dict | None:
        """A random existing article via the PK index — O(log n), no full scan.

        Article ids are sparse (gaps from clobbered shards), so we pick a random id
        in [0, max_id] and take the next existing row (`WHERE id >= r ORDER BY id
        LIMIT 1`, using the PK B-tree). If the pick lands past the last id we wrap to
        the first row. Every call returns a fresh pick — the home page never caches."""
        with self.db_lock:
            if self._max_id is None:
                row = self.db.execute("SELECT MAX(id) FROM articles").fetchone()
                self._max_id = int(row[0]) if row and row[0] is not None else 0
        r = random.randint(0, self._max_id)
        with self.db_lock:
            row = self.db.execute(
                "SELECT id, title FROM articles WHERE id >= ? ORDER BY id LIMIT 1", (r,)
            ).fetchone()
            if row is None:  # r fell past the last id — wrap to the first article
                row = self.db.execute(
                    "SELECT id, title FROM articles ORDER BY id LIMIT 1"
                ).fetchone()
        return {"id": int(row[0]), "title": row[1]} if row else None

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

    # -- Inline auto-linking (Layer A: real links; Layer B: notable terms) -- #
    def _titles_chunked(self, ids: list[int]) -> dict[int, str]:
        """_titles for arbitrarily many ids (chunked under SQLite's param limit)."""
        out: dict[int, str] = {}
        for i in range(0, len(ids), 900):
            out.update(self._titles(ids[i : i + 900]))
        return out

    def _out_target_titles(self, aid: int) -> dict[int, str]:
        """Directed out-edge targets of `aid` as {id: canonical_title} (capped)."""
        if aid + 1 >= len(self.csr_off):
            return {}
        lo, hi = int(self.csr_off[aid]), int(self.csr_off[aid + 1])
        dst_ids = [int(x) for x in self.csr_dst[lo:hi][:LINK_TARGET_CAP]]
        return self._titles_chunked(dst_ids)

    def _aliases_for(self, ids: list[int]) -> list[tuple[int, str]]:
        """Redirect aliases (prose surface forms) for the given target ids."""
        if not self._has_redir or not ids:
            return []
        rows: list[tuple[int, str]] = []
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            qm = ",".join("?" * len(chunk))
            with self.db_lock:
                rows.extend(
                    self.db.execute(
                        f"SELECT target_id, alias FROM redir WHERE target_id IN ({qm})",
                        chunk,
                    ).fetchall()
                )
        return rows

    def _ensure_indeg(self) -> np.ndarray:
        """Inbound-degree per article id (bincount over the CSR dst values), cached.

        Shared by the full-corpus link matcher (notability tie-break) and filtered
        semantic search's importance filter, so the ~7M-bin bincount runs once."""
        if self._indeg is not None:
            return self._indeg
        with self._indeg_lock:
            if self._indeg is None:
                self._indeg = np.bincount(self.csr_dst.astype(np.int64, copy=False))
        return self._indeg

    def _ensure_global(self) -> dict[str, int]:
        """Lazily build the FULL comprehensive matcher: {lowercased title surface ->
        best article id}. Every title in the 6.9M corpus enters (only the tiny
        _SKIP_WORDS function words are dropped). On a lowercase collision the higher
        inbound-degree article wins (notability tie-break). Footprint on the 128 GB
        box: ~6.9M keys, ~8 s build, ~2 GB resident — built once, then cached."""
        if self._global is not None:
            return self._global
        with self._global_lock:
            if self._global is not None:
                return self._global
            t0 = time.time()
            print("[serve] building full-corpus link matcher (first article view)...")
            indeg = self._ensure_indeg()
            n_ids = len(indeg)

            def deg(i: int) -> int:
                return int(indeg[i]) if i < n_ids else 0

            gmap: dict[str, int] = {}
            with self.db_lock:
                for aid, title in self.db.execute("SELECT id, title FROM articles"):
                    key = _norm_ws(title).lower()
                    if not key or key in _SKIP_WORDS:
                        continue
                    cur = gmap.get(key)
                    if cur is None or deg(aid) > deg(cur):
                        gmap[key] = aid
            self._global = gmap
            print(
                f"[serve] full link matcher: {len(gmap):,} surface forms "
                f"in {time.time() - t0:.1f}s"
            )
        return self._global

    def _linkify(self, frag: str, a_surf: dict[str, int], a_rx, aid: int) -> str:
        """Escape a plain-text fragment and insert inline <a class="wl"> links.

        Precedence: Layer A (the page's real out-edge targets, matched by the `a_rx`
        regex over canonical titles + redirect aliases — handles punctuation and
        disambiguates) is applied FIRST and wins. Every remaining word/phrase is then
        matched against the full-corpus matcher by longest-first token n-gram. Link
        markup is only ever inserted around html.escape()'d spans → injection-safe."""
        gmap = self._ensure_global()
        spans: list[tuple[int, int, int]] = []
        if a_rx is not None:
            for m in a_rx.finditer(frag):
                tid = a_surf.get(_norm_ws(m.group(0)).lower())
                if tid is not None:
                    spans.append((m.start(), m.end(), tid))
        toks = list(_WORD_RE.finditer(frag))
        lower = [t.group(0).lower() for t in toks]
        # mark tokens already covered by a Layer-A span (two-pointer, both sorted)
        spans.sort()
        cov = [False] * len(toks)
        si = 0
        for j, t in enumerate(toks):
            ts = t.start()
            while si < len(spans) and spans[si][1] <= ts:
                si += 1
            if si < len(spans) and spans[si][0] <= ts < spans[si][1]:
                cov[j] = True
        # global longest-match scan over uncovered tokens
        i, N = 0, len(toks)
        while i < N:
            if cov[i]:
                i += 1
                continue
            hit = 0
            for n in range(min(MAX_PHRASE_WORDS, N - i), 0, -1):
                if any(cov[i + j] for j in range(n)):
                    continue
                phrase = " ".join(lower[i : i + n])
                if n == 1 and (len(phrase) < 2 or phrase in _SKIP_WORDS):
                    continue  # skip bare function words + 1-char tokens (e.g. the "s" in "Babbage's")
                tid = gmap.get(phrase)
                if tid is None or tid == aid:
                    continue
                spans.append((toks[i].start(), toks[i + n - 1].end(), tid))
                hit = n
                break
            i += hit if hit else 1
        # render
        spans.sort()
        out: list[str] = []
        pos = 0
        for s, e, tid in spans:
            if s < pos:
                continue
            out.append(html.escape(frag[pos:s]))
            out.append(f'<a href="/article/{tid}" class="wl" data-id="{tid}">')
            out.append(html.escape(frag[s:e]))
            out.append("</a>")
            pos = e
        out.append(html.escape(frag[pos:]))
        return "".join(out)

    def link_body(self, aid: int, body: str) -> str:
        """Render the plain-text body to safe HTML: comprehensive inline links plus
        the typographic structure recoverable from plain text (section headings,
        paragraph spacing, and the rare surviving bullet list). Rich Wikipedia
        formatting — infoboxes, tables, bold/italic, images, citations — is NOT
        recoverable here: the extractor discarded all markup (see the doc)."""
        # -- Layer A surface map + regex (out-edge targets + redirect aliases) -
        targets = self._out_target_titles(aid)
        a_surf: dict[str, int] = {}
        for tid, title in targets.items():
            key = _norm_ws(title).lower()
            if len(key) >= 2:
                a_surf.setdefault(key, tid)
        for tid, alias in self._aliases_for(list(targets)):
            key = _norm_ws(alias).lower()
            if len(key) >= 3:
                a_surf.setdefault(key, tid)
        a_rx = None
        if a_surf:
            alt = "|".join(re.escape(k) for k in sorted(a_surf, key=len, reverse=True))
            a_rx = re.compile(
                r"(?<![0-9A-Za-z])(?:" + alt + r")(?![0-9A-Za-z])", re.IGNORECASE
            )

        def link(frag: str) -> str:
            return self._linkify(frag, a_surf, a_rx, aid)

        # -- structure: headings / paragraphs / bullet lists ------------------
        parts: list[str] = []
        for block in re.split(r"\n{2,}", body):
            lines = block.split("\n")
            buf: list[str] = []

            def flush() -> None:
                real = [ln for ln in buf if ln.strip()]
                if real:
                    parts.append("<p>" + "<br>".join(link(ln) for ln in real) + "</p>")
                buf.clear()

            i = 0
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    i += 1
                    continue
                if _is_heading(s):
                    flush()
                    parts.append('<h3 class="wsec">' + link(s) + "</h3>")
                    i += 1
                elif _is_bullet(s):
                    j = i
                    items: list[str] = []
                    while j < len(lines) and _is_bullet(lines[j].strip()):
                        items.append(re.sub(r"^[\*•]\s+", "", lines[j].strip()))
                        j += 1
                    if len(items) >= 2:  # conservative: only a real run becomes a list
                        flush()
                        parts.append(
                            "<ul>" + "".join(f"<li>{link(it)}</li>" for it in items) + "</ul>"
                        )
                        i = j
                    else:
                        buf.append(lines[i])
                        i += 1
                else:
                    buf.append(lines[i])
                    i += 1
            flush()
        return "".join(parts)

    def summary(self, aid: int) -> dict | None:
        """Lead of an article for hovercards: title + first paragraph (1–2 sentences)."""
        art = self.article(aid)
        if art is None:
            return None
        lead = ""
        for para in re.split(r"\n\n+", art["body"]):
            para = para.strip()
            # skip a bare section-heading-ish first line; take the first real prose
            if len(para) >= 40:
                lead = para
                break
        if not lead:
            lead = art["body"].strip()[:300]
        # first ~2 sentences, hard-capped
        sents = re.split(r"(?<=[.!?])\s+", lead)
        lead = " ".join(sents[:2]).strip()
        if len(lead) > 320:
            lead = lead[:317].rstrip() + "…"
        return {"id": aid, "title": art["title"], "lead": lead}

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

    def _embed_query(self, q: str) -> np.ndarray:
        """Encode a query with the SAME BGE model as the corpus and L2-normalise it,
        so a dot product against a normalised corpus vector is cosine similarity."""
        emb = self._get_embedder()
        v = emb.encode([q])
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
        return np.ascontiguousarray(v, dtype=np.float32)

    def _knn_passages(
        self, qvec: np.ndarray, k: int, exclude: set[int] | None = None
    ) -> list[dict]:
        """Top-k passages for an already-embedded query over the CAGRA index.
        Missing articles (the ~4% clobbered) are skipped, so we over-fetch."""
        exclude = exclude or set()
        with self.index_lock:
            labels, dists = self.index.knn_query(qvec, k=k + 4 + len(exclude))
        passages: list[dict] = []
        for lab, d in zip(labels[0], dists[0]):
            aid = int(lab)
            if aid in exclude:
                continue
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

    def retrieve(self, q: str, k: int = ASK_K) -> list[dict]:
        """Embed the question, then pull the top-k passages from the CAGRA index."""
        return self._knn_passages(self._embed_query(q), k)

    # -- Filtered semantic search (vector similarity + relational filter) --- #
    def _filter_by_category(self, ids: list[int], text: str) -> set[int]:
        """Subset of `ids` whose article has a category LIKE %text% (case-insens).

        Constrained by the (indexed) candidate ids first, so the LIKE only scans the
        handful of category rows belonging to the top-N — no full-table scan."""
        if not ids or not self._has_cats or not text:
            return set()
        keep: set[int] = set()
        like = f"%{text.lower()}%"
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            qm = ",".join("?" * len(chunk))
            with self.db_lock:
                rows = self.db.execute(
                    f"SELECT DISTINCT article_id FROM cats WHERE article_id IN ({qm}) "
                    "AND lower(category) LIKE ?",
                    (*chunk, like),
                ).fetchall()
            keep.update(int(r[0]) for r in rows)
        return keep

    def _cats_for(self, ids: list[int], per: int = 3) -> dict[int, list[str]]:
        """Up to `per` category names per id, to annotate results with their tags."""
        if not ids or not self._has_cats:
            return {}
        out: dict[int, list[str]] = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            qm = ",".join("?" * len(chunk))
            with self.db_lock:
                rows = self.db.execute(
                    f"SELECT article_id, category FROM cats WHERE article_id IN ({qm})",
                    chunk,
                ).fetchall()
            for aid, cat in rows:
                lst = out.setdefault(int(aid), [])
                if len(lst) < per:
                    lst.append(cat)
        return out

    def search_semantic(
        self,
        q: str,
        pool: int = 150,
        min_indeg: int = 0,
        min_len: int = 0,
        max_len: int = 0,
        cat: str = "",
    ) -> dict:
        """TriDB's thesis as a personal tool: retrieve the `pool` nearest articles by
        MEANING (cuVS cosine), THEN apply RELATIONAL filters (inbound-degree /
        importance, body length, category) and return the surviving ranked list with
        pre/post-filter counts. Semantic first, filter second — never the reverse."""
        q = (q or "").strip()
        base = {
            "query": q,
            "pool": pool,
            "cats_available": self._has_cats,
            "filters": {
                "min_indeg": min_indeg,
                "min_len": min_len,
                "max_len": max_len,
                "category": cat,
            },
        }
        if not q:
            return {**base, "pre_count": 0, "post_count": 0, "results": []}
        qvec = self._embed_query(q)
        with self.index_lock:
            labels, dists = self.index.knn_query(qvec, k=pool)
        cand: list[tuple[int, float]] = []
        seen: set[int] = set()
        for lab, d in zip(labels[0], dists[0]):
            aid = int(lab)
            if aid in seen:
                continue
            seen.add(aid)
            cand.append((aid, round(1.0 - float(d), 4)))
        titles = self._titles([a for a, _ in cand])
        cand = [(a, s) for a, s in cand if a in titles]  # drop clobbered/missing
        pre_count = len(cand)

        indeg = self._ensure_indeg()
        n_indeg = len(indeg)

        def deg(a: int) -> int:
            return int(indeg[a]) if a < n_indeg else 0

        # cheap relational filters first (importance, category) — no body read
        if min_indeg > 0:
            cand = [(a, s) for a, s in cand if deg(a) >= min_indeg]
        if cat and self._has_cats:
            keep = self._filter_by_category([a for a, _ in cand], cat)
            cand = [(a, s) for a, s in cand if a in keep]

        # length filter needs a body read — only for the survivors of the above
        lengths: dict[int, int] = {}
        if min_len > 0 or max_len > 0:
            filtered: list[tuple[int, float]] = []
            for a, s in cand:
                art = self.article(a)
                if art is None:
                    continue
                length = len(art["body"])
                if min_len and length < min_len:
                    continue
                if max_len and length > max_len:
                    continue
                lengths[a] = length
                filtered.append((a, s))
            cand = filtered

        ids = [a for a, _ in cand]
        cats_by = self._cats_for(ids[:40]) if self._has_cats else {}
        results = [
            {
                "id": a,
                "title": titles[a],
                "score": s,
                "indeg": deg(a),
                "length": lengths.get(a),
                "cats": cats_by.get(a, []),
            }
            for a, s in cand
        ]
        return {**base, "pre_count": pre_count, "post_count": len(cand), "results": results}

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

    def _score_by_query(self, qvec: np.ndarray, ids: list[int]) -> dict[int, float]:
        """Cosine similarity of each id's corpus vector to the (unit) query vector,
        for ids that have an embedding row. Batched numpy — cheap for a few hundred."""
        rows: list[int] = []
        valid: list[int] = []
        for a in ids:
            if 0 <= a < len(self.id2row):
                r = int(self.id2row[a])
                if r >= 0:
                    rows.append(r)
                    valid.append(a)
        if not rows:
            return {}
        mat = np.ascontiguousarray(self.vecs[rows], dtype=np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
        sims = mat @ qvec[0]
        return {a: float(sims[i]) for i, a in enumerate(valid)}

    def _expand_graph(
        self,
        qvec: np.ndarray,
        seed_ids: list[int],
        hops: int = 1,
        cap_total: int = 6,
        per_seed: int = 60,
    ) -> list[tuple[int, float, int]]:
        """1- (optionally 2-) hop out-neighbours of the seeds from the directed CSR,
        ranked by cosine to the query and returned most-relevant first. Grounds the
        answer in the hyperlink CHAIN, not just articles near the question.

        Returns [(aid, cosine_to_query, via_seed_id)] — `via` is the ORIGINAL seed
        each neighbour was reached from, so a 2-hop pick still cites a real seed."""
        seen = set(seed_ids)
        cand: dict[int, int] = {}  # neighbour id -> originating seed id
        via = {s: s for s in seed_ids}
        frontier = list(seed_ids)
        for _ in range(max(1, hops)):
            nxt: list[int] = []
            for node in frontier:
                if node + 1 >= len(self.csr_off):
                    continue
                lo, hi = int(self.csr_off[node]), int(self.csr_off[node + 1])
                for x in self.csr_dst[lo:hi][:per_seed].tolist():
                    if x in seen or x in cand:
                        continue
                    src_seed = via.get(node, node)
                    cand[x] = src_seed
                    via[x] = src_seed
                    nxt.append(x)
            frontier = nxt
            if not frontier:
                break
        if not cand:
            return []
        sims = self._score_by_query(qvec, list(cand))
        ranked = sorted(sims.items(), key=lambda kv: -kv[1])
        return [(a, round(s, 4), cand[a]) for a, s in ranked[:cap_total]]

    def ask(self, q: str, k: int = ASK_K, expand: bool = True, hops: int = 1) -> dict:
        """Graph-aware RAG. Semantic retrieval seeds the context; with `expand` on
        (default) the top seeds' hyperlink neighbours are ranked by relevance and the
        best are folded in (context capped ~12 passages) BEFORE the LLM answers, so
        multi-hop questions are grounded in the link chain. Citations always point at
        the real source article; each source is tagged semantic vs graph-expanded."""
        q = (q or "").strip()
        if not q:
            return {"answer": "Ask a question.", "sources": [], "expanded": False}
        qvec = self._embed_query(q)
        cap_total = 12
        k_seed = 6 if expand else k
        seeds = self._knn_passages(qvec, k_seed)
        if not seeds:
            return {
                "answer": "No matching Wikipedia articles were found for this question.",
                "sources": [],
                "expanded": bool(expand),
            }
        passages = [dict(p, origin="semantic", via=None) for p in seeds]
        if expand:
            seed_ids = [p["id"] for p in seeds]
            picks = self._expand_graph(
                qvec, seed_ids, hops=hops, cap_total=cap_total - len(passages)
            )
            via_titles = self._titles([v for _, _, v in picks])
            for aid, score, via in picks:
                art = self.article(aid)
                if art is None:
                    continue
                passages.append(
                    {
                        "id": aid,
                        "title": art["title"],
                        "score": score,
                        "body": art["body"][:PASSAGE_CHARS],
                        "origin": "graph",
                        "via": via_titles.get(via),
                    }
                )
        answer = self._llm_answer(q, passages)
        sources = [
            {
                "n": i + 1,
                "id": p["id"],
                "title": p["title"],
                "score": p["score"],
                "origin": p["origin"],
                "via": p.get("via"),
            }
            for i, p in enumerate(passages)
        ]
        return {
            "answer": answer,
            "sources": sources,
            "expanded": bool(expand),
            "n_semantic": sum(1 for p in passages if p["origin"] == "semantic"),
            "n_graph": sum(1 for p in passages if p["origin"] == "graph"),
        }

    # -- Enrich (on-demand, reviewed, CITED fact suggestions) -------------- #
    def _init_overlay(self) -> None:
        """Create the overlay schema on first serve. `facts` is the accepted-fact
        store; `dismissed` suppresses a suggestion the maintainer rejected so it
        does not resurface on the next /enrich of the same article."""
        with self.overlay_lock:
            self.overlay.execute(
                "CREATE TABLE IF NOT EXISTS facts ("
                "subject_id INTEGER, property TEXT, value TEXT, source_id INTEGER, "
                "source_title TEXT, source_snippet TEXT, accepted_ts REAL)"
            )
            self.overlay.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject_id)"
            )
            self.overlay.execute(
                "CREATE TABLE IF NOT EXISTS dismissed ("
                "subject_id INTEGER, property TEXT, value TEXT)"
            )
            self.overlay.commit()

    def _out_neighbor_ids(self, aid: int) -> list[int]:
        if aid + 1 >= len(self.csr_off):
            return []
        lo, hi = int(self.csr_off[aid]), int(self.csr_off[aid + 1])
        return [int(x) for x in self.csr_dst[lo:hi]]

    def _in_neighbor_ids(self, aid: int) -> list[int]:
        """Articles that link TO `aid` (they genuinely reference this exact subject).

        Reconstructed cheaply as (undirected neighbours) − (out neighbours): the
        undirected CSR is out∪in, so subtracting the out set leaves the pure
        in-links. Mutual links fall into the out set and are still covered there.
        Preferring these over mere semantic similarity is the entity-resolution
        guard against the 'Mercury the planet vs Mercury the element' trap — a page
        that links here is talking about THIS Mercury, a cosine neighbour may not."""
        if self.undir_off is None:
            return []
        out = set(self._out_neighbor_ids(aid))
        return [n for n in set(self._undir_neighbors(aid)) if n not in out and n != aid]

    def _section_headings(self, body: str) -> list[str]:
        """Ordered, de-duplicated section headings recoverable from a plain-text body
        (reuses the same conservative _is_heading heuristic as the reader render)."""
        seen: set[str] = set()
        out: list[str] = []
        for line in body.split("\n"):
            s = line.strip()
            if _is_heading(s):
                key = s.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(s)
        return out

    def _enrich_source_ids(self, aid: int, sem: list[dict]) -> list[int]:
        """Ordered candidate SOURCE ids for enriching `aid`, capped.

        Priority is deliberate (see _in_neighbor_ids): IN-neighbours first (they
        reference this exact subject), then OUT-neighbours, then cosine neighbours —
        so semantic drift never crowds out the genuinely-linked sources."""
        ordered: list[int] = []
        seen = {aid}
        for i in (
            self._in_neighbor_ids(aid)
            + self._out_neighbor_ids(aid)
            + [d["id"] for d in sem]
        ):
            if i in seen:
                continue
            seen.add(i)
            ordered.append(i)
            if len(ordered) >= ENRICH_MAX_SOURCES:
                break
        return ordered

    def _snippet_grounded(self, snippet: str, source_text: str) -> bool:
        """Anti-hallucination gate: the model's supporting snippet must be a real
        (whitespace-normalised, case-insensitive) span of the source text we fed it.
        A too-short or not-found snippet fails, and the fact is dropped."""
        a = _norm_ws(snippet).lower()
        return len(a) >= ENRICH_SNIPPET_MIN and a in _norm_ws(source_text).lower()

    def _enrich_extract(self, target: dict, sources: list[dict]) -> list[dict]:
        """Grounded LLM extraction (local ollama). Ask for NEW facts about the target
        subject that are stated in ONE specific source's provided text and NOT already
        in the target, each with a verbatim supporting snippet. Best-effort: any LLM
        or JSON failure returns [] (the endpoint still returns missing-sections)."""
        ctx = "\n\n".join(
            f"SOURCE id={s['id']} — {s['title']}\n{s['text']}" for s in sources
        )
        system = (
            "You enrich a TARGET Wikipedia article with NEW, SOURCED facts. You are "
            "given the target article text and several SOURCE articles that reference "
            "the target's subject. Propose facts about the TARGET SUBJECT that are "
            "(a) explicitly stated in ONE specific source's provided text, and (b) NOT "
            "already stated in the target text. For each fact you MUST copy a verbatim "
            "snippet CHARACTER-FOR-CHARACTER from that source's text that supports it. "
            "Never invent snippets and never use outside knowledge. Output ONLY JSON: "
            '{"facts":[{"property":"...","value":"...","source_id":<int>,'
            '"source_snippet":"<verbatim span from that source>"}]}. '
            'If there are none, return {"facts":[]}.'
        )
        user = (
            f"TARGET ARTICLE: {target['title']}\n{target['body'][:ENRICH_TARGET_CHARS]}\n\n"
            f"SOURCE ARTICLES (each may state facts about {target['title']}):\n\n{ctx}\n\n"
            f'Return JSON with NEW sourced facts about "{target["title"]}".'
        )
        payload = {
            "model": ASK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 800},
        }
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                content = json.loads(r.read())["message"]["content"]
            obj = json.loads(content)
        except Exception as e:
            print(f"[enrich] LLM extraction failed: {e!r}")
            return []
        facts = obj.get("facts") if isinstance(obj, dict) else obj
        if not isinstance(facts, list):
            return []
        return [f for f in facts if isinstance(f, dict)]

    def _enrich_missing_sections(
        self, aid: int, target: dict, sem: list[dict]
    ) -> list[dict]:
        """Sections that several same-type (semantic) neighbours have but the target
        lacks. 'Same-type' is approximated by cosine neighbours; a heading must recur
        in >= ENRICH_SECTION_MIN of them to be reported (suppresses one-off noise)."""
        from collections import defaultdict

        tgt = {h.lower() for h in self._section_headings(target["body"])}
        seen_in: dict[str, list[int]] = defaultdict(list)
        names: dict[str, str] = {}
        for d in sem:
            art = self.article(d["id"])
            if art is None:
                continue
            for h in self._section_headings(art["body"]):
                key = h.lower()
                if key in tgt:
                    continue
                seen_in[key].append(d["id"])
                names.setdefault(key, h)
        out = [
            {"type": "section", "name": names[k], "seen_in": ids[:6]}
            for k, ids in seen_in.items()
            if len(ids) >= ENRICH_SECTION_MIN
        ]
        out.sort(key=lambda s: -len(s["seen_in"]))
        return out[:8]

    def _dismissed_set(self, aid: int) -> set[tuple[str, str]]:
        with self.overlay_lock:
            rows = self.overlay.execute(
                "SELECT property, value FROM dismissed WHERE subject_id=?", (aid,)
            ).fetchall()
        return {(r[0].lower(), r[1].lower()) for r in rows}

    def overlay_facts(self, aid: int) -> list[dict]:
        """Already-accepted overlay facts for an article (rendered on the page)."""
        with self.overlay_lock:
            rows = self.overlay.execute(
                "SELECT property, value, source_id, source_title, source_snippet, "
                "accepted_ts FROM facts WHERE subject_id=? ORDER BY accepted_ts",
                (aid,),
            ).fetchall()
        return [
            {
                "property": r[0],
                "value": r[1],
                "source_id": r[2],
                "source_title": r[3],
                "source_snippet": r[4],
                "accepted_ts": r[5],
            }
            for r in rows
        ]

    def enrich(self, aid: int) -> dict:
        """On-demand cited enrichment for `aid`: gather linked/semantic sources, run a
        grounded LLM extraction, drop any fact whose snippet is not verbatim in its
        cited source (anti-hallucination gate) or already stated in the target, and
        return the reviewable suggestions + missing-section signals. No mutation —
        nothing is persisted until the maintainer POSTs /enrich/accept."""
        target = self.article(aid)
        if target is None:
            return {"error": "not found"}
        sem = self.semantic(aid, k=8)
        sources: list[dict] = []
        for i in self._enrich_source_ids(aid, sem):
            art = self.article(i)  # None if the source shard was clobbered
            if art is None:
                continue
            sources.append(
                {"id": i, "title": art["title"], "text": art["body"][:ENRICH_SOURCE_CHARS]}
            )
        raw = self._enrich_extract(target, sources) if sources else []
        by_src = {s["id"]: s for s in sources}
        tgt_lower = _norm_ws(target["body"]).lower()
        dismissed = self._dismissed_set(aid)
        accepted = {
            (f["property"].lower(), f["value"].lower()) for f in self.overlay_facts(aid)
        }
        suggestions: list[dict] = []
        dropped = 0
        for f in raw:
            prop = str(f.get("property") or "").strip()
            val = str(f.get("value") or "").strip()
            snip = str(f.get("source_snippet") or "").strip()
            try:
                sid = int(f.get("source_id"))
            except (TypeError, ValueError):
                dropped += 1
                continue
            src = by_src.get(sid)
            if src is None or not prop or not val or not snip:
                dropped += 1
                continue
            if not self._snippet_grounded(snip, src["text"]):
                dropped += 1  # snippet not verbatim in the cited source -> hallucinated
                continue
            if len(val) > 3 and _norm_ws(val).lower() in tgt_lower:
                dropped += 1  # already stated in the target article
                continue
            key = (prop.lower(), val.lower())
            if key in dismissed or key in accepted:
                continue
            suggestions.append(
                {
                    "property": prop,
                    "value": val,
                    "source_id": sid,
                    "source_title": src["title"],
                    "source_snippet": snip,
                }
            )
        return {
            "id": aid,
            "title": target["title"],
            "suggestions": suggestions,
            "missing_sections": self._enrich_missing_sections(aid, target, sem),
            "n_sources": len(sources),
            "n_dropped": dropped,
        }

    def accept_fact(self, d: dict) -> dict:
        """Persist an accepted suggestion to the overlay (idempotent per subject +
        property + value). Returns the refreshed accepted-fact list for the subject."""
        sid = int(d["subject_id"])
        prop = str(d.get("property", ""))[:300]
        val = str(d.get("value", ""))
        try:
            source_id = int(d.get("source_id"))
        except (TypeError, ValueError):
            source_id = -1
        with self.overlay_lock:
            exists = self.overlay.execute(
                "SELECT 1 FROM facts WHERE subject_id=? AND lower(property)=? "
                "AND lower(value)=?",
                (sid, prop.lower(), val.lower()),
            ).fetchone()
            if not exists:
                self.overlay.execute(
                    "INSERT INTO facts VALUES (?,?,?,?,?,?,?)",
                    (
                        sid,
                        prop,
                        val,
                        source_id,
                        str(d.get("source_title", "")),
                        str(d.get("source_snippet", "")),
                        time.time(),
                    ),
                )
                self.overlay.commit()
        return {"ok": True, "facts": self.overlay_facts(sid)}

    def dismiss_fact(self, d: dict) -> dict:
        """Suppress a suggestion so it does not resurface on the next /enrich."""
        sid = int(d["subject_id"])
        with self.overlay_lock:
            self.overlay.execute(
                "INSERT INTO dismissed VALUES (?,?,?)",
                (sid, str(d.get("property", "")), str(d.get("value", ""))),
            )
            self.overlay.commit()
        return {"ok": True}


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
header h1 { font-size:16px; margin:0; font-weight:600; white-space:nowrap; cursor:pointer; }
header h1:hover { text-decoration:underline; }
#q { flex:1; padding:8px 12px; font-size:15px; border:0; border-radius:4px; }
#ask { flex:1.4; padding:8px 12px; font-size:15px; border:0; border-radius:4px;
  background:#fff8e1; }
.answer { line-height:1.7; font-size:15px; white-space:pre-wrap; margin-bottom:8px;
  background:#fffbea; border:1px solid #f0e2b0; border-radius:6px; padding:14px 16px; }
main { display:grid; grid-template-columns:260px 1fr 300px; gap:0; height:calc(100vh - 140px); }
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
/* semantic-search bar (vector similarity + relational filters) */
#sembar { display:flex; gap:8px; align-items:center; padding:6px 16px;
  background:#f4f0fb; border-bottom:1px solid #e3ddf0; position:sticky; top:96px;
  z-index:8; font-size:13px; flex-wrap:wrap; }
#sembar input { padding:6px 10px; border:1px solid #d6cfe8; border-radius:4px;
  font-size:13px; background:#fff; }
#sq { flex:1; min-width:180px; }
#sembar input.numf { width:90px; }
#sbtn { padding:6px 16px; border:0; border-radius:4px; background:#7a4aa0; color:#fff;
  cursor:pointer; font-size:13px; }
#sbtn:hover { background:#653c88; }
.gexp { display:flex; align-items:center; gap:4px; color:#fff; font-size:12px;
  white-space:nowrap; cursor:pointer; }
.countbar { font-size:12px; color:#555; margin:4px 8px 8px; line-height:1.5; }
.countbar b { color:#7a4aa0; }
.meta { font-size:11px; color:#8a8a8a; margin-top:2px; }
.catpill { display:inline-block; background:#f4ecf9; color:#7a4aa0; border-radius:8px;
  padding:0 6px; margin:2px 3px 0 0; font-size:10px; }
.og { font-size:9.5px; text-transform:uppercase; letter-spacing:.03em; padding:1px 5px;
  border-radius:8px; font-weight:600; white-space:nowrap; }
.og-sem { background:#e8eefc; color:#2554c7; }
.og-graph { background:#e3f5e6; color:#1a7a2e; }
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
/* inline links are INVISIBLE at rest: identical color/font to body prose, no
   underline, no blue. A faint tint + underline appears ONLY on :hover, so links
   stay discoverable without disrupting reading. */
#article a.wl { color:inherit; text-decoration:none; cursor:pointer; }
#article a.wl:hover { background:#eef3ff; text-decoration:underline;
  text-decoration-color:#9db6e0; text-underline-offset:2px; }
/* section headings + lists recovered from the plain text */
#article h3.wsec { font-size:1.16em; font-weight:600; margin:1.5em 0 .5em;
  padding-bottom:.2em; border-bottom:1px solid #e4e4e4; }
#article ul { margin:.5em 0 .6em 1.4em; padding:0; }
#article li { line-height:1.6; margin:.2em 0; }
/* back control in the header */
#back { padding:7px 12px; border:0; border-radius:4px; background:#2a5bd0; color:#fff;
  cursor:pointer; font-size:13px; white-space:nowrap; visibility:hidden; }
#back:hover { background:#25b; }
/* home control in the header (distinct from Back: jumps to a fresh random article) */
#home { padding:7px 12px; border:0; border-radius:4px; background:#2a5bd0; color:#fff;
  cursor:pointer; font-size:13px; white-space:nowrap; }
#home:hover { background:#25b; }
/* landing-page "another random" bar, sits above the random article body */
.homebar { display:flex; align-items:center; gap:10px; margin:0 0 14px;
  padding-bottom:12px; border-bottom:1px solid #eee; }
.homebar .subtle { margin:0; }
/* hovercard (page preview) */
#hovercard { position:fixed; display:none; z-index:50; width:320px; max-width:88vw;
  background:#fff; border:1px solid #d3d7de; border-radius:8px;
  box-shadow:0 6px 24px rgba(0,0,0,.18); padding:12px 14px; font-size:13px;
  line-height:1.5; color:#222; }
#hovercard .hc-title { font-weight:600; font-size:14px; margin-bottom:5px; }
#hovercard .hc-lead { color:#333; }
#hovercard .hc-read { display:inline-block; margin-top:8px; color:#36c; cursor:pointer;
  font-size:12px; font-weight:600; }
#hovercard .hc-load { color:#999; font-style:italic; }
/* Enrichment: on-demand cited fact suggestions + the accepted overlay section.
   Tinted + clearly marked so it never reads as part of the original article body
   (which is NEVER mutated — accepted facts live only in the overlay DB). */
#enrichbar { margin:2px 0 10px; }
.enrich-sec, .enrich-sug { border-radius:8px; padding:10px 14px; margin:12px 0 16px; }
.enrich-sec { background:#eefaf0; border:1px solid #bfe6c8; }      /* accepted (green) */
.enrich-sug { background:#fff8e6; border:1px solid #f0deac; }      /* suggestions (amber) */
.enrich-h { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  color:#556; margin-bottom:8px; }
.enrich-fact { border-top:1px solid rgba(0,0,0,.06); padding:8px 0 6px; }
.enrich-fact:first-of-type { border-top:0; }
.ef-prop { font-size:11px; text-transform:uppercase; letter-spacing:.03em; color:#7a4aa0;
  font-weight:600; }
.ef-val { font-size:15px; line-height:1.45; margin:1px 0 3px; color:#1a1a1a; }
.ef-src { font-size:12px; color:#777; line-height:1.45; }
.ef-link { color:#36c; cursor:pointer; }
.ef-link:hover { text-decoration:underline; }
.ef-act { margin-top:6px; display:flex; gap:8px; }
</style></head>
<body>
<header>
  <button id="back" onclick="goBack()" title="Back (also works with the browser Back button)">&larr; Back</button>
  <button id="home" onclick="goHome()" title="Home — jump to a fresh random article">&#127968; Home</button>
  <h1 onclick="goHome()" title="Home — a fresh random article">Offline Wikipedia</h1>
  <input id="q" placeholder="Search titles (e.g. Ada Lovelace) — Enter" autofocus>
  <input id="ask" placeholder="Ask a question (RAG over 6.9M articles) — Enter">
  <label class="gexp" title="Graph-aware RAG: expand along hyperlinks before answering">
    <input type="checkbox" id="gexp" checked> graph
  </label>
</header>
<div id="connectbar">
  <span class="cblabel">How are these connected?</span>
  <input id="from" placeholder="From (e.g. Ada Lovelace)">
  <span class="cbarrow">&rarr;</span>
  <input id="to" placeholder="To (e.g. Charles Babbage)">
  <button id="cbtn" onclick="connect()">Connect</button>
</div>
<div id="sembar">
  <span class="cblabel">Semantic search</span>
  <input id="sq" placeholder="Meaning query (e.g. quantum computing)">
  <input id="minlinks" class="numf" type="number" min="0" placeholder="min links">
  <input id="minlen" class="numf" type="number" min="0" placeholder="min chars">
  <input id="maxlen" class="numf" type="number" min="0" placeholder="max chars">
  <input id="scat" placeholder="category contains…">
  <button id="sbtn" onclick="semSearch()">Search</button>
</div>
<main>
  <div id="results"><div class="hint">Type a query and press Enter.</div></div>
  <div id="article"><div class="hint">Select an article.</div></div>
  <div id="related"></div>
</main>
<div id="hovercard"></div>
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
  await loadSearch(q);
  pushView({view:'search', q:q});
}
async function loadSearch(q){   // render only, no history push
  $('#q').value = q;
  const items = await j('/search?q='+encodeURIComponent(q));
  const el = $('#results');
  if(!items.length){ el.innerHTML = '<div class="hint">No title matches.</div>'; return; }
  el.innerHTML = '<div class="secttl">'+items.length+' results</div>' +
    items.map(a => `<div class="item" onclick="open_(${a.id})">${esc(a.title)}</div>`).join('');
}
let _curId = null;
// open_ = in-app navigation to an article: render + push a history entry so the
// browser Back/Forward buttons and the header "← Back" walk the article sequence.
async function open_(id){ await loadArticle(id); pushView({view:'article', id:id}); }
async function loadArticle(id){   // render only, no history push (used by popstate)
  _curId = id;
  const a = await j('/article/'+id);
  const art = $('#article');
  if(!a || a.error){ art.innerHTML = '<div class="hint">Not found.</div>'; return; }
  // body_html carries the inline <a class="wl"> links + heading/list structure
  // (escaped server-side); fall back to plain-text if an older server omits it.
  const body = a.body_html
    ? a.body_html
    : a.body.split(/\\n\\n+/).map(p => '<p>'+esc(p).replace(/\\n/g,'<br>')+'</p>').join('');
  art.innerHTML = '<h2>'+esc(a.title)+'</h2>'+
    '<div id="enrichbar"><button class="lbtn" onclick="findEnrich('+id+')">'+
      '&#128269; Find enrichments</button>'+
      '<span id="enrichmsg" class="subtle" style="margin-left:8px"></span></div>'+
    '<div id="enrichaccepted">'+renderEnrichments(a.enrichments)+'</div>'+
    '<div id="enrichsug"></div>'+
    body;
  art.scrollTop = 0;
  loadRelated(id);
}
// -- Enrichment (on-demand, reviewed, CITED) ----------------------------------
// Accepted overlay facts render in a tinted, clearly-marked section (green) — the
// original article body is NEVER mutated. Suggestions (amber) each cite a source
// article + a verbatim snippet; unsourced ones were dropped server-side.
let _enrichData = {};
function renderEnrichments(facts){
  if(!facts || !facts.length) return '';
  return '<div class="enrich-sec"><div class="enrich-h">&#10024; Enrichments '+
    '(from related articles)</div>'+facts.map(enrichFactRow).join('')+'</div>';
}
function enrichFactRow(f){
  return '<div class="enrich-fact"><div class="ef-prop">'+esc(f.property)+'</div>'+
    '<div class="ef-val">'+esc(f.value)+'</div>'+
    '<div class="ef-src">source: <a class="ef-link" onclick="open_('+f.source_id+')">'+
      esc(f.source_title || ('#'+f.source_id))+'</a> &mdash; &ldquo;'+
      esc(f.source_snippet)+'&rdquo;</div></div>';
}
function sugRow(id, f, i){
  return '<div class="enrich-fact" id="sug'+i+'"><div class="ef-prop">'+esc(f.property)+
    '</div><div class="ef-val">'+esc(f.value)+'</div>'+
    '<div class="ef-src">source: <a class="ef-link" onclick="open_('+f.source_id+')">'+
      esc(f.source_title || ('#'+f.source_id))+'</a> &mdash; &ldquo;'+
      esc(f.source_snippet)+'&rdquo;</div>'+
    '<div class="ef-act"><button class="lbtn" onclick="acceptEnrich('+id+','+i+')">'+
      'Accept</button><button class="lbtn" onclick="dismissEnrich('+id+','+i+')">'+
      'Dismiss</button></div></div>';
}
function renderSuggestions(id, r){
  _enrichData = {};
  let h = '';
  if(r.suggestions && r.suggestions.length){
    r.suggestions.forEach((f,i) => { _enrichData[i] = f; });
    h += '<div class="enrich-sug"><div class="enrich-h">Possibly missing '+
      '(from related articles)</div>'+
      r.suggestions.map((f,i) => sugRow(id,f,i)).join('')+'</div>';
  }
  if(r.missing_sections && r.missing_sections.length){
    h += '<div class="enrich-sug"><div class="enrich-h">Sections similar articles '+
      'have</div>'+r.missing_sections.map(s =>
        '<div class="enrich-fact"><div class="ef-prop">section</div>'+
        '<div class="ef-val">'+esc(s.name)+'</div>'+
        '<div class="ef-src">seen in '+s.seen_in.length+' similar article(s)</div></div>'
      ).join('')+'</div>';
  }
  if(!h) h = '<div class="hint">No sourced suggestions found.</div>';
  return h;
}
async function findEnrich(id){
  const msg = $('#enrichmsg'), box = $('#enrichsug');
  msg.textContent = 'scanning related articles (local LLM)…';
  box.innerHTML = '';
  let r;
  try { r = await j('/enrich/'+id); }
  catch(e){ msg.textContent = 'enrich failed: '+String(e); return; }
  if(r.error){ msg.textContent = r.error; return; }
  const n = (r.suggestions?r.suggestions.length:0)+(r.missing_sections?r.missing_sections.length:0);
  msg.textContent = n+' suggestion(s) from '+r.n_sources+' related articles'+
    (r.n_dropped ? (' · '+r.n_dropped+' unsourced dropped') : '');
  box.innerHTML = renderSuggestions(id, r);
}
async function acceptEnrich(id, i){
  const f = _enrichData[i]; if(!f) return;
  const payload = { subject_id:id, property:f.property, value:f.value,
    source_id:f.source_id, source_title:f.source_title, source_snippet:f.source_snippet };
  let r;
  try { r = await (await fetch('/enrich/accept', { method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) })).json(); }
  catch(e){ return; }
  const row = $('#sug'+i); if(row) row.style.display = 'none';
  if(r.facts) $('#enrichaccepted').innerHTML = renderEnrichments(r.facts);
}
async function dismissEnrich(id, i){
  const f = _enrichData[i]; if(!f) return;
  try { await fetch('/enrich/dismiss', { method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ subject_id:id, property:f.property, value:f.value }) }); }
  catch(e){ return; }
  const row = $('#sug'+i); if(row) row.style.display = 'none';
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
  await loadConnect(f, t);
  pushView({view:'connect', f:f, t:t});
}
async function loadConnect(f, t){   // render only, no history push
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
  await loadAsk(q);
  pushView({view:'ask', q:q});
}
function srcRow(s){
  const badge = (s.origin==='graph')
    ? '<span class="og og-graph" title="pulled in by graph expansion'+
        (s.via?' — hyperlinked from '+esc(s.via):'')+'">graph'+
        (s.via?' &larr; '+esc(s.via):'')+'</span>'
    : '<span class="og og-sem">semantic</span>';
  return '<div class="item" onclick="open_('+s.id+')"><div class="rtitle">['+s.n+'] '+
    esc(s.title)+' '+badge+'</div>'+relInd(s.score)+'</div>';
}
async function loadAsk(q){   // render only, no history push
  const art = $('#article');
  const expand = $('#gexp').checked ? 1 : 0;
  art.innerHTML = '<div class="hint">Thinking — retrieving passages'+
    (expand?' + graph expansion':'')+' + local LLM…</div>';
  let r;
  try { r = await j('/ask?q='+encodeURIComponent(q)+'&expand='+expand); }
  catch(e){ art.innerHTML = '<div class="hint">Ask failed: '+esc(String(e))+'</div>'; return; }
  let h = '<h2>Ask</h2><div class="answer">'+esc(r.answer)+'</div>';
  if(r.sources && r.sources.length){
    const sub = r.expanded
      ? '<div class="legend">'+r.n_semantic+' from semantic retrieval &middot; '+
          r.n_graph+' pulled in by graph expansion (hyperlink neighbours), '+
          'ranked by relevance to the question</div>'
      : '<div class="legend">semantic retrieval only (graph expansion off)</div>';
    h += '<div class="hd">Sources (click to open)</div>' + sub;
    h += r.sources.map(srcRow).join('');
  } else {
    h += '<div class="hint">No sources retrieved.</div>';
  }
  art.innerHTML = h;
  art.scrollTop = 0;
}
// -- filtered semantic search (vector similarity + relational filter) ----------
function semParams(){
  return { q: $('#sq').value.trim(), min_indeg: $('#minlinks').value.trim(),
    min_len: $('#minlen').value.trim(), max_len: $('#maxlen').value.trim(),
    cat: $('#scat').value.trim() };
}
async function semSearch(){
  const p = semParams();
  if(!p.q) return;
  await loadSem(p);
  pushView({view:'sem', p:p});
}
async function loadSem(p){   // render only, no history push
  $('#sq').value=p.q||''; $('#minlinks').value=p.min_indeg||''; $('#minlen').value=p.min_len||'';
  $('#maxlen').value=p.max_len||''; $('#scat').value=p.cat||'';
  const el = $('#results');
  el.innerHTML = '<div class="hint">Embedding query + relational filter…</div>';
  const qs = '/search_semantic?q='+encodeURIComponent(p.q)+
    '&min_indeg='+encodeURIComponent(p.min_indeg||0)+
    '&min_len='+encodeURIComponent(p.min_len||0)+
    '&max_len='+encodeURIComponent(p.max_len||0)+
    '&cat='+encodeURIComponent(p.cat||'');
  let r;
  try { r = await j(qs); } catch(e){ el.innerHTML='<div class="hint">Search failed.</div>'; return; }
  renderSem(r);
}
function fdesc(f, catsAvail){
  const parts=[];
  if(f.min_indeg>0) parts.push('≥'+f.min_indeg+' inbound links');
  if(f.min_len>0) parts.push('≥'+f.min_len+' chars');
  if(f.max_len>0) parts.push('≤'+f.max_len+' chars');
  if(f.category) parts.push('category ~ "'+f.category+'"'+(catsAvail?'':' (category index absent)'));
  return parts.length ? parts.join(', ') : 'no filters';
}
function semRow(a){
  const meta=[a.indeg.toLocaleString()+' links'];
  if(a.length!=null) meta.push(a.length.toLocaleString()+' chars');
  const cats = (a.cats&&a.cats.length)
    ? '<div>'+a.cats.map(c=>'<span class="catpill">'+esc(c)+'</span>').join('')+'</div>' : '';
  return '<div class="item" onclick="open_('+a.id+')"><div class="rtitle">'+esc(a.title)+'</div>'+
    relInd(a.score)+'<div class="meta">'+meta.join(' · ')+'</div>'+cats+'</div>';
}
function renderSem(r){
  const el = $('#results');
  let h = '<div class="countbar">Retrieved <b>'+r.pool+'</b> by meaning &rarr; <b>'+r.pre_count+
    '</b> resolved &rarr; <b>'+r.post_count+'</b> after filter'+
    '<br><span style="color:#999">filters: '+esc(fdesc(r.filters, r.cats_available))+'</span></div>';
  if(!r.results.length){ el.innerHTML = h+'<div class="hint">No results after filtering.</div>'; return; }
  el.innerHTML = h + r.results.map(semRow).join('');
}
// -- inline-link clicks + hovercards (page previews) --------------------------
const _sumCache = {};        // id -> {title, lead}, cached per session
let _hcTimer = null, _hcId = null;
const _hc = $('#hovercard');
function hideHover(){ if(_hcTimer){clearTimeout(_hcTimer); _hcTimer=null;} _hcId=null; _hc.style.display='none'; }
function placeHover(x, y){
  _hc.style.display = 'block';
  const w = _hc.offsetWidth, h = _hc.offsetHeight;
  let left = x + 14, top = y + 16;
  if(left + w > window.innerWidth - 8) left = x - w - 14;
  if(left < 8) left = 8;
  if(top + h > window.innerHeight - 8) top = y - h - 16;
  if(top < 8) top = 8;
  _hc.style.left = left + 'px'; _hc.style.top = top + 'px';
}
function renderHover(s, x, y){
  _hc.innerHTML = '<div class="hc-title">'+esc(s.title)+'</div>'+
    '<div class="hc-lead">'+esc(s.lead || '')+'</div>'+
    '<div class="hc-read" onclick="open_('+s.id+');hideHover()">read &rarr;</div>';
  placeHover(x, y);
}
async function showHover(id, x, y){
  _hcId = id;
  if(_sumCache[id]){ renderHover(_sumCache[id], x, y); return; }
  _hc.innerHTML = '<div class="hc-load">loading preview…</div>';
  placeHover(x, y);
  let s;
  try { s = await j('/summary/'+id); } catch(e){ return; }
  if(!s || s.error) { if(_hcId===id) hideHover(); return; }
  _sumCache[id] = s;
  if(_hcId === id) renderHover(s, x, y);      // still hovering the same link
}
$('#article').addEventListener('mouseover', e => {
  const a = e.target.closest('a.wl');
  if(!a) return;
  const id = parseInt(a.getAttribute('data-id'), 10);
  const x = e.clientX, y = e.clientY;
  if(_hcTimer) clearTimeout(_hcTimer);
  _hcTimer = setTimeout(() => showHover(id, x, y), 250);   // debounce
});
$('#article').addEventListener('mouseout', e => {
  const a = e.target.closest('a.wl');
  if(a && !a.contains(e.relatedTarget) && !_hc.contains(e.relatedTarget)) hideHover();
});
$('#article').addEventListener('click', e => {   // intercept inline links -> open in-app
  const a = e.target.closest('a.wl');
  if(!a) return;
  e.preventDefault();
  hideHover();
  open_(parseInt(a.getAttribute('data-id'), 10));
});
$('#hovercard').addEventListener('mouseleave', hideHover);

// -- in-app history navigation (browser Back/Forward + header "← Back") --------
// Each in-app navigation pushes a state; popstate re-renders WITHOUT pushing, so
// native Back/Forward and goBack() walk the same article/search sequence.
function pushView(st){
  const hash = st.view + (st.id!=null ? '/'+st.id : '');
  history.pushState(st, '', '#'+hash);
  updateBack();
}
function updateBack(){
  const st = history.state;
  $('#back').style.visibility = (st && st.view && st.view!=='home') ? 'visible' : 'hidden';
}
function goBack(){ history.back(); }
// Home = the random-article landing. Distinct from Back (history): Home jumps to a
// FRESH random article every time, never a cached pick.
async function loadHome(){   // render a fresh random article as the landing view
  _curId = null; hideHover();
  $('#results').innerHTML = '<div class="hint">Type a query and press Enter, or explore a random article &rarr;</div>';
  const art = $('#article');
  art.innerHTML = '<div class="hint">Loading a random article…</div>';
  $('#related').innerHTML = '';
  let r;
  try { r = await j('/random'); }
  catch(e){ art.innerHTML = '<div class="hint">Random article failed: '+esc(String(e))+'</div>'; return; }
  if(!r || !r.id){ art.innerHTML = '<div class="hint">No article available.</div>'; return; }
  await loadArticle(r.id);   // full render: invisible links + hovercards + related
  // prepend the "another random" control above the freshly-rendered article
  const bar = document.createElement('div');
  bar.className = 'homebar';
  bar.innerHTML = '<button class="lbtn" onclick="anotherRandom()">&#127922; Another random article</button>'+
    '<span class="subtle">Random article &middot; Home or &#127922; for another</span>';
  art.insertBefore(bar, art.firstChild);
  art.scrollTop = 0;
}
// 🎲 refresh in place — no new history entry (keeps the Back stack clean)
function anotherRandom(){ loadHome(); }
// Home button / title click — jump to a fresh random landing. If we are already on
// a home state, just refresh in place; otherwise push a home entry so Back returns.
function goHome(){
  if(history.state && history.state.view==='home'){ loadHome(); }
  else { loadHome(); pushView({view:'home'}); }
}
function renderState(st){   // restore a view for a popstate (no new push)
  hideHover();
  if(!st || st.view==='home'){ loadHome(); }
  else if(st.view==='article'){ loadArticle(st.id); }
  else if(st.view==='connect'){ loadConnect(st.f, st.t); }
  else if(st.view==='ask'){ loadAsk(st.q); }
  else if(st.view==='sem'){ loadSem(st.p); }
  else if(st.view==='search'){ loadSearch(st.q); }
  updateBack();
}
window.addEventListener('popstate', e => renderState(e.state));
history.replaceState({view:'home'}, '', location.pathname);   // base state
updateBack();
loadHome();   // landing view: a fresh random article

$('#q').addEventListener('keydown', e => { if(e.key==='Enter') search(); });
$('#ask').addEventListener('keydown', e => { if(e.key==='Enter') ask(); });
$('#from').addEventListener('keydown', e => { if(e.key==='Enter') connect(); });
$('#to').addEventListener('keydown', e => { if(e.key==='Enter') connect(); });
['#sq','#minlinks','#minlen','#maxlen','#scat'].forEach(sel =>
  $(sel).addEventListener('keydown', e => { if(e.key==='Enter') semSearch(); }));
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
                elif path == "/random":
                    r = reader.random_article()
                    self._json(r if r is not None else {"error": "no articles"})
                elif path == "/search":
                    q = parse_qs(u.query).get("q", [""])[0]
                    self._json(reader.search(q))
                elif path == "/ask":
                    qs = parse_qs(u.query)
                    q = qs.get("q", [""])[0]
                    expand = qs.get("expand", ["1"])[0] != "0"
                    try:
                        hops = int(qs.get("hops", ["1"])[0] or "1")
                    except ValueError:
                        hops = 1
                    self._json(reader.ask(q, expand=expand, hops=hops))
                elif path == "/search_semantic":
                    qs = parse_qs(u.query)

                    def _int(name: str) -> int:
                        try:
                            return int(qs.get(name, ["0"])[0] or "0")
                        except ValueError:
                            return 0

                    self._json(
                        reader.search_semantic(
                            qs.get("q", [""])[0],
                            pool=_int("pool") or 150,
                            min_indeg=_int("min_indeg"),
                            min_len=_int("min_len"),
                            max_len=_int("max_len"),
                            cat=qs.get("cat", [""])[0].strip(),
                        )
                    )
                elif path.startswith("/article/"):
                    aid = int(unquote(path.split("/")[-1]))
                    art = reader.article(aid)
                    if art is None:
                        self._json({"error": "not found"})
                    else:
                        art["body_html"] = reader.link_body(aid, art["body"])
                        # cheap indexed overlay lookup — accepted enrichments render
                        # on the page without a second round-trip (never mutates body)
                        art["enrichments"] = reader.overlay_facts(aid)
                        self._json(art)
                elif path.startswith("/enrich/"):
                    aid = int(unquote(path.split("/")[-1]))
                    self._json(reader.enrich(aid))
                elif path.startswith("/summary/"):
                    aid = int(unquote(path.split("/")[-1]))
                    s = reader.summary(aid)
                    self._json(s if s is not None else {"error": "not found"})
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

        def do_POST(self):
            u = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if u.path == "/enrich/accept":
                    self._json(reader.accept_fact(body))
                elif u.path == "/enrich/dismiss":
                    self._json(reader.dismiss_fact(body))
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:
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
    sub.add_parser(
        "build-redirects",
        help="build ONLY the redirect alias index in reader.db (fast, no re-scan)",
    )
    sub.add_parser(
        "build-categories",
        help="build ONLY the category index (cats table) in reader.db (no re-scan)",
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
    elif args.cmd == "build-redirects":
        cmd_build_redirects(args.corpus)
    elif args.cmd == "build-categories":
        cmd_build_categories(args.corpus)
    else:
        cmd_serve(args.corpus, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

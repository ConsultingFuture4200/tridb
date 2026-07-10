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
import math
from html.parser import HTMLParser
import json
import os
import random
import re
import secrets
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
        pool = max(limit * 5, 200)  # over-fetch by FTS rank, then re-rank by notability
        with self.db_lock:
            try:
                rows = self.db.execute(
                    "SELECT rowid, title FROM titles_fts WHERE titles_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (match, pool),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        if not rows:
            return []
        # Bias toward EXACT + MOST-NOTABLE titles: in-degree (log) dominates, with a
        # nudge for an exact / prefix / whole-word title match. So "Einstein" -> the
        # heavily-linked person, not a short-but-obscure "Einsteinhaus".
        indeg = self._ensure_indeg()
        n_indeg = len(indeg)
        ql = q.strip().lower()
        qword = re.compile(r"(?<!\w)" + re.escape(ql) + r"(?!\w)")

        def score(fts_pos: int, aid: int, title: str) -> float:
            tl = title.lower()
            deg = int(indeg[aid]) if aid < n_indeg else 0
            s = math.log1p(deg)                 # notability, log-scaled — dominant
            if tl == ql:
                s += 2.5                        # exact title match
            elif tl.startswith(ql):
                s += 1.0                        # title starts with the query
            elif qword.search(tl):
                s += 0.5                        # query is a whole word in the title
            return s - fts_pos * 1e-4           # stable FTS-order tiebreak

        ranked = sorted(
            enumerate(rows), key=lambda t: -score(t[0], t[1][0], t[1][1])
        )
        return [{"id": r[0], "title": r[1]} for _, r in ranked[:limit]]

    def _titles(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        qmarks = ",".join("?" * len(ids))
        with self.db_lock:
            rows = self.db.execute(
                f"SELECT id, title FROM articles WHERE id IN ({qmarks})", ids
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def article(self, aid: int, with_html: bool = False) -> dict | None:
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
        out = {"id": aid, "title": title, "body": _clean_body(obj.get("text", ""))}
        if with_html:
            out["html"] = obj.get("html") or ""
        return out

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

    def render_html_body(self, aid: int, raw_html: str) -> str:
        """Sanitise the structured article HTML (dump-sourced -> safe subset) and
        rewire its data-wiki-title links to in-app navigation. Out-edge targets
        resolve to a direct article link; unresolved titles become a title-search
        link. Used when a record carries an `html` field; link_body (plain text)
        is the fallback for the older text-only corpus."""
        id2title = self._out_target_titles(aid)
        title2id = {t: i for i, t in id2title.items()}
        norm2id = {_norm_title(t): i for i, t in id2title.items()}
        # dense inline auto-linking over the HTML's text nodes: same Layer-A surface
        # map + full-corpus matcher as link_body, so nearly every notable term links.
        a_surf: dict[str, int] = {}
        for tid, title in id2title.items():
            key = _norm_ws(title).lower()
            if len(key) >= 2:
                a_surf.setdefault(key, tid)
        for tid, alias in self._aliases_for(list(id2title)):
            key = _norm_ws(alias).lower()
            if len(key) >= 3:
                a_surf.setdefault(key, tid)
        a_rx = None
        if a_surf:
            alt = "|".join(re.escape(k) for k in sorted(a_surf, key=len, reverse=True))
            a_rx = re.compile(
                r"(?<![0-9A-Za-z])(?:" + alt + r")(?![0-9A-Za-z])", re.IGNORECASE
            )

        gmap = self._ensure_global()

        def link(frag: str) -> str:
            return self._linkify(frag, a_surf, a_rx, aid)

        san = _HtmlSanitizer(title2id, norm2id, link, gmap)
        san.feed(raw_html)
        san.close()
        return san.result()

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

    def search_trimodal(
        self,
        q: str,
        seed: int = 40,
        pool: int = 150,
        expand: bool = True,
        min_indeg: int = 0,
        min_len: int = 0,
        max_len: int = 0,
        cat: str = "",
    ) -> dict:
        """All three legs in ONE query — the reader-side mirror of TriDB's tjs_open:
        (1) VECTOR seed: the `seed` nearest articles by meaning (cuVS cosine);
        (2) GRAPH expand: fold in the seeds' out-link neighbours (native adjacency);
        (3) RELATIONAL filter: prune by inbound-degree / length / category;
        then rank the survivors by a fused cosine + graph-proximity score."""
        q = (q or "").strip()
        base = {
            "query": q,
            "seed": seed,
            "expand": expand,
            "cats_available": self._has_cats,
            "filters": {
                "min_indeg": min_indeg,
                "min_len": min_len,
                "max_len": max_len,
                "category": cat,
            },
        }
        if not q:
            return {**base, "seed_count": 0, "expanded_count": 0,
                    "pre_count": 0, "post_count": 0, "results": []}
        qvec = self._embed_query(q)
        # (1) VECTOR seed
        with self.index_lock:
            labels, _ = self.index.knn_query(qvec, k=seed)
        seed_ids: list[int] = []
        seed_set: set[int] = set()
        for lab in labels[0]:
            a = int(lab)
            if a not in seed_set:
                seed_set.add(a)
                seed_ids.append(a)
        # (2) GRAPH expand — out-neighbours of the seeds (native CSR adjacency)
        graph_hits: dict[int, int] = {}
        if expand:
            cap_out = 200  # bound per-seed fan-out on hub pages
            for sid in seed_ids:
                if sid + 1 >= len(self.csr_off):
                    continue
                lo, hi = int(self.csr_off[sid]), int(self.csr_off[sid + 1])
                for x in self.csr_dst[lo:hi][:cap_out].tolist():
                    x = int(x)
                    if x in seed_set:
                        continue
                    graph_hits[x] = graph_hits.get(x, 0) + 1
        expanded_ids = [i for i, _ in sorted(graph_hits.items(), key=lambda t: -t[1])[:pool]]
        cand = list(dict.fromkeys(seed_ids + expanded_ids))  # de-dup, order-stable
        titles = self._titles(cand)
        cand = [a for a in cand if a in titles]
        seed_count, expanded_count, pre_count = len(seed_ids), len(expanded_ids), len(cand)
        # (3) RELATIONAL filter
        indeg = self._ensure_indeg()
        n_indeg = len(indeg)

        def deg(a: int) -> int:
            return int(indeg[a]) if a < n_indeg else 0

        if min_indeg > 0:
            cand = [a for a in cand if deg(a) >= min_indeg]
        if cat and self._has_cats:
            keep = self._filter_by_category(cand, cat)
            cand = [a for a in cand if a in keep]
        lengths: dict[int, int] = {}
        if min_len > 0 or max_len > 0:
            filt: list[int] = []
            for a in cand:
                art = self.article(a)
                if art is None:
                    continue
                length = len(art["body"])
                if min_len and length < min_len:
                    continue
                if max_len and length > max_len:
                    continue
                lengths[a] = length
                filt.append(a)
            cand = filt
        # RANK — fuse cosine (vector) with normalised graph proximity (seed in-links)
        cos = self._score_by_query(qvec, cand)
        max_hits = max(graph_hits.values()) if graph_hits else 1
        results = []
        for a in cand:
            c = float(cos.get(a, 0.0))
            g = graph_hits.get(a, 0) / max_hits
            in_seed, in_graph = a in seed_set, a in graph_hits
            prov = ("meaning + linked" if in_seed and in_graph
                    else "meaning" if in_seed else "linked")
            results.append({
                "id": a, "title": titles[a],
                "score": round(c + 0.25 * g, 4), "cos": round(c, 4),
                "graph": graph_hits.get(a, 0), "indeg": deg(a),
                "length": lengths.get(a), "prov": prov,
            })
        results.sort(key=lambda r: (-r["score"], r["id"]))
        cats_by = self._cats_for([r["id"] for r in results[:40]]) if self._has_cats else {}
        for r in results:
            r["cats"] = cats_by.get(r["id"], [])
        return {**base, "seed_count": seed_count, "expanded_count": expanded_count,
                "pre_count": pre_count, "post_count": len(results), "results": results}

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
        val = str(d.get("value", ""))[:300]
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
                        str(d.get("source_title", ""))[:300],
                        str(d.get("source_snippet", ""))[:300],
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


_HTML_OK = {
    "p", "div", "span", "section", "br", "hr", "b", "strong", "i", "em", "u", "s",
    "small", "sub", "sup", "abbr", "cite", "q", "code", "pre", "blockquote", "kbd",
    "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "figure", "figcaption", "img", "a",
}
_HTML_VOID = {"br", "hr", "col"}
_HTML_DROP_BLOCK = {
    "script", "style", "iframe", "object", "form", "noscript", "svg", "math",
    "button", "select", "textarea", "audio", "video",
}
_HTML_DROP_VOID = {"input", "embed", "link", "meta", "source", "track"}
_IMG_OK = re.compile(r"^(?:https?:)?//upload\.wikimedia\.org/")
_NS_DROP = {"File", "Image", "Category", "Template", "Help", "Wikipedia",
            "Portal", "Special", "Module", "Draft", "MediaWiki", "Talk", "User"}


def _norm_title(t: str) -> str:
    return (t or "").strip().lower().replace("_", " ")


class _HtmlSanitizer(HTMLParser):
    """Whitelist-sanitise the extractor's dump-sourced article HTML into a safe,
    Wikipedia-like subset. Drops script/style/iframe/form/... (with their content),
    strips every attribute except `class` (for styling), colspan/rowspan on cells,
    and an <img> `src` whitelisted to upload.wikimedia.org. `data-wiki-title` links
    are rewired to in-app navigation. Injection-safe: text is html.escape()'d and
    only integer ids, escaped class tokens, and whitelisted image URLs reach any
    attribute value."""

    def __init__(self, title2id, norm2id, link_fn=None, gmap=None):
        super().__init__(convert_charrefs=True)
        self.title2id = title2id
        self.norm2id = norm2id
        self.link_fn = link_fn
        self.gmap = gmap
        self.out = []
        self.skip = 0
        self.astack = []
        self.anchor_depth = 0

    def _attrs(self, tag, attrs):
        d = dict(attrs)
        out = ""
        cls = d.get("class")
        if cls:
            out += f' class="{html.escape(cls, quote=True)}"'
        if tag in ("td", "th", "col", "colgroup"):
            for k in ("colspan", "rowspan", "span"):
                v = d.get(k)
                if v and v.isdigit():
                    out += f' {k}="{v}"'
        return out

    def _img(self, d):
        src = d.get("src") or d.get("data-src") or ""
        if not _IMG_OK.match(src):
            return
        if src.startswith("//"):
            src = "https:" + src
        a = f' src="{html.escape(src, quote=True)}" loading="lazy"'
        for k in ("width", "height"):
            v = d.get(k) or d.get("data-" + k)
            if v and str(v).isdigit():
                a += f' {k}="{v}"'
        alt = d.get("alt")
        if alt:
            a += f' alt="{html.escape(alt, quote=True)}"'
        cls = d.get("class")
        if cls:
            a += f' class="{html.escape(cls, quote=True)}"'
        self.out.append(f"<img{a}>")

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in _HTML_DROP_BLOCK:
                self.skip += 1
            return
        if tag in _HTML_DROP_BLOCK:
            self.skip = 1
            return
        if tag in _HTML_DROP_VOID:
            return
        if tag == "a":
            self._open_a(dict(attrs))
            return
        if tag == "img":
            self._img(dict(attrs))
            return
        if tag in _HTML_VOID:
            self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
            return
        if tag in _HTML_OK:
            self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        # unknown tag -> unwrap (children kept)

    def handle_startendtag(self, tag, attrs):
        if self.skip:
            return
        if tag == "img":
            self._img(dict(attrs))
            return
        if tag in _HTML_VOID:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if self.skip:
            if tag in _HTML_DROP_BLOCK:
                self.skip -= 1
            return
        if tag == "a":
            if self.astack and self.astack.pop():
                self.out.append("</a>")
                self.anchor_depth -= 1
            return
        if tag in _HTML_OK and tag not in _HTML_VOID and tag != "img":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.anchor_depth == 0 and self.link_fn is not None:
            self.out.append(self.link_fn(data))
        else:
            self.out.append(html.escape(data))

    def _open_a(self, attrs):
        wt = attrs.get("data-wiki-title")
        if wt:
            ns = wt.split(":", 1)[0] if ":" in wt else ""
            if ns in _NS_DROP:
                self.astack.append(False)  # File:/Category:/... -> plain text, no link
                return
            tid = self.title2id.get(wt)
            if tid is None:
                tid = self.norm2id.get(_norm_title(wt))
            if tid is None and self.gmap is not None:
                tid = self.gmap.get(_norm_ws(wt).lower())
            if tid is not None:
                self.out.append(
                    f'<a href="/article/{int(tid)}" class="wl" data-id="{int(tid)}">'
                )
                self.anchor_depth += 1
                self.astack.append(True)
                return
            self.out.append(
                f'<a class="wl-title" data-title="{html.escape(wt, quote=True)}">'
            )
            self.anchor_depth += 1
            self.astack.append(True)
            return
        self.astack.append(False)

    def result(self):
        return "".join(self.out)


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
LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>TriDB Wiki — a tri-modal database, demonstrated on all of Wikipedia</title>
<meta name="description" content="A private Wikipedia running on TriDB — a vector + graph + relational database in one Postgres process. Search by meaning, traverse the link graph, find the path between any two topics, ask questions. Wikipedia is the demonstration; Wikidata is next." />
<style>
  :root{
    --ink:#202122; --soft:#54595d; --dim:#72777d; --line:#e4e6eb; --line2:#eef0f2;
    --bg:#ffffff; --bg2:#f8f9fa; --link:#3366cc; --link2:#2a4b8d;
    --v:#0f8b7c;   /* vector — teal   */
    --g:#6f4fd0;   /* graph  — violet */
    --r:#b8790f;   /* rel    — amber  */
    --maxw:900px;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  a{color:var(--link);text-decoration:none}
  a:hover{text-decoration:underline}
  .serif{font-family:"Linux Libertine",Georgia,"Times New Roman",serif}
  .wrap{max-width:var(--maxw);margin:0 auto;padding:0 22px}
  .mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}

  /* ---- hero / portal ---- */
  .portal{padding:54px 0 26px;text-align:center}
  .globe{width:96px;height:96px;margin:0 auto 14px;display:block}
  .word{font-size:clamp(40px,8vw,64px);font-weight:400;letter-spacing:-.5px;line-height:1}
  .word b{font-weight:700}
  .tag{color:var(--soft);font-size:15px;margin-top:8px;font-style:italic}
  .tag .sep{color:var(--line);margin:0 8px}
  .intro{max-width:60ch;margin:22px auto 0;color:var(--soft);font-size:16px}
  .intro b{color:var(--ink)}

  /* ---- central search ---- */
  form.search{max-width:620px;margin:30px auto 6px;display:flex;position:relative}
  form.search input{flex:1;font-size:17px;padding:13px 16px;border:1px solid #a2a9b1;
    border-right:0;border-radius:3px 0 0 3px;outline:none}
  form.search input:focus{border-color:var(--link);box-shadow:inset 0 0 0 1px var(--link)}
  form.search button{border:1px solid var(--link);background:var(--link);color:#fff;
    font-size:16px;font-weight:600;padding:0 22px;border-radius:0 3px 3px 0;cursor:pointer}
  form.search button:hover{background:var(--link2);border-color:var(--link2)}
  .enter{margin-top:12px;font-size:14px;color:var(--dim)}
  .enter a{font-weight:600}
  .modes{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;margin:22px auto 0;
    color:var(--soft);font-size:13.5px}
  .modes span{display:inline-flex;align-items:center;gap:7px}
  .sw{width:9px;height:9px;border-radius:50%;display:inline-block}

  /* ---- section chrome ---- */
  section{border-top:1px solid var(--line);padding:40px 0}
  h2{font-size:22px;font-weight:600;margin:0 0 6px}
  h2 .k{font:600 11px/1 ui-monospace,monospace;letter-spacing:.14em;text-transform:uppercase;
    color:var(--dim);display:block;margin-bottom:8px}
  .lead{color:var(--soft);max-width:70ch;margin:0 0 18px}

  .why3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:22px}
  .why3 .c{border:1px solid var(--line);border-radius:8px;padding:16px;background:var(--bg2)}
  .why3 .c h3{font-size:15px;margin:0 0 6px;display:flex;align-items:center;gap:8px}
  .why3 .c p{margin:0;color:var(--soft);font-size:14px}

  /* ---- comparison table ---- */
  .tablewrap{overflow-x:auto;margin-top:20px;border:1px solid var(--line);border-radius:8px}
  table{border-collapse:collapse;width:100%;min-width:640px;font-size:14.5px}
  thead th{text-align:left;padding:13px 16px;background:var(--bg2);border-bottom:2px solid var(--line);
    font-weight:600;font-size:13px}
  thead th.tri{color:var(--ink)}
  thead th.tri .badge{display:inline-block;width:8px;height:8px;border-radius:50%;
    background:linear-gradient(135deg,var(--v),var(--g),var(--r));margin-right:6px;vertical-align:middle}
  tbody td{padding:11px 16px;border-bottom:1px solid var(--line2);vertical-align:top}
  tbody tr:last-child td{border-bottom:0}
  td.dim{color:var(--dim)}
  td.win{color:var(--ink)}
  td.win b{color:var(--v)}
  .yes{color:var(--v);font-weight:600}
  .no{color:var(--dim)}
  .note{margin-top:14px;font-size:13.5px;color:var(--soft);border-left:3px solid var(--r);
    background:var(--bg2);padding:12px 16px;border-radius:0 6px 6px 0}
  .note b{color:var(--ink)}

  /* ---- proof stats ---- */
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}
  .stat{border:1px solid var(--line);border-radius:8px;padding:16px;background:var(--bg2)}
  .stat .n{font-size:26px;font-weight:700;letter-spacing:-.02em}
  .stat .n small{font-size:13px;color:var(--soft);font-weight:600}
  .stat .l{color:var(--soft);font-size:13px;margin-top:6px}

  footer{border-top:1px solid var(--line);padding:26px 0 60px;text-align:center;color:var(--dim);font-size:13.5px}
  footer .links{display:flex;gap:20px;justify-content:center;flex-wrap:wrap;margin-bottom:12px}
  footer .links a{color:var(--link)}

  @media(max-width:720px){.why3,.stats{grid-template-columns:1fr}}
</style>
</head>
<body>

<div class="wrap portal">
  <!-- tri-modal globe: meridians/parallels + three colored arcs (vector/graph/relational) -->
  <svg class="globe" viewBox="0 0 100 100" aria-hidden="true">
    <circle cx="50" cy="50" r="46" fill="#fff" stroke="#c8ccd1" stroke-width="1.5"/>
    <g fill="none" stroke="#dadde1" stroke-width="1">
      <ellipse cx="50" cy="50" rx="46" ry="17"/>
      <ellipse cx="50" cy="50" rx="46" ry="33"/>
      <ellipse cx="50" cy="50" rx="17" ry="46"/>
      <ellipse cx="50" cy="50" rx="33" ry="46"/>
      <line x1="4" y1="50" x2="96" y2="50"/><line x1="50" y1="4" x2="50" y2="96"/>
    </g>
    <path d="M50 4 A46 46 0 0 1 89.8 27" fill="none" stroke="var(--v)" stroke-width="3.5" stroke-linecap="round"/>
    <path d="M89.8 73 A46 46 0 0 1 50 96" fill="none" stroke="var(--g)" stroke-width="3.5" stroke-linecap="round"/>
    <path d="M10.2 73 A46 46 0 0 1 10.2 27" fill="none" stroke="var(--r)" stroke-width="3.5" stroke-linecap="round"/>
  </svg>

  <div class="word serif">Tri<b>DB</b> <span style="font-weight:400">Wiki</span></div>
  <div class="tag serif">A tri-modal database <span class="sep">·</span> demonstrated on all of Wikipedia</div>

  <p class="intro">
    A <b>private Wikipedia</b> running on TriDB — a database that fuses <b>vector similarity</b>,
    <b>graph traversal</b>, and <b>relational filters</b> inside one Postgres process, under one
    write-ahead log. Search by meaning, walk the link graph, find the path between any two topics,
    ask questions — in a single early-terminating query.
  </p>

  <form class="search" action="/read" method="get" role="search">
    <input name="q" placeholder="Search 6.9 million articles — by title or by meaning" aria-label="Search the wiki" autofocus>
    <button type="submit">Search</button>
  </form>
  <div class="enter"><a href="/read">Enter the wiki &rarr;</a> &nbsp;·&nbsp; no keyword needed — it understands meaning</div>

  <div class="modes">
    <span><i class="sw" style="background:var(--v)"></i>Vector — similarity</span>
    <span><i class="sw" style="background:var(--g)"></i>Graph — traversal</span>
    <span><i class="sw" style="background:var(--r)"></i>Relational — filter</span>
  </div>
</div>

<!-- WHY WIKI -->
<section>
  <div class="wrap">
    <h2><span class="k">Why Wikipedia is the demonstration</span>The perfect tri-modal proving ground.</h2>
    <p class="lead">
      A database that unifies similarity, traversal, and filter needs data that is all three at once.
      Wikipedia is exactly that — and at a scale that makes the fusion matter.
    </p>
    <div class="why3">
      <div class="c"><h3><i class="sw" style="background:var(--v)"></i>Meaning</h3>
        <p>Every article is text — an embedding. "Find articles like this one," not just string matches.</p></div>
      <div class="c"><h3><i class="sw" style="background:var(--g)"></i>Links</h3>
        <p>~6.9M articles woven by hundreds of millions of hyperlinks — a native graph to traverse and path-find.</p></div>
      <div class="c"><h3><i class="sw" style="background:var(--r)"></i>Structure</h3>
        <p>Infobox facts, categories, lengths, in-degree — relational predicates to filter and rank by.</p></div>
    </div>
    <div class="note">
      Wikipedia proves the fusion at <b>6.9M articles</b>. The next proving ground is <b>Wikidata</b> —
      ~110M entities joined by ~1.5B <em>typed</em> statements, ~16&times; larger, and edited millions of
      times a day (a live consistency workload). <a href="https://github.com/ConsultingFuture4200/tridb">See the roadmap &#8599;</a>
    </div>
  </div>
</section>

<!-- COMPARISON -->
<section>
  <div class="wrap">
    <h2><span class="k">Official Wikipedia vs the TriDB Wiki</span>Same articles. A different engine underneath.</h2>
    <p class="lead">
      This isn't a faster way to load a page — Wikimedia's global CDN is excellent at that. It's a
      knowledge engine: things the official reader can't do in one step, done in one fused query.
    </p>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th>Capability</th>
          <th>Official Wikipedia</th>
          <th class="tri"><span class="badge"></span>TriDB Wiki</th>
        </tr></thead>
        <tbody>
          <tr><td>Article page delivery</td><td class="dim">Global CDN — needs a network round-trip</td><td class="win">Local / offline — <b>zero network RTT</b> on the host</td></tr>
          <tr><td>Search</td><td class="dim">Keyword full-text (Elasticsearch)</td><td class="win">Full-text <b>+ semantic vector similarity</b></td></tr>
          <tr><td>Related articles</td><td class="dim">"What links here" — raw link list</td><td class="win">Fused <b>vector + graph</b>, relevance-ranked, early-terminating</td></tr>
          <tr><td>Path between two topics</td><td class="no">Not built in</td><td class="win"><span class="yes">Native shortest-path</span> ("Connect")</td></tr>
          <tr><td>Ask a question (RAG)</td><td class="no">Not available</td><td class="win"><span class="yes">Graph-aware RAG</span> over the articles</td></tr>
          <tr><td>Structured filters</td><td class="no">Not in the reader</td><td class="win">Relational predicates — in-degree, length, category</td></tr>
          <tr><td>Cross-modal update consistency</td><td class="dim">n/a (single modality)</td><td class="win">One WAL, atomic — <b>0 torn vs 42</b> across a 3-store stack</td></tr>
          <tr><td>Private &amp; offline</td><td class="no">Public, online only</td><td class="win"><span class="yes">Self-hosted</span>, private, fully offline</td></tr>
          <tr><td>Scale demonstrated</td><td class="dim">~6.9M (English)</td><td class="win">6.9M today &rarr; <b>Wikidata 110M</b> next</td></tr>
        </tbody>
      </table>
    </div>
    <div class="note">
      <b>On raw speed:</b> we don't claim to out-serve Wikipedia's cached HTML. Where TriDB wins is the
      <em>knowledge</em> query — semantic related, multi-hop paths, fused RAG — which the fused operator
      returns while examining as little as <b>~0.71%</b> of the corpus. A real page-load and search
      head-to-head will be <em>measured</em> and posted here, not asserted.
    </div>
  </div>
</section>

<!-- EVIDENCE -->
<section>
  <div class="wrap">
    <h2><span class="k">Measured, not asserted</span>The engine underneath, in numbers.</h2>
    <div class="stats">
      <div class="stat"><div class="n">0 <small>vs 42</small></div><div class="l">Torn cross-modal writes under injected failure — one transaction vs three independent stores.</div></div>
      <div class="stat"><div class="n">+15.6 <small>pts</small></div><div class="l">Multi-hop joint recall@5 when the graph leg injects source-anchored context into retrieval (HotpotQA).</div></div>
      <div class="stat"><div class="n">~0.71<small>%</small></div><div class="l">Share of the corpus examined while the streaming fused operator beat a full blocking oracle on recall.</div></div>
    </div>
  </div>
</section>

<footer>
  <div class="wrap">
    <div class="links">
      <a href="/read">Enter the wiki</a>
      <a href="https://github.com/ConsultingFuture4200/tridb">GitHub &#8599;</a>
      <a href="#">Architecture</a>
      <a href="#">Why Wikidata</a>
    </div>
    <div>TriDB — vector, graph, and relational retrieval fused in one Postgres process, under one write-ahead log. Wikipedia is the demonstration.</div>
  </div>
</footer>

</body>
</html>
"""


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Offline Wikipedia</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="wr-token" content="">
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
/* --- structured-HTML article rendering (Wikipedia-style) --- */
#article .infobox { float:right; clear:right; width:300px; max-width:44%; margin:2px 0 14px 18px;
  border:1px solid #a2a9b1; background:#f8f9fa; font-size:12.5px; line-height:1.5; border-collapse:collapse; }
#article .infobox tr > th[colspan], #article .infobox caption { font-weight:700; text-align:center;
  background:#eaecf0; padding:6px; font-size:13.5px; }
#article .infobox td, #article .infobox th { border:0; padding:3px 8px; vertical-align:top; text-align:left; }
#article .infobox-label { font-weight:600; padding-right:8px; white-space:nowrap; }
#article .infobox img, #article figure img, #article .thumb img { max-width:100%; height:auto; }
#article .hatnote { font-style:italic; color:#54595d; padding:2px 0 4px 20px; font-size:13.5px; }
#article .thumb { border:1px solid #c8ccd1; background:#f8f9fa; padding:4px; margin:6px 0 6px 14px;
  max-width:320px; float:right; clear:right; }
#article .thumbcaption, #article figcaption { font-size:12px; color:#54595d; padding:3px 2px 0; }
#article .references, #article .reflist, #article ol.references { font-size:12px; color:#444; }
#article ol.references li { margin:2px 0; }
#article sup.reference, #article sup { font-size:.72em; line-height:0; }
#article table:not(.infobox) { border-collapse:collapse; margin:10px 0; font-size:13.5px; max-width:100%; }
#article table:not(.infobox) th, #article table:not(.infobox) td { border:1px solid #ccd; padding:4px 8px;
  text-align:left; vertical-align:top; }
#article table:not(.infobox) th { background:#f4f6fb; }
#article .mw-editsection, #article .Z3988, #article .mw-cite-backlink, #article .noprint,
#article .mw-empty-elt, #article style { display:none; }
#article img { max-width:100%; height:auto; }
#article a.wl-title { color:#36c; text-decoration:none; cursor:pointer; border-bottom:1px dotted #9bb; }
#article a.wl-title:hover { background:#eef3ff; }
#article blockquote { border-left:3px solid #ddd; margin:8px 0; padding:2px 12px; color:#555; }
/* collapsible section headers */
#artbody h2.collap, #artbody h3.collap { cursor:pointer; user-select:none;
  border-bottom:1px solid #a2a9b1; padding-bottom:3px; }
#artbody h2.collap:hover, #artbody h3.collap:hover { color:#36c; }
#artbody .ctri { display:inline-block; width:14px; color:#888; font-size:.75em; vertical-align:middle; }
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
#tribar { display:flex; gap:8px; align-items:center; padding:6px 16px;
  background:#eafaf1; border-bottom:1px solid #cdeede; flex-wrap:wrap; }
#tribar input { padding:6px 10px; border:1px solid #bfe3cd; border-radius:4px;
  font-size:13px; background:#fff; }
#tribar input.numf { width:90px; }
#tribar .tlbl { font-weight:600; color:#127a4a; font-size:13px; white-space:nowrap; }
#tribar label { font-size:12.5px; color:#555; display:flex; align-items:center; gap:4px; }
#tribar button { padding:6px 16px; border:0; border-radius:4px; background:#127a4a; color:#fff;
  cursor:pointer; font-size:13px; }
#tribar button:hover { background:#0e6a3f; }
.prov { font-size:10.5px; color:#127a4a; margin-left:6px; font-weight:600; }
.ex { display:inline-block; width:15px; height:15px; line-height:15px; text-align:center;
  border-radius:50%; background:rgba(255,255,255,.30); color:#fff; font-size:11px; font-weight:700;
  cursor:help; position:relative; margin-left:5px; font-style:normal; vertical-align:middle; }
#connectbar .ex, #sembar .ex, #tribar .ex { background:#c7d2e6; color:#3355bb; }
.ex .win { display:none; position:absolute; top:20px; left:0; width:300px; background:#fff; color:#222;
  border:1px solid #d0d4da; border-radius:8px; padding:11px 13px; box-shadow:0 6px 22px rgba(0,0,0,.16);
  z-index:60; font-size:12.5px; line-height:1.55; font-weight:400; text-transform:none;
  letter-spacing:normal; text-align:left; white-space:normal; }
.ex:hover .win { display:block; }
.ex .win b { color:#111; }
.ex .win code { background:#f2f2f4; border-radius:3px; padding:0 4px; font-size:11.5px; }
.ex .win .legs { margin-top:7px; }
.ex .win .legs i { font-style:normal; padding:1px 7px; border-radius:10px; margin-right:4px;
  color:#fff; font-size:10.5px; }
.leg-v { background:#0f8b7c; } .leg-g { background:#6f4fd0; } .leg-r { background:#b8790f; }
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
#article a.wl-title { color:#36c; text-decoration:none; cursor:pointer; border-bottom:1px dotted #9bb; }
#article a.wl-title:hover { background:#eef3ff; }
#article table { border-collapse:collapse; margin:10px 0; font-size:13.5px; max-width:100%; }
#article th, #article td { border:1px solid #ccd; padding:4px 8px; text-align:left; vertical-align:top; }
#article th { background:#f4f6fb; }
#article blockquote { border-left:3px solid #ddd; margin:8px 0; padding:2px 12px; color:#555; }
#article figure { margin:8px 0; } #article figcaption { font-size:12px; color:#777; }
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
  <a href="/" title="TriDB — portal home" style="color:#fff;font-weight:700;text-decoration:none;white-space:nowrap;font-size:15px">TriDB</a>
  <button id="back" onclick="goBack()" title="Back (also works with the browser Back button)">&larr; Back</button>
  <button id="home" onclick="goHome()" title="Home — jump to a fresh random article">&#127968; Home</button>
  <h1 onclick="goHome()" title="Home — a fresh random article">Offline Wikipedia</h1>
  <input id="q" placeholder="Search titles (e.g. Ada Lovelace) — Enter" autofocus><span class="ex">&#9432;<span class="win"><b>Title search.</b> Matches your text against article <em>titles</em> (full-text index) — fast exact/prefix lookup. Use it when you know the name. Press Enter.</span></span>
  <input id="ask" placeholder="Ask a question (RAG over 6.9M articles) — Enter"><span class="ex">&#9432;<span class="win"><b>Ask (RAG).</b> Type a natural-language <em>question</em>: it retrieves relevant passages and a local LLM writes a <em>cited</em> answer. The <b>graph</b> box expands along links first, so multi-hop questions are grounded in the link chain.<span class="legs"><i class="leg-v">vector</i><i class="leg-g">graph</i></span></span></span>
  <label class="gexp" title="Graph-aware RAG: expand along hyperlinks before answering">
    <input type="checkbox" id="gexp" checked> graph
  </label>
</header>
<div id="connectbar">
  <span class="cblabel">How are these connected?<span class="ex">&#9432;<span class="win"><b>Connect.</b> Finds the shortest <em>path</em> of links between two articles — pure graph traversal over the 448M-edge adjacency. Great for &ldquo;how is X related to Y?&rdquo;.<span class="legs"><i class="leg-g">graph</i></span></span></span></span>
  <input id="from" placeholder="From (e.g. Ada Lovelace)">
  <span class="cbarrow">&rarr;</span>
  <input id="to" placeholder="To (e.g. Charles Babbage)">
  <button id="cbtn" onclick="connect()">Connect</button>
</div>
<div id="sembar">
  <span class="cblabel">Semantic search<span class="ex">&#9432;<span class="win"><b>Semantic search.</b> Finds articles by <em>meaning</em> (vector similarity), then prunes by relational filters (inbound links / length / category). Semantic first, filter second.<span class="legs"><i class="leg-v">vector</i><i class="leg-r">relational</i></span></span></span></span>
  <input id="sq" placeholder="Meaning query (e.g. quantum computing)">
  <input id="minlinks" class="numf" type="number" min="0" placeholder="min links">
  <input id="minlen" class="numf" type="number" min="0" placeholder="min chars">
  <input id="maxlen" class="numf" type="number" min="0" placeholder="max chars">
  <input id="scat" placeholder="category contains…">
  <button id="sbtn" onclick="semSearch()">Search</button>
</div>
<div id="tribar">
  <span class="tlbl">Tri-modal<span class="ex">&#9432;<span class="win"><b>Tri-modal search.</b> All three TriDB legs in one query: seed by <em>meaning</em>, walk the <em>link graph</em> out of those seeds, then <em>filter</em> — the reader-side mirror of the engine's <code>tjs_open</code>. Results tag their provenance (meaning / linked / both).<span class="legs"><i class="leg-v">vector</i><i class="leg-g">graph</i><i class="leg-r">relational</i></span></span></span></span>
  <input id="tq" placeholder="Meaning + links + filters (e.g. cryptography)">
  <label title="Expand along out-links from the semantic seeds (the graph leg)"><input type="checkbox" id="texpand" checked> expand links</label>
  <input id="tminlinks" class="numf" type="number" min="0" placeholder="min links">
  <input id="tminlen" class="numf" type="number" min="0" placeholder="min chars">
  <input id="tmaxlen" class="numf" type="number" min="0" placeholder="max chars">
  <input id="tcat" placeholder="category contains…">
  <button id="tbtn" onclick="triSearch()">Search</button>
</div>
<main>
  <div id="results"><div class="hint">Type a query and press Enter.</div></div>
  <div id="article"><div class="hint">Select an article.</div></div>
  <div id="related"></div>
</main>
<div id="hovercard"></div>
<script>
const $ = s => document.querySelector(s);
function wrToken(){ const m = document.querySelector('meta[name="wr-token"]'); return m ? m.content : ''; }
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
function makeCollapsible(){
  document.querySelectorAll('#artbody h2, #artbody h3').forEach(hd => {
    if(hd.dataset.collap) return;
    hd.dataset.collap = '1'; hd.classList.add('collap');
    hd.insertAdjacentHTML('afterbegin', '<span class="ctri">&#9662;</span> ');
    hd.addEventListener('click', () => toggleSection(hd));
  });
}
function toggleSection(hd){
  const collapsed = hd.classList.toggle('collapsed');
  const tri = hd.querySelector('.ctri'); if(tri) tri.innerHTML = collapsed ? '&#9656;' : '&#9662;';
  const stop = (hd.tagName === 'H3') ? ['H2','H3'] : ['H2'];
  let el = hd.nextElementSibling;
  while(el){ if(stop.includes(el.tagName)) break; el.style.display = collapsed ? 'none' : ''; el = el.nextElementSibling; }
}
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
    '<div id="artbody">'+body+'</div>';
  art.scrollTop = 0;
  makeCollapsible();
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
    headers:{'Content-Type':'application/json', 'X-TriDB-Token':wrToken()},
    body:JSON.stringify(payload) })).json(); }
  catch(e){ return; }
  const row = $('#sug'+i); if(row) row.style.display = 'none';
  if(r.facts) $('#enrichaccepted').innerHTML = renderEnrichments(r.facts);
}
async function dismissEnrich(id, i){
  const f = _enrichData[i]; if(!f) return;
  try { await fetch('/enrich/dismiss', { method:'POST',
    headers:{'Content-Type':'application/json', 'X-TriDB-Token':wrToken()},
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

// -- Tri-modal search: vector seed -> graph expand -> relational filter (reader-side tjs_open) --
function triParams(){
  return { q: $('#tq').value.trim(), expand: $('#texpand').checked ? 1 : 0,
    min_indeg: $('#tminlinks').value.trim(), min_len: $('#tminlen').value.trim(),
    max_len: $('#tmaxlen').value.trim(), cat: $('#tcat').value.trim() };
}
async function triSearch(){
  const p = triParams();
  if(!p.q) return;
  await loadTri(p);
  pushView({view:'tri', p:p});
}
async function loadTri(p){
  $('#tq').value=p.q||''; $('#texpand').checked = p.expand!=0;
  $('#tminlinks').value=p.min_indeg||''; $('#tminlen').value=p.min_len||'';
  $('#tmaxlen').value=p.max_len||''; $('#tcat').value=p.cat||'';
  const el = $('#results');
  el.innerHTML = '<div class="hint">Vector seed &rarr; graph expand &rarr; relational filter…</div>';
  const qs = '/search_trimodal?q='+encodeURIComponent(p.q)+
    '&expand='+encodeURIComponent(p.expand)+
    '&min_indeg='+encodeURIComponent(p.min_indeg||0)+
    '&min_len='+encodeURIComponent(p.min_len||0)+
    '&max_len='+encodeURIComponent(p.max_len||0)+
    '&cat='+encodeURIComponent(p.cat||'');
  let r;
  try { r = await j(qs); } catch(e){ el.innerHTML='<div class="hint">Search failed.</div>'; return; }
  renderTri(r);
}
function triRow(a){
  const meta=[a.indeg.toLocaleString()+' links'];
  if(a.length!=null) meta.push(a.length.toLocaleString()+' chars');
  const cats = (a.cats&&a.cats.length)
    ? '<div>'+a.cats.map(c=>'<span class="catpill">'+esc(c)+'</span>').join('')+'</div>' : '';
  return '<div class="item" onclick="open_('+a.id+')"><div class="rtitle">'+esc(a.title)+
    '<span class="prov">'+esc(a.prov)+'</span></div>'+
    relInd(a.cos)+'<div class="meta">'+meta.join(' · ')+'</div>'+cats+'</div>';
}
function renderTri(r){
  const el = $('#results');
  let h = '<div class="countbar"><b>'+r.seed_count+'</b> seeds by meaning &rarr; <b>'+
    r.expanded_count+'</b> via links &rarr; <b>'+r.pre_count+'</b> pooled &rarr; <b>'+
    r.post_count+'</b> after filter<br><span style="color:#999">filters: '+
    esc(fdesc(r.filters, r.cats_available))+'</span></div>';
  if(!r.results.length){ el.innerHTML = h+'<div class="hint">No results after filtering.</div>'; return; }
  el.innerHTML = h + r.results.map(triRow).join('');
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
$('#article').addEventListener('click', e => {   // unresolved html links -> title search, open top hit
  const a = e.target.closest('a.wl-title');
  if(!a) return;
  e.preventDefault(); hideHover();
  const t = a.getAttribute('data-title') || '';
  loadSearch(t).then(() => { const top = document.querySelector('#results .item'); if(top) top.click(); });
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
  else if(st.view==='tri'){ loadTri(st.p); }
  else if(st.view==='search'){ loadSearch(st.q); }
  updateBack();
}
window.addEventListener('popstate', e => renderState(e.state));
const _bootQ = new URLSearchParams(location.search).get('q');   // capture BEFORE replaceState strips ?q=
history.replaceState({view:'home'}, '', location.pathname);   // base state (drops ?q= from the URL)
updateBack();
(function(){   // boot: a ?q= handoff from the portal landing runs the search and opens the top hit; else random landing
  if(_bootQ){ $('#q').value = _bootQ; loadSearch(_bootQ).then(() => { const top = document.querySelector('#results .item'); if(top) top.click(); }); }
  else { loadHome(); }
})();

$('#q').addEventListener('keydown', e => { if(e.key==='Enter') search(); });
$('#ask').addEventListener('keydown', e => { if(e.key==='Enter') ask(); });
$('#from').addEventListener('keydown', e => { if(e.key==='Enter') connect(); });
$('#to').addEventListener('keydown', e => { if(e.key==='Enter') connect(); });
['#sq','#minlinks','#minlen','#maxlen','#scat'].forEach(sel =>
  $(sel).addEventListener('keydown', e => { if(e.key==='Enter') semSearch(); }));
[['#tq','#tminlinks','#tminlen','#tmaxlen','#tcat']][0].forEach(sel =>
  $(sel).addEventListener('keydown', e => { if(e.key==='Enter') triSearch(); }));
</script>
</body></html>"""


MAX_BODY_BYTES = 64 * 1024  # cap mutating POST bodies (accept/dismiss payloads are small)


def check_token(headers, expected: str) -> bool:
    """True iff `headers` (dict-like, `.get(name, default)`) carries `expected` via
    the `X-TriDB-Token` header or an `Authorization: Bearer <token>` header."""
    if not expected:
        return False
    supplied = headers.get("X-TriDB-Token", "") or ""
    if not supplied:
        auth = headers.get("Authorization", "") or ""
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer ") :]
    return supplied == expected


def parse_body(raw: bytes, max_len: int = MAX_BODY_BYTES) -> dict:
    """Parse a JSON POST body, rejecting oversized payloads.

    Raises ValueError if `raw` exceeds `max_len` bytes."""
    if len(raw) > max_len:
        raise ValueError(f"body exceeds {max_len} bytes")
    return json.loads(raw or b"{}")


def make_handler(reader: Reader, token: str):
    index_html = INDEX_HTML.replace(
        '<meta name="wr-token" content="">',
        f'<meta name="wr-token" content="{html.escape(token)}">',
    )

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
                    self._send(200, LANDING_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path in ("/read", "/read/"):
                    self._send(200, index_html.encode("utf-8"), "text/html; charset=utf-8")
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
                elif path == "/search_trimodal":
                    qs = parse_qs(u.query)

                    def _ti(name: str, dv: int = 0) -> int:
                        try:
                            return int(qs.get(name, [str(dv)])[0] or dv)
                        except ValueError:
                            return dv

                    self._json(
                        reader.search_trimodal(
                            qs.get("q", [""])[0],
                            seed=_ti("seed", 40) or 40,
                            expand=qs.get("expand", ["1"])[0] != "0",
                            min_indeg=_ti("min_indeg"),
                            min_len=_ti("min_len"),
                            max_len=_ti("max_len"),
                            cat=qs.get("cat", [""])[0].strip(),
                        )
                    )
                elif path.startswith("/article/"):
                    aid = int(unquote(path.split("/")[-1]))
                    art = reader.article(aid, with_html=True)
                    if art is None:
                        self._json({"error": "not found"})
                    else:
                        if art.get("html"):
                            try:
                                art["body_html"] = reader.render_html_body(aid, art["html"])
                            except Exception:
                                art["body_html"] = reader.link_body(aid, art["body"])
                        else:
                            art["body_html"] = reader.link_body(aid, art["body"])
                        art.pop("html", None)
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
                if length > MAX_BODY_BYTES:
                    self._send(413, b"payload too large", "text/plain")
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    body = parse_body(raw)
                except ValueError:
                    self._send(413, b"payload too large", "text/plain")
                    return
                if u.path in ("/enrich/accept", "/enrich/dismiss") and not check_token(
                    self.headers, token
                ):
                    self._send(401, b"unauthorized", "text/plain")
                    return
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
    token = os.environ.get("WIKI_READER_TOKEN") or secrets.token_urlsafe(24)
    if not os.environ.get("WIKI_READER_TOKEN"):
        print(f"[serve] auth token (mutating POSTs): {token}")
    httpd = ThreadingHTTPServer((host, port), make_handler(reader, token))
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
    sp.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit a non-loopback --host (fail-closed by default)",
    )
    args = ap.parse_args(argv)

    if not (args.corpus / "manifest.json").exists():
        ap.error(f"no manifest.json under {args.corpus}")

    if (
        args.cmd == "serve"
        and args.host not in ("127.0.0.1", "::1", "localhost")
        and not args.allow_remote
    ):
        ap.error(
            f"--host {args.host!r} is non-loopback; pass --allow-remote to confirm "
            "intentional remote exposure"
        )

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

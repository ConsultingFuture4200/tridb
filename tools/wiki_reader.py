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
main { display:grid; grid-template-columns:260px 1fr 300px; gap:0; height:calc(100vh - 52px); }
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
</style></head>
<body>
<header>
  <h1>Offline Wikipedia</h1>
  <input id="q" placeholder="Search titles (e.g. Ada Lovelace) — Enter" autofocus>
  <input id="ask" placeholder="Ask a question (RAG over 6.9M articles) — Enter">
</header>
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
const RELFOOT = '<div class="foot">&ldquo;Related by meaning&rdquo; uses AI embeddings; '+
  '&ldquo;Linked articles&rdquo; uses Wikipedia&rsquo;s own hyperlinks.</div>';

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
  const r = await j('/related/'+id);
  let h = '<div class="hd">Related by meaning</div>' +
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
    sp = sub.add_parser("serve", help="serve the reader")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)

    if not (args.corpus / "manifest.json").exists():
        ap.error(f"no manifest.json under {args.corpus}")

    if args.cmd == "build":
        cmd_build(args.corpus)
    else:
        cmd_serve(args.corpus, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

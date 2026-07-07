# Offline Wikipedia Reader — v0.1.0

DEV-1354 action #3. A single-user, fully-offline browsable Wikipedia reader over
the enwiki corpus already staged on the Spark box. Scope is **review + search +
related**. No LLM / RAG — that is a deferred follow-up.

One tool, `tools/wiki_reader.py`, with two subcommands (`build`, `serve`). The
frontend is a single self-contained HTML string served by a stdlib
`http.server` app — no external CDNs, works with the network fully off.

## What it does

- **Title search** — FTS5 over article titles (prefix-matched last token).
- **Article view** — fetches the body from the JSONL shards in O(1) via a byte
  offset recorded at build time (no 30 GB scan, no corpus in RAM).
- **Related — semantic** — cuVS CAGRA nearest neighbours over the 6.9M BGE
  (`bge-small-en-v1.5`, dim 384) vectors, reusing
  `tools/wiki_linkpredict.build_cuvs_index`.
- **Related — hyperlinks** — an article's out-going wikilinks, from a CSR
  adjacency built from the edge TSV shards (capped at 25 for display).

## Data it reads (all under `data/wiki/enwiki/`, READ-ONLY corpus)

- `articles-*.jsonl` — article text (`{id, title, text, ts}` per line).
- `edges-*.tsv` — directed `src<TAB>dst` article-id hyperlinks (~224M).
- `emb/vectors.f32` — 6,900,039 × 384 float32, L2-normalized (memmapped).
- `emb/ids.i64.npy` — embedding row → article id.
- `emb/meta.json`, `manifest.json` — metadata.

## Artifacts it builds (next to the corpus; gitignored, live only on Spark)

| File | What |
|---|---|
| `reader.db` | sqlite: `articles(id, title, shard, byte_offset)` + `titles_fts` (FTS5) |
| `id2row.i32.npy` | article id → embedding row (reverse of `ids.i64.npy`) |
| `edges_csr_dst.i32.npy` | CSR values: out-edge dst ids grouped by src |
| `edges_csr_off.i64.npy` | CSR offsets: out-edges of `id` = `dst[off[id]:off[id+1]]` |

## Run it (on Spark)

```bash
cd ~/code/tridb && . .venv/bin/activate

# 1. Build the index (one-time; scans the shards + edges). Long-running.
python3 tools/wiki_reader.py build

# 2. Serve (binds 127.0.0.1 only). CAGRA index builds at startup (~49s).
nohup python3 tools/wiki_reader.py serve --port 8080 \
  > /tmp/wiki_reader.log 2>&1 & echo $! > /tmp/wiki_reader.pid
```

## Open it in a local browser (from your workstation)

```bash
ssh -L 8080:localhost:8080 spark
# then browse to:  http://localhost:8080
```

Leave that ssh session open for as long as you want the tunnel up.

## Deferred (not in v1)

- **Full-body FTS.** Only titles are indexed for search; semantic neighbours
  cover content-level discovery. Full-text search over bodies is heavier to
  build and was intentionally skipped.
- **LLM / RAG ("Ask").** No question-answering or retrieval-augmented
  generation. That is the follow-up track.
- **Redirect / disambiguation UX, incoming-link view, category browse.** Out of
  scope for a personal reader v1.

## Notes / rough edges

- A few article/edge shards were truncated by an earlier extractor run (~4% of
  articles lost, documented in DEV-1354). The build indexes whatever lines are
  actually present and tolerates a truncated final line, so the reader is
  self-consistent; some hyperlink/neighbour targets whose article is missing are
  simply dropped from the related lists.
- Body rendering does a light wikitext-residue cleanup only (drops image/thumb
  caption fragments); it is not a full wiki renderer.
- Single-user tool: sqlite and the CAGRA index are each guarded by one lock.

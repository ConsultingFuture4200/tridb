# Offline Wikipedia Reader — v0.1.0

DEV-1354 action #3. A single-user, fully-offline browsable Wikipedia reader over
the enwiki corpus already staged on the Spark box. Scope is **review + search +
related + ask (RAG)**. The Ask track (v2) is documented in the addendum below.

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

## Deferred (not in v1/v2)

- **Full-body FTS.** Only titles are indexed for search; semantic neighbours and
  the Ask retriever cover content-level discovery. Full-text search over bodies
  is heavier to build and was intentionally skipped.
- **Redirect / disambiguation UX, incoming-link view, category browse.** Out of
  scope for a personal reader.

---

# Addendum — v2: Ask (RAG)

A natural-language question box that retrieves over the 6.9M-article corpus and
answers with a small local LLM, citing the retrieved sources. Same
`tools/wiki_reader.py serve` process — it **reuses the already-loaded cuVS CAGRA
index and `reader.db`**; no second index is built.

## Pipeline

1. **Embed the question** with the *same* model the corpus was embedded with —
   `BAAI/bge-small-en-v1.5` (dim 384, normalized) via fastembed (CPU; onnxruntime
   has no aarch64 GPU wheel — fine for one query). The embedder is lazy-loaded on
   the first `/ask`, so `serve` startup stays ~49s.
2. **Retrieve** top-k (`k=8`) passages from the CAGRA index the serve process
   already holds. Rows → article ids → titles; bodies fetched from `reader.db`
   byte offsets and truncated to `PASSAGE_CHARS` (1500) each so k passages fit
   the LLM context. Missing articles (the ~4% clobbered shards) are skipped; the
   retriever over-fetches a few to still land k.
3. **Generate** with a small quantized instruct LLM served locally by **ollama**
   (`qwen2.5:7b-instruct`, Q4) over `localhost:11434`. The reader POSTs to
   `/api/chat` with `num_ctx=8192` (ollama otherwise defaults to 2048 and would
   truncate the passages), `temperature=0.2`.
4. **Grounded prompt** — system instruction: *answer ONLY from the provided
   passages, cite passage numbers like [1], say so if the answer isn't there, no
   outside knowledge* + the k numbered passages + the question. The response is
   returned with the ordered source list (each clickable to `/article/{id}`).

## Model & GPU footprint

| | |
|---|---|
| LLM | `qwen2.5:7b-instruct` (ollama, Q4_K_M, ~4.7 GB on disk) |
| Resident GPU | **~5.1 GB** (`ollama ps`) on the GB10 unified pool |
| CAGRA index (reader) | ~1.0 GB |
| Total | **~6.1 GB** of 128 GB — well under the ~15 GB budget so a later heavy job has the pool free |
| Idle behaviour | ollama auto-unloads the model after ~5 min idle (`keep_alive`), freeing the 5.1 GB until the next question |

`num_ctx=8192` at k=8×1500 chars leaves comfortable headroom.

## Endpoint & UI

- `GET /ask?q=...` → `{answer, sources:[{n,id,title,score}]}`.
- The single-page HTML gains an amber **Ask** box in the header (Enter to ask);
  the answer + clickable numbered sources render in the article pane. The
  existing title-search / article / related UI is unchanged. No external CDNs —
  fully offline.

## Run it (on Spark)

The `serve` command is unchanged; Ask activates automatically once the LLM is
available. One-time model pull:

```bash
ollama pull qwen2.5:7b-instruct     # ~4.7 GB; ollama server already runs on :11434
```

Optional overrides (env vars read at startup): `WIKI_ASK_MODEL` (default
`qwen2.5:7b-instruct`), `OLLAMA_URL` (default `http://127.0.0.1:11434`).

Use it exactly as v1: `ssh -L 8080:localhost:8080 spark`, then the Ask box at
`http://localhost:8080`.

## Stopping the LLM

The LLM is a separate ollama-managed process, not part of the reader. It unloads
itself after idle. To free it immediately:

```bash
ollama stop qwen2.5:7b-instruct     # unloads the model now (reader stays up)
```

Stopping the reader (`kill $(cat /tmp/wiki_reader.pid)`) does **not** stop
ollama; the two are independent.

## Honesty / guardrails

- The LLM answers from retrieved passages, not parametric memory; the prompt
  forbids outside knowledge and citations point to real retrieved article ids.
- If retrieval is empty the UI says so; if the LLM (ollama) is unreachable the
  answer field returns an explicit `[LLM unavailable: ...]` message with the
  valid sources still listed — it does not fabricate.

## Verified (Spark, 2026-07-07)

Three real questions via curl against the live serve process:

- *"Who is considered the first computer programmer?"* → "Ada Lovelace is
  considered the first computer programmer [7]." ([7] = article id 195, *Ada
  Lovelace*.)
- *"What is the capital of Bhutan?"* → "The capital of Bhutan is Thimphu [1]."
  ([1] = id 28752, *Thimphu*.)
- *"Who painted the Mona Lisa?"* → "…painted by the Italian artist Leonardo da
  Vinci [2]." ([2] = id 34643, *Mona Lisa*.)

## Still deferred

- **Query-side BGE instruction prefix.** The question is embedded with the same
  document-mode `.embed()` path as the corpus (symmetric, and retrieval is
  strong in practice); BGE's `"Represent this sentence for searching..."` query
  prefix was not added.
- **Streaming answers / multi-turn chat / answer caching.** Single-shot Q&A only.
- **Reranking / passage-level (sub-article) chunking.** Retrieval is whole-article
  (leading 1500 chars per hit), not sentence/paragraph chunks.

## Notes / rough edges

- A few article/edge shards were truncated by an earlier extractor run (~4% of
  articles lost, documented in DEV-1354). The build indexes whatever lines are
  actually present and tolerates a truncated final line, so the reader is
  self-consistent; some hyperlink/neighbour targets whose article is missing are
  simply dropped from the related lists.
- Body rendering does a light wikitext-residue cleanup only (drops image/thumb
  caption fragments); it is not a full wiki renderer.
- Single-user tool: sqlite and the CAGRA index are each guarded by one lock.

---

# Addendum — legibility polish for the related / sources panels (2026-07-07)

Client-side-only UX polish; no endpoints, artifacts, or retrieval logic changed.

- The two related lists are now unambiguously labelled with a one-line legend
  each: **"Related by meaning"** (semantic / cuVS neighbours) — *"how closely the
  topics match (embedding similarity)"* — and **"Linked articles"** (out-edges) —
  *"articles this page links to"*.
- Each semantic result now renders a small horizontal **bar** (CSS `width` % =
  the score) plus a plain-language **bucket word** instead of a bare float. The
  raw number is kept but demoted to a muted, small `.num` span and repeated in the
  row's `title=` tooltip.
- **Score semantics (verified).** `/related` and `/ask` return `score` =
  **cosine similarity** in `[0,1]`, higher = more related. Chain: the cuVS shim
  (`wiki_linkpredict._CuvsIndex.knn_query`) returns `sqeuclidean/2 = 1 - cos` on
  the unit vectors, and `semantic()`/`retrieve()` apply `1 - d = cos`, so a longer
  bar correctly means *more* related. Buckets: `>=0.85` near-identical,
  `0.75-0.85` very related, `0.60-0.75` related, `<0.60` loosely related.
- **/ask sources** use the same bar + bucket indicator (still clickable to
  `/article/{id}`).
- A one-line footer under the related lists explains the distinction once:
  *"Related by meaning uses AI embeddings; Linked articles uses Wikipedia's own
  hyperlinks."* Inline styles, self-contained, still fully offline.

---

# Addendum — connection finder + fused related (2026-07-07)

Two graph-forward features added to `tools/wiki_reader.py`. Both are self-
contained, fully offline (no CDNs), and reuse the already-loaded serve state
(cuVS CAGRA index, `reader.db`, the CSR adjacency). The legibility bars/buckets
above are preserved.

## Feature 1 — Connection finder ("How are A and B connected?")

Finds the **shortest hyperlink path** between two articles and renders it as a
clickable chain `A → n1 → … → B`.

- **Endpoint** `GET /path?from=<id|title>&to=<id|title>[&narrate=1]`.
  `resolve()` accepts a numeric article id, an exact title, or a free-text title
  query (best FTS hit) for each field, so the two form fields reuse title search.
- **Response** `{from:{id,title}, to:{id,title}, found:true, hops:N,
  path:[{id,title},…]}`, or `{found:false, reason:"…"}` when no path is found
  within the bound (or the undirected index is absent).
- **Algorithm** — **bidirectional BFS** over an **undirected** view of the graph
  (a link in either direction connects the two topics). Two frontiers grow
  alternately (always expanding the smaller one) until they meet. Bounded two
  ways so hub-heavy neighbourhoods stay fast: `max_hops=6` and `max_expand=400k`
  nodes touched across both sides. The path is reconstructed from the two parent
  maps at the meet node.
- **Undirected CSR** — the BFS needs both link directions, so `build` now emits an
  undirected CSR (`edges_undir_dst.i32.npy` + `edges_undir_off.i64.npy`) **derived
  from the existing directed CSR** — for every directed `(u,v)` we emit `(u,v)` and
  `(v,u)`, then re-group by src. No second pass over the 224M-edge TSV shards.
  Built once, mmap'd at serve startup (guarded: absent → `/path` returns a clear
  disabled message rather than crashing). A dedicated `build-undirected`
  subcommand rebuilds only this sidecar (bounded incremental build).
  - **Measured (Spark, real enwiki):** 448,950,566 half-edges (2× 224,475,283),
    max node id 7,189,650, **35.4 s**, sidecars **1,853 MB** (1.80 GB dst + 57 MB
    off), peak RSS ~11.5 GB. `serve` startup unchanged (~45–49 s, CAGRA-dominated).
- **Optional LLM narration** (secondary): with `&narrate=1` the resolved chain is
  sent to the same local ollama backend `/ask` uses for a one-paragraph plain-
  English explanation ("Explain this connection" button in the UI). Best-effort —
  the path is the deliverable; an ollama failure returns an explicit
  `[LLM unavailable: …]` note, never a fabrication.
- **UI** — a sticky sub-header bar with **From / → / To** fields + a **Connect**
  button (Enter also submits). The chain renders in the article pane as pill
  "chips", each clickable to `/article/{id}`.

## Feature 2 — Fused related (meaning × topology)

Adds a **primary "Related (fused)"** ranking to the article view that combines
semantic similarity with graph proximity, so articles that are BOTH semantically
near AND topologically close rank highest.

- **Endpoint** `GET /related_fused/{id}` →
  `{fused:[{id,title,rrf,prov,cos,cocite}], semantic:[…], hyperlinks:[…]}`. The
  `semantic` and `hyperlinks` arrays are the unchanged component breakdowns (the
  two existing labelled panels, bars intact) shown below the fused list.
- **Method** — **reciprocal-rank fusion (RRF)** of two rankings, consistent with
  `tools/wiki_linkpredict_fused.py`: `score(B) = 1/(rrf_k+rank_cos) +
  1/(rrf_k+rank_graph)` (`rrf_k=60`; a modality where B is absent contributes 0).
  - *cosine ranking* = the cuVS CAGRA semantic neighbours (`pool=50`).
  - *graph-proximity ranking* = 1-hop out-neighbours (directly linked) first, then
    2-hop neighbours ranked by **co-citation count** (how many of A's own out-links
    also point at them). The 2-hop fan-out is bounded (`cap_direct=300`,
    `cap_out=64`) so the call stays fast on hub pages.
- **Provenance tag** per fused result: **"meaning + linked"** (directly linked AND
  in the semantic pool — strongest), **"meaning only"** (semantic pool only),
  **"linked (1-hop)"** / **"linked (2-hop)"** (graph only). Rendered as a small
  coloured pill; the cosine bar shows when the item is semantically ranked, else a
  "co-cited ×N" badge.
- **Verified (Spark, Ada Lovelace / id 195):** the top of the fused list is all
  "meaning + linked" — *Charles Babbage*, *Analytical engine*, *Alan Turing*,
  *Lady Byron*, *Note G* — i.e. articles that are both semantically near and
  directly linked, exactly the dual-signal boost intended; a purely-linked entry
  ("linked (1-hop)", e.g. *The Right Honourable*) trails below.

## Endpoints (summary)

| Endpoint | Returns |
|---|---|
| `GET /path?from=&to=&narrate=` | shortest undirected hyperlink chain (+ optional LLM narration) |
| `GET /related_fused/{id}` | RRF(meaning, topology) ranking + provenance + component panels |

## Verified live (Spark, 2026-07-07, PID rotated cleanly)

- `/path?from=195&to=Charles%20Babbage` → **1 hop** (directly linked).
- `/path?from=Ada%20Lovelace&to=Photosynthesis` → **2 hops** via *Computer*.
- `/related_fused/195` → fused ranking above.
- No regression: `/`, `/search`, `/article/195`, `/related/195`, `/ask` all 200;
  `/related` still returns `{semantic, hyperlinks}` unchanged.

## Deferred / corners cut

- **Path search is bounded**, not provably global-shortest for pathological
  hub-saturated pairs: if `max_expand` (400k nodes) is hit before the frontiers
  meet, `/path` reports "no path found within N hops" rather than searching on.
  Fine for a personal tool on a densely-linked graph (most real pairs meet in
  1–3 hops).
- **Graph proximity in the fused ranking is 1-hop + bounded 2-hop co-citation**,
  not the full Adamic-Adar / leakage-corrected scorer of
  `wiki_linkpredict_fused.py` — that offline analysis materialises a bounded
  adjacency over all 224M edges per run, too heavy for a per-request serve path.
  The RRF *method* is the same; the graph feature is the cheap serve-time variant.
- **Undirected CSR keeps duplicate edges** (when both `u→v` and `v→u` existed);
  harmless for BFS, not de-duplicated.
- **Narration** is single-shot and un-cached; it re-runs the `/path` resolve +
  BFS when `narrate=1` (cheap) before calling the LLM.

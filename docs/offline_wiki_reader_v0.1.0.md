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

---

## Addendum v4 — inline article auto-linking + hover-preview cards (DEV-1354)

Article bodies are now rendered with inline links and Wikipedia-style **page-preview
hovercards**, instead of flat text. The stored body is **plain text** (the extractor
stripped all `[[wikilink]]` markup — `articles-*.jsonl` is `{id,title,text,ts}` with
no link residue), so the links are **reconstructed**, not parsed out.

### Layer A — real links (default ON, precise)
For the viewed article we take its **directed out-edge target ids** (the CSR
`edges_csr_dst`, i.e. the articles this page actually links to) and match each
target's **canonical title** — plus any **prose redirect alias** pointing at it — in
the body, wrapping the phrase as `<a class="wl" href="/article/{id}">`. This
reproduces Wikipedia's own curated linking: dense where the author linked, zero
overlinking elsewhere. Matching is **longest-match, case-insensitive, word-boundary**,
**first occurrence per target** (cleaner than linking every mention). The body is
**HTML-escaped first**; link markup is only ever inserted around already-escaped text
spans, so the output is injection-safe (verified: `&`, `<`, `>`, `'` in a body all
escape correctly and never break a tag). Ada Lovelace (id 195): **~179 real links
restored**, e.g. *Charles Babbage*, *the analytical engine*, *Lord Byron*, *general-
purpose computer*.

Redirect aliases come from a new `redir(target_id, alias)` table in `reader.db`, built
by `python tools/wiki_reader.py build-redirects` (23 s; also folded into full `build`).
Of 11.9M redirects we keep the **10.0M prose-like** aliases (contain a space, no
subpage `/`, ≤60 chars); camelCase link tokens and slashed subpages are dropped as
they never occur in running prose. At serve time only the aliases **whose target is an
out-edge of the current page** are pulled, so aliases add surface forms without
loosening precision.

### Layer B — denser "notable terms" (toggle, default OFF)
The **"denser links"** checkbox re-renders with `?dense=1`, additionally linking
**notable multi-word titles** found in the body that Layer A didn't already link.
Notability filter (the WP:OVERLINK guard): a title must be **2–4 words**, have
**inbound degree ≥ 40**, and **not begin or end with a function word** (kills common-
phrase false positives like *"up to"*, *"for good"*, *"the junction"* while keeping
*United States*, *nervous system*, *Harvard University*, *think tank*). That yields a
**~600k-title** matcher (from the 6.9M corpus), built **lazily on the first denser
request** (~5 s, one bincount over the CSR + one title scan) and cached — normal
startup stays ~49 s. Matching is a token n-gram lookup against the set (no external
Aho-Corasick dependency). On Ada Lovelace, Layer B adds ~47 links (*Fourth Estate*,
*women in science*, *operating system*, *popular culture*, …).

### Hovercards (both layers)
New endpoint **`GET /summary/{id}`** returns the article's **lead** (title + first
real paragraph, ~2 sentences, 320-char cap) via the same O(1) offset fetch. On the
frontend, hovering a `.wl` link (debounced 250 ms) fetches the summary and shows a
floating card near the cursor (title + lead + **read →**); summaries are cached
client-side per session. Inline links are click-intercepted to open in-app rather than
navigate. All JS/CSS is inline — offline, no CDNs.

### The maintainer's question, answered honestly
Yes — hovercards and matching against the **whole 6.9M-title DB** are both technically
possible. We deliberately **do not link every word**: that is textbook overlinking
(WP:OVERLINK) and makes the body unreadable, and a whole-corpus automaton is memory-
heavy for marginal value. The chosen design is **real links (precise, from the page's
own out-edges) + optional notable-term density + hovercards on all links** — more
links than the original where useful, without the noise.

### Perf / limits of Layer B (corners cut)
- The notable set is **restricted** to 2–4-word titles with **inbound degree ≥ 40**
  and non-stopword edges (~600k of 6.9M). Single words and long/low-degree titles are
  intentionally excluded — raising density there is the overlink trap.
- n-gram matching joins word tokens with single spaces, so titles with **internal
  punctuation** (e.g. *Spider-Man*, *Rock 'n' Roll*) can be missed by Layer B. Layer A
  still links them when they're the page's own out-edge (matched on the exact title).
- Layer A links the **first occurrence per target** only; later mentions are plain.
- Redirect aliases are filtered to prose forms; some legitimate one-word alternate
  names (no space) are not indexed.

### New / changed endpoints

| Endpoint | Returns |
|---|---|
| `GET /article/{id}?dense=0\|1` | now also `body_html` — body with inline `.wl` links (Layer A always; Layer B when `dense=1`) |
| `GET /summary/{id}` | `{id, title, lead}` — lead paragraph for hovercards |
| `python tools/wiki_reader.py build-redirects` | build ONLY the `redir` alias table (fast, no corpus re-scan) |

### Verified live (Spark, 2026-07-07)
- `/article/195` → 179 inline `.wl` links restored (Layer A), injection-safe escaping.
- `/article/195?dense=1` → +47 notable-term links (Layer B), noise-phrases filtered.
- `/summary/195` → Ada Lovelace lead paragraph.
- Served page carries the hovercard handler (`showHover`, `#hovercard`, `/summary/`).
- No regression: `/`, `/search`, `/related/{id}` (unchanged `{semantic,hyperlinks}`),
  `/related_fused/{id}`, `/path`, `/ask` all 200; cuVS CAGRA still loads (~48 s).

---

## Addendum v5 — invisible links, comprehensive linking, plain-text formatting, Back nav (DEV-1354)

Maintainer revision of the v4 inline-linking feature. **This addendum supersedes v4's
link styling, the density model, and the `?dense` toggle** (the two-layer "precise vs
notable" split and the "denser links" checkbox are gone). Layer A's precise out-edge
matching survives as the *precedence* rule below.

### 1. Invisible link styling
Inline links now render **identical to body prose** — `#article a.wl { color:inherit;
text-decoration:none; }` — no blue, no underline at rest. Reading looks like plain
prose. The only affordance is **hover-only**: `a.wl:hover` shows a faint tint + faint
underline. This is what makes comprehensive linking acceptable (density is invisible).

### 2. Comprehensive linking is the DEFAULT (link every word/phrase that resolves)
Every word/phrase that resolves to an article is linked, from a **full-corpus matcher**
`{lowercased title surface → best article id}`:
- **Precedence:** a phrase that is one of the page's **real out-edge targets** (matched
  by the Layer-A regex over canonical titles + prose redirect aliases — handles
  punctuation and disambiguates) is linked to that **precise** target and wins. Every
  other matchable phrase links to the **best global title match** (highest inbound-degree
  article for that surface form, a notability tie-break).
- **Longest-match wins** (multi-word entities beat their component words).
- **Skip list is tiny** — only pure function words (`the of a an and to is in on for
  as`) plus 1-char tokens (the stray "s" in "Babbage's"). Everything else links; maximal
  coverage is the goal, and invisible styling keeps it readable.

**Matcher footprint (measured, Spark / 128 GB):** the full set is **~6.89M surface
forms**, built **lazily on the first article view in ~8 s** (one bincount over the CSR +
one title scan), then cached. It is genuinely feasible on this box — no cutoff needed,
the FULL 6.9M title set is used. The matcher dict adds ~2 GB; total serve RSS ~12.6 GB
(dominated by the CAGRA/vector working set, not the matcher). Ada Lovelace (id 195):
**~179 links (v4 Layer A) → ~3,175 links** now.

### 3. Wikipedia formatting from plain text — what is and isn't recoverable
The extractor stored **plain `{text}` with all markup stripped**, so the following is
recovered heuristically from the plain text:
- **Section headings** — short, standalone, title-cased lines with no terminal
  punctuation/commas and ≤6 words become `<h3 class="wsec">` (e.g. *Biography*,
  *Childhood*, *Work*, *First published computer program*; 36 on Ada Lovelace).
  Conservative on purpose — a comma or trailing `.`/`:` marks prose/caption, not a
  heading. **Heading levels (h2 vs h3) are not recoverable** — all render as one level.
- **Paragraph spacing** — blank-line-separated blocks are preserved as `<p>`.
- **Bullet lists** — only a run of ≥2 consecutive `*`/`•` lines becomes `<ul>`. Lists
  almost never survived extraction (~0.1% of lines); numbered lists are NOT detected
  (ambiguous with years/citations).

**Hard limit (honest):** full Wikipedia formatting — **infoboxes, tables, bold/italic,
images, reference/citation lists** — **cannot be recovered from the current data** and
is deliberately **not faked**. The current extractor discarded structure at ingest.
Restoring it requires **RE-EXTRACTING from Wikipedia's HTML (or wikitext) dump
preserving structure** — a separate pipeline job, related to the deferred clean
re-extract (see the wiki-scale memory / DEV-1354 re-extract track). Until then the
reader shows structured plain text + comprehensive links + hovercards.

### 4. Back / history navigation
The reader now uses the **History API**: each in-app navigation (`open_`, connect, ask,
search) does `history.pushState`, and a `popstate` handler re-renders without pushing —
so the **browser's native Back/Forward** buttons and a **visible header `← Back`
control** (`goBack()` → `history.back()`) both walk the article/search sequence back to
the initial search results. Base state is seeded with `history.replaceState`.

### Verified live (Spark, 2026-07-07)
- Served `/` CSS: `#article a.wl { color:inherit; text-decoration:none; cursor:pointer; }`
  (invisible at rest) + hover-only underline/tint; `#back` button + `history.pushState`
  / `popstate` / `goBack()` all present.
- `/article/195`: **3,175** inline `.wl` links (single content words included), **36**
  `<h3 class="wsec">` headings; injection-safe escaping (`&#x27;`, `&lt;`).
- No regression: `/`, `/search`, `/related/{id}` (unchanged `{semantic,hyperlinks}`),
  `/related_fused/{id}`, `/summary/{id}`, `/path`, `/ask` (8 sources) all 200; cuVS
  CAGRA still loads (~48 s).

### Corners cut / tradeoffs
- Comprehensive linking has a noise tail: common one-word titles (*was*, *had*, *also*)
  link because they are real article titles and the skip list is intentionally minimal.
  Invisible styling absorbs this; tightening the skip list is a one-line change if noise
  is unwanted.
- Global matches are single-sense (highest-indegree article for the surface form); only
  the page's own out-edges are disambiguated to the author's intended target.
- First article view after a restart stalls ~8 s while the matcher builds (then cached).
- Section-heading detection is heuristic; a rare short prose line with no terminal
  punctuation could be mis-styled as a heading.

---

## Addendum v6 — filtered semantic search + graph-aware RAG (DEV-1354)

The two closing reader features. Both are **TriDB's tri-modal thesis as a personal
tool**: v6.1 is *vector similarity + relational filter*; v6.2 is *vector similarity +
graph traversal*. No engine/docker changes — pure host-side reader python + one new
host index.

### Feature 1 — Filtered semantic search (`GET /search_semantic`)
Retrieve the `pool` (default 150) nearest articles by **meaning** (cuVS CAGRA cosine
over the query embedded with the same BGE model as `/ask`), **then** apply RELATIONAL
filters and return the surviving ranked list with **pre/post-filter counts**. Semantic
first, filter second — never the reverse.

- **Endpoint** `GET /search_semantic?q=<text>&min_indeg=&min_len=&max_len=&cat=&pool=`
  → `{query, pool, pre_count, post_count, cats_available, filters, results:[{id, title,
  score, indeg, length, cats}]}`.
- **Filters:**
  - **`min_indeg`** — minimum inbound-degree (importance). From a cached `bincount`
    over the CSR dst values (`_ensure_indeg`, shared with the link matcher — built once).
  - **`min_len` / `max_len`** — article body length in chars. Read lazily: bodies are
    seeked only for candidates that survive the cheap filters, so `length` is `null` in
    results unless a length filter is active.
  - **`cat`** — category contains `<text>` (case-insensitive). Backed by a new `cats`
    table (see below); a bounded index join `article_id IN (candidates) AND
    lower(category) LIKE '%text%'`. Degrades to a no-op (with `cats_available:false`) if
    the index is absent.
- **UI:** a purple "Semantic search" bar under the connect bar — meaning query + `min
  links` / `min chars` / `max chars` / `category contains` inputs. Results render in the
  left panel with the existing cosine legibility bars + invisible links, a `countbar`
  showing `pool → pre → post`, per-result meta (`N links · M chars`) and category pills.

### Category index (`build-categories` subcommand — the new host artifact)
`cats(article_id, category)` with an index on `article_id`, built from the already-
extracted `categories-*.tsv` sidecars (`article_id \t category`) — **no corpus
re-scan**. Only `article_id` is indexed: the category filter always constrains by
candidate id first, so the `LIKE` scans only the handful of category rows for the
top-N. **Measured on Spark:** 72 shards → **40,178,200 rows in 40.1 s**; reader.db grew
**910 MB → 3.09 GB**. Run once: `python3 tools/wiki_reader.py build-categories` (also
folded into the full `build`).

### Feature 2 — Graph-aware RAG (`GET /ask?...&expand=1`, default ON)
After the semantic top-k seed retrieval, **expand along hyperlinks**: pull 1-hop
(optionally `hops=2`) out-neighbours of the seeds from the directed CSR, rank those
neighbours by **cosine to the question**, and fold the most relevant into the LLM
context (total capped at **12 passages**: 6 semantic seeds + up to 6 graph) **before**
answering. This grounds multi-hop questions in the link **chain**, not just articles
near the question wording.

- Citations always point at the **real source article id**; each source is tagged
  `origin: "semantic" | "graph"`, and graph sources carry `via` = the seed they were
  hyperlinked from (the *original* seed even for 2-hop, so a citation always traces to a
  real seed).
- `expand=0` restores the exact prior `/ask` (8 semantic seeds, no graph). Response adds
  `expanded`, `n_semantic`, `n_graph`.
- **UI:** a `graph` checkbox next to the Ask box (default checked); the Sources panel
  shows a per-source SEMANTIC / GRAPH badge (graph badge reads `graph ← <seed title>`)
  and a sub-line `"N from semantic retrieval · M pulled in by graph expansion"`.

### Endpoints (summary)
| Endpoint | Returns |
|---|---|
| `GET /search_semantic?q=&min_indeg=&min_len=&max_len=&cat=&pool=` | cosine top-N + relational filter; pre/post counts + ranked results |
| `GET /ask?q=&expand=1&hops=1` | graph-aware RAG; sources tagged semantic vs graph-expanded |

### Verified live (Spark, 2026-07-07, PID rotated cleanly)
- **Category build:** 40,178,200 rows / 40.1 s; reader.db 910 MB → 3.09 GB.
- **Filtered search** `q="quantum computing"` (pool=150):
  - `min_indeg=100` → 150 → **11** (Quantum computing indeg=1069, Quantum algorithm 108,
    Post-quantum cryptography 114, David Deutsch 118, …).
  - `min_len=20000` → 150 → **15** (Quantum computing len=52,399; Superconducting quantum
    computing 42,926; …).
  - `cat~algorithm` → 150 → **16** (Quantum algorithm, Feynman's algorithm, Quantum
    Fourier transform, … all `Quantum algorithms`-tagged).
  - combined `min_indeg=50 & min_len=10000 & cat~quantum` → 150 → **13**.
- **Graph-aware RAG** `q="How is Ada Lovelace connected to the modern computer?"`,
  `expand=1` → **6 semantic + 6 graph** sources; graph picks tagged e.g. *Herman
  Goldstine ← via Adele Goldstine*, *Thelma Estrin ← via Ada Lovelace Award*; answer
  cited [1] Ada Lovelace. `expand=0` → 8 semantic-only sources (prior behaviour intact).
- **No regression:** `/`, `/search`, `/article/{id}` (invisible `.wl` links intact),
  `/summary/{id}`, `/related/{id}` (unchanged `{semantic,hyperlinks}`),
  `/related_fused/{id}`, `/path` all 200; cuVS CAGRA still loads (~48 s).

### Corners cut / tradeoffs
- **Body length is lazy** — only computed (and shown) when a length filter is active, to
  avoid seeking `pool` bodies on every search. Importance (`indeg`) is always shown
  (free).
- **Category `LIKE` is a substring match**, not a taxonomy walk — `cat=physics` matches
  any category *containing* "physics", not sub-categories of Physics.
- **Filter pool is fixed at 150** by default: a filter stricter than any of the top-150
  candidates legitimately returns `post_count=0` (correct, not a bug). Raise `pool` for
  rarer intersections.
- **Graph expansion ranks neighbours by cosine to the question**, so on a tightly-
  clustered seed set the picks can be topically adjacent (e.g. the seed's biographical
  neighbours) rather than a long bridging chain; the mechanism and provenance are honest
  regardless. 2-hop (`hops=2`) is wired but off by default (1-hop keeps context tight).

---

# Addendum v7 — random-article home page + Home nav (DEV-1354)

Small, client-plus-one-endpoint feature: a landing view that surfaces a **fresh
random article every time**, and a **Home** control distinct from the existing Back
(history) button. No retrieval, artifact, or engine change.

### Feature — `GET /random`
Returns `{id, title}` of a random existing article. Selection is **O(log n) on the PK
index — no `ORDER BY RANDOM()`** (which would full-scan 6.9M rows): pick a random id in
`[0, max_id]` (max_id cached once) and take the next existing row
(`WHERE id >= :r ORDER BY id LIMIT 1`, using the sparse PK B-tree), wrapping to the
first row if the pick lands past the last id. Every call returns a fresh pick — never
cached.

Examples (three consecutive calls, three distinct articles):
```
{"id": 5622402, "title": "Chessa"}
{"id": 3520820, "title": "Milko Gaydarski"}
{"id": 2237904, "title": "Rafael Carbonell"}
```

### Home view
On load — and on every Home navigation — the client calls `/random` and **fully
renders** the picked article via the existing `loadArticle` path, so all inline
invisible `.wl` links, hovercards, headings/lists, and the Related panel apply exactly
as on any article. A `.homebar` above the title carries a **"🎲 Another random
article"** button that pulls a fresh `/random` **in place** (no full page reload). The
prominent header search box sits above it as always. Refreshing the page or going Home
= a new random article each time (the pick is never cached).

### Home navigation control
The header gains a **`🏠 Home`** button, and the **"Offline Wikipedia" title is now
clickable** — both call `goHome()`. This is **distinct from `← Back`**: Home jumps to
the random-article landing; Back walks history.

**History integration.** `goHome()` renders a fresh random article then
`pushState({view:'home'})` (unless already on a home state, in which case it refreshes
in place — no stacked home entries). `🎲 Another random` refreshes in place without a
push, keeping the Back stack clean. The `popstate` handler's `home` branch now calls
`loadHome()`, so reaching Home via the browser Back button lands on the random view too
(with a fresh pick — consistent with the "never cached" design). All prior popstate
branches (article / search / connect / ask / sem) are unchanged, so the existing Back
button is not regressed. `#back` stays hidden on home states (as before).

### Verified live (Spark, 2026-07-07, PID rotated cleanly)
- `/random` ×3 → three distinct articles (above).
- Served `/` HTML carries the wiring: `#home` button + clickable `<h1 onclick="goHome()">`,
  `loadHome()`/`anotherRandom()`/`goHome()`, the `/random` fetch, the "Another random
  article" control, `popstate`→`loadHome()`, and `loadHome()` on initial load.
- **No regression:** `/article/195` (Ada Lovelace) 200 with **3,175 invisible `.wl`
  links** intact; `/search`, `/summary/195`, `/related_fused/195`, `/related/195`,
  `/search_semantic`, `/path` all 200; cuVS CAGRA still loads.

### Corners cut
- **Home is intentionally non-deterministic:** Back-ing into a home entry re-randomises
  rather than restoring the exact article you last saw there (home state stores no id).
  This matches the explicit "Home/refresh = a new random article each time" requirement;
  the trade-off is that a specific random article is not itself a stable back target
  (open it as an `article` view — e.g. via a link — if you want to return to it).

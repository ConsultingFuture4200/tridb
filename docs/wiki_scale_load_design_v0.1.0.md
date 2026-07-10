# TriDB full-Wikipedia LOAD contracts — Phase 2 (v0.1.0)

**Scope.** Phase-2 of the full-Wikipedia benchmark (`docs/wiki_scale_benchmark_spec_v0.1.0.md`,
DEV-1354): how to get a `tools/wiki_extract` manifest — ~6.8M articles, ~200M hyperlink edges,
~6.8M article vectors — INTO the three TriDB legs at a size that stresses the Spark's 128 GB
working set. This is a **contract document**: precise enough for a GX10 implementer to drop code
in against a known surface. **Every load step here RUNS ON THE GX10/Spark, not on the x86 standin**
(native `graph_store_am` + `vectordb` HNSW + GPU are engine/hardware-gated). Nothing below has been
run; it is UNBUILT-HERE by design.

The upstream artifact is fixed and already built here: the extractor manifest (`tools/wiki_extract.py`,
its contract in `manifest.json`). Article ids are **dense, 0-based, contiguous in encounter order** —
that single property is what makes every bulk path below cheap, so the loader must preserve it.

---

## 0. Inputs (the manifest the loader consumes)

Per the manifest contract (do not re-derive from extractor code):
- `articles-NNNNN.jsonl` — `{"id","title","text","ts"}`, id = dense 0..N-1, shard `i//shard_size`.
- `edges-NNNNN.tsv` — `src_id<TAB>dst_id`, redirect-resolved, deduped, self-loops removed. Both ids
  are article ids. **Index-aligned to the article shards.**
- `categories-NNNNN.tsv` — `article_id<TAB>category`. The category text column is
  **PG COPY FORMAT-text escaped** by the extractor (backslash doubled, tab/newline as
  `\t`/`\n`), so it loads verbatim via `FORMAT text` — MediaWiki category names can
  contain backslashes (`$wgLegalTitleChars`) that a raw TSV would corrupt.
- `redirects.tsv` — `source_title<TAB>target_title`. **Plain TSV, NOT a FORMAT-text COPY
  target** (not needed for load; provenance / query-time, and read back as plain TSV by
  `tools/wiki_hotpot_link.load_title_index`). If a loader ever COPY-loads it, convert to
  escaped FORMAT-text or CSV first.
- `counts` — authoritative totals; the loader asserts its row counts against these.

Vectors are produced separately in Phase 1 (Spark GPU, `fastembed`/BGE) as an id-aligned
`corpus_emb.npy` (`float32[N,dim]`), row `i` = article `i`. The loader normalizes at write (cosine on
the L2 `<->` path) exactly as `tools/hotpot_corpus.embed_corpus` does.

---

## 1. Relational + article payload — COPY bulk load (PERF-11 / DEV-1346)

**Why not INSERT.** Per-row `INSERT` of 6.8M articles is INSERT-bound and a non-starter (PERF-11
context, `docs/perf_research_v0.1.0.md`). Load is COPY-staged; the fork's COPY rework (DEV-1346, plan
035) is the enabling work, and the baseline PG must be COPY-capable for a fair at-scale SM-2.

**Contract.**
```sql
CREATE TABLE articles (
    id     bigint PRIMARY KEY,        -- = the manifest article id (dense 0..N-1)
    title  text   NOT NULL,
    ts     text,                       -- ISO-8601 revision timestamp ("" -> NULL at load)
    embedding float8[]                 -- dim-D vector, normalized at write; NULL until §3
);
CREATE TABLE article_categories (article_id bigint, category text);  -- from categories-*.tsv
```
- Stream each `articles-NNNNN.jsonl` shard → `COPY articles (id,title,ts) FROM STDIN` (text/body kept
  in a side table or column as the personal-wiki surface needs; the benchmark needs only id+title+ts+vector).
  **Title is FROM the JSON (raw MediaWiki title) — `tools/wiki_load.py` MUST FORMAT-text-escape it
  (backslash/tab/newline) when building COPY rows, or use `FORMAT csv` / a parameterized path; titles
  can contain backslashes (`$wgLegalTitleChars`) just like categories.**
- Stream `categories-NNNNN.tsv` → `COPY article_categories FROM STDIN (FORMAT text)` — the extractor
  already FORMAT-text-escapes the category column (see §0), so the shard loads verbatim and byte-safe.
- **Order-preserving:** feed shards in lexical order so `articles` lands in id order (helps the §3
  vector build and keeps id == physical order for locality).
- **Assert:** `SELECT count(*) FROM articles` == `manifest.counts.articles`; likewise categories.
- `embedding` is loaded in §3 (a second COPY/UPDATE pass) so the text and vector legs decouple.

Deliverable interface: a `tools/wiki_load.py --manifest DIR --pg PORT` staged COPY driver (GX10), the
scale twin of `tools/bench_sm2_corpus.py`'s loader. Host-independent to WRITE; GX10 to RUN.

---

## 2. Native graph — BULK edge loader (the new work the spec calls out)

**Why not per-edge.** `add_edge(src,dst)` (the v0-compat front door,
`src/graph_store/graph_store_am--0.1.0.sql`) does, per call,
`gph_insert_edge(gph_upsert_vertex(src), gph_upsert_vertex(dst))` — two id-map descents + an insert.
At 200M edges that per-edge map tax is **days** (spec §2). The bulk loader must bypass the per-edge map.

**The lever: dense ids + identity mode.** The map layer (`gph_vid_map`, ADR-0013) exists because
external ids are arbitrary. Here they are NOT — article ids are exactly `0..N-1`. So:

1. **Materialize vertices in id order.** Call `gph_insert_vertex()` N times (or a bulk vertex-extend
   variant) so vid == ext_id == article id for every article. This is the "VERIFIED dense-in-order
   load" precondition the SQL comments require.
2. **Turn on identity mode.** `SELECT graph_store.gph_set_identity_mode(true);` — now `ext_id == vid`,
   so edge endpoints need NO map lookup (the plan-033 fast-path the read side already exploits).
3. **Bulk-insert edges by vid directly.** Feed `edges-NNNNN.tsv` straight into
   `gph_insert_edge(src_id, dst_id)` — src_id/dst_id ARE the vids. No `gph_upsert_vertex`, no
   `gph_vid_map` write per edge. This is the staged path: a COPY-into-a-staging-table
   (`edge_stage(src bigint, dst bigint)`) then a single set-oriented
   `SELECT gph_insert_edge(src,dst) FROM edge_stage ORDER BY src` — grouping by `src` so each source's
   adjacency chain is written contiguously (page-locality: the native-store win the benchmark measures).

**New engine work to specify (GX10):** a true batched edge-append entry point
`gph_insert_edges(src bigint, dst bigint[])` (or a COPY-target AM path) that appends a whole adjacency
run under one page-extend + one GenericXLog record instead of one WAL record per edge. Contract:
- Input: edges grouped by `src` (the staging `ORDER BY src` guarantees this).
- Effect: identical on-disk adjacency chains to N× `gph_insert_edge`, byte-for-byte (parity oracle:
  `scripts/graph_am_test.sh` style — the bulk path and the per-edge path must produce identical
  `gph_neighbors` output for every vertex).
- Txn: one host txn / one WAL (golden rule 2); FR-7 abort-atomic (a rolled-back bulk load leaves ZERO
  edges, like `scripts/txn_atomicity_test.sh`).
- **Assert:** `gph_edge_count()` == `manifest.counts.edges`.

**Identity-mode hazard (must gate).** `gph_set_identity_mode(true)` with `ext_id != vid` corrupts
reads (`gph_neighbors_ext` returns wrong ids). The loader turns it on ONLY after asserting the
vertices are dense-in-order (step 1). Relatedly, DEV-1352 (identity-mode bug, see
`[[tridb-gbrain-backend]]`) must be closed before this path is trusted at scale.

---

## 3. Vector leg — HNSW build on 6.8M vectors (PERF-04 or PERF-08)

**Why it is the gating cost.** Single-threaded `addPoint` on 6.8M×dim-768 is tens of hours (the
recall-decay bench stalled on exactly this, STATUS.md). Two realistic tools:

- **PERF-04 — parallel `addPoint`.** Multi-threaded insert into hnswlib within the fork. Cheaper to
  build, CPU-bound, hours→tens-of-minutes. Contract: identical graph quality (recall@k vs an exact
  numpy oracle within noise) to the single-threaded build; the existing CPU iterator loads it unchanged.
- **PERF-08 — GPU CAGRA build (plan 008).** cuVS builds a CAGRA graph on the GPU and exports to the
  hnswlib on-disk format the EXISTING CPU iterator reads unchanged (the seam is already specified —
  `scripts/gpu_build_index.sh`, `docs/gpu_index_build_v0.1.0.md`, `make gpu-build-index`). **The right
  tool at 6.8M: minutes vs hours.** GX10-only (cuVS for ARM64 + sm_121).

**Contract (either path).**
```sql
-- embeddings loaded id-aligned (§1), normalized at write
CREATE INDEX articles_hnsw ON articles USING hnsw (embedding)
    WITH (dimension = D, distmethod = l2_distance, m = 16, ef_construction = 200);
```
- Input: id-aligned normalized vectors (row i = article i), so an HNSW label == article id == graph vid.
  This three-way id identity (relational PK = graph vid = HNSW label) is what lets `tjs_open` fuse the
  legs without a translation table.
- `float8[]` doubles footprint vs pgvector float32 (spec: ~42 GB raw at dim-768); a dim-384 model
  halves it. The index leg (~60–80 GB) is the tight part on 128 GB — this is the I/O-bound point.
- **Assert:** index row count == `manifest.counts.articles`; smoke recall@10 on a held-out sample vs
  the numpy oracle ≥ the Phase-1 encoder's known ceiling.

Build driver: `make gpu-build-index DATASET=corpus_emb.npy INDEX_OUT=...` (GX10), then attach.

---

## 4. Chunk-level (Phase 6) — RaBitQ 4-bit quantization (PERF-10)

**Why.** Chunk-level embeddings (~25M chunks) exceed 128 GB decisively (spec §Phase 6), forcing
quantization. RaBitQ 4-bit takes the vector leg ~42 GB → ~6 GB.

**Contract (simulator already here; in-engine storage GX10-pending).**
- The recall/footprint tradeoff is measurable HERE without the engine: `bench/rabitq_sim.py`
  (`make rabitq-sim`) quantizes to 1/2/4-bit and reports recall@10 raw + **full-precision rerank** vs
  footprint. Use it to pick bits before the GX10 build.
- In-engine (GX10, plan 008): store 4-bit RaBitQ codes as the primary HNSW payload; keep full-precision
  vectors on the side (or recompute) for an **in-scan rerank** of the top candidates, preserving TR-1
  early termination (rerank only the small examined set, never a materialized full pass).
- Contract: quantized-index recall@k (with rerank) within a stated ε of the full-precision index at a
  fraction of the footprint; the rerank stays inside the early-terminating scan (no blocking operator —
  golden rule TR-1).

---

## 5. Load order + gates (GX10 runbook)

1. `make wiki-extract WIKI_DUMP=…/enwiki-…xml.bz2 WIKI_OUT=data/wiki/enwiki WIKI_MAX=` (Phase 0, here or Spark).
2. Phase 1 (Spark GPU): embed → `corpus_emb.npy` (id-aligned, normalized).
3. §1 COPY-load `articles` + `article_categories`; assert counts.
4. §2 vertices-in-order → `gph_set_identity_mode(true)` → staged bulk edge load; assert `gph_edge_count`.
5. §3 GPU-CAGRA (PERF-08) HNSW build on the vectors; attach; assert index count + smoke recall.
6. Phase 3: `bench/wiki_scale_report.py --emit-sql` → run the tjs_open SELECTs live; measure latency +
   SM-3 candidates-examined / pages-touched (`gph_page_reads()`), vs the multi-store baseline.

**Honesty gate (spec §"winning" / "failure mode").** Steps 1–5 are load; step 6 is the actual test.
Do NOT pre-announce a win: if fused `tjs_open` is not faster at this I/O-bound scale, TriDB's value is
one-WAL consistency, not speed — report it either way.

# ADR-0018: Wikidata-on-TriDB benchmark — dataset, slice, id-mapping, honesty framing

**Status:** Accepted (2026-07-10) — spike design for plan 060 (DEV-1354 residual). Ingest tooling +
two measurement harnesses authored on the x86 standin; the ≥1M measured runs are GX10-gated.
**Issue:** DEV-1354 (proving-ground selection after the seedless-fusion 1M leg blocked on plan 043)
**Related:** ADR-0013 (`gph_upsert_vertex` dense id-map — the Q-id→vid layer this reuses), ADR-0016
(typed + directional + source-scoped traversal, `register_edge_type` — the P-id→edge-type layer),
ADR-0017 (heterogeneous `tjs_open`; normalize-at-write B4-interim — the embedding contract),
plan 043 (blocked seedless/vector-first 1M leg), plan 060 (this spike), `tools/wiki_extract.py`
(the shard/manifest convention mirrored here), `docs/benchmark_wiki_scale_h2h_v0.1.0.md`.

## Context

TriDB's demonstrated, durable value is **one-WAL cross-modal consistency + fused early-terminating
retrieval** (ADR-0017), proven at 200k on the wiki corpus (0 vs 42 torn writes; +15.6pt multi-hop
recall@5 on HotpotQA) and at the filter-first physical operating point up to 1M (DEV-1290: recall 1.0,
4.7ms). The remaining GTM gate is **evidence at a scale a stranger can reproduce, on a dataset that is
natively tri-modal and where the multi-store stack (Milvus + Neo4j + Postgres) is what people actually
run.**

Wikipedia (the existing corpus) is append/read-mostly and its hyperlink graph is untyped. Wikidata is
the natural next proving ground:

- **~110M entities, ~1.5B typed statements** (P-property edges) — ~16× Wikipedia — and the statements
  are the exact typed/directional/source-scoped shape ADR-0016 (plan 038) was built for. No other public
  corpus exercises the typed traversal leg.
- Modalities map 1:1: label+description **embedding** (vector) / **typed statement** (graph edge) /
  structured **claims + entity metadata** (relational filter).
- It has a **public real-time edit firehose** (millions of edits/day) — a genuine *mutation* workload,
  which lets us extend the consistency demo from "42 torn writes avoided" to "torn cross-modal reads
  avoided under real concurrent edits."
- Its natural queries are **selective** (typed edge + entity-type constraint) → the **filter-first**
  physical path, already green at 1M. So the headline experiments do NOT depend on the blocked
  seedless-fusion (vector-first) leg — the reason Wikidata, not a bigger wiki slice, is the right
  proving ground while plan 043 is open.

This ADR records the load-bearing design choices so the ingest tool, the two harnesses, and the report
are reproducible and honest. It is a **spike design**, not a commitment to the full 110M load — that is
the *result* of a GO verdict, not part of the spike.

## Decision

### (a) Dump source — truthy JSON, SQL dump as fallback

Ingest the **`latest-truthy.nt`/`latest-all.json` truthy JSON dump** (gzip, line-delimited JSON, one
entity per line). Rationale: statement-level, streamable, and ~2× smaller than `latest-all` because
truthy keeps only the best-rank statement per (entity, property) — which is exactly the edge set a
benchmark wants (no deprecated/duplicate-rank noise). It streams with bounded memory the same way
`tools/wiki_extract.py` streams the MediaWiki dump — never materialize the whole dump in RAM.

**STOP → fallback:** if the truthy JSON schema has drifted or the dump is unavailable, fall back to the
`wikidatawiki` SQL dump (`wb_terms` + the `pagelinks`/`wbc_entity_usage` tables) and document the schema
in the report. Do **not** hand-fabricate entities.

**Pin for reproducibility:** the report and ingest manifest record the dump date and, for Harness A,
the edit-window bounds (a fixed recorded replay window), so a stranger re-running gets the same slice.

### (b) Slice — BFS closure from a seed entity set, not a random Q-id cut

The slice is a **breadth-first closure from a seed entity set** (a domain: e.g. chemistry `Q11173`
closure, or geography) expanded over truthy statements to a target entity count. A random Q-id range cut
produces a **disconnected** graph and understates traversal value (most edges point outside the cut).
Because the full frontier cannot be held in RAM at target scale, the closure is **two-pass streaming**
(pass 1: collect the frontier id set to the target count; pass 2: emit rows for exactly that set,
dropping edges whose dst is outside it), mirroring `wiki_extract`'s two-pass title/redirect design. The
memory ceiling is the frontier id set (a `set[int]` of Q-numbers), documented in the report.

### (c) id mapping — Q-id → dense vid, P-id → typed-edge dict id

- **Q-id → dense table id == graph vid.** A Wikidata entity id `Qn` maps by its integer `n` through the
  existing `gph_upsert_vertex(ext_id) RETURNS bigint` + `gph_vid_map` dictionary side-table (ADR-0013,
  Option A). The relational entity table's primary key is that same dense vid, so the vector row, the
  graph vertex, and the relational tuple for an entity share one id — no cross-store join key. The ingest
  tool emits ext-ids (the Q-numbers) and lets the engine assign dense vids at load, so the mapping is the
  engine's, not a parallel scheme that could drift.
- **P-id → typed-edge dictionary id.** A property `Pm` maps through `register_edge_type('Pm')` into the
  `edge_type(id, name)` dictionary (ADR-0016). A statement `(Qs, Pp, Qo)` becomes a typed edge
  `gph_insert_edge(vid(Qs), vid(Qo), type_id(Pp))`. Topology is native; the P-id↔name mapping is
  relational — golden rule 3.
- **Direction.** A statement is directional subject→object, emitted as an **out-edge** `Qs → Qo`. ADR-0016
  ships out-direction only; `direction=in`/backlinks raise `feature_not_supported` pending the deferred
  reverse index. Harness B query templates therefore traverse in the natural subject→object direction; any
  backlink-shaped query is labeled blocked-on-the-reverse-index, not silently reversed.

### (d) Embedding — label+description, normalize-at-write

Each entity's embedding source is `label + " — " + description` via the existing fastembed/BGE path
(the same embedder the wiki corpus uses). Vectors are **L2-normalized at write** (ADR-0017 B4-interim),
so `<->` / `<#>` distance is order-equivalent to cosine and the HNSW index and the oracle agree on the
metric. Entities with neither a label nor a description in the target language are dropped from the
vector leg (still valid graph vertices).

### (e) Honesty framing — what a number does and does not prove

- **Regime.** At 1M this is **compute-bound**, not I/O-bound. The I/O-locality thesis is dead
  (wiki-scale memory); the demonstrated value is (1) **fusion speed** from early-terminating tri-modal
  co-iteration and (2) **one-WAL cross-modal consistency**. Every reported number is framed as one of
  those two, never as an I/O-locality win.
- **Physical path.** The headline experiments run the **filter-first** `tjs_open` mode (selective typed
  edge + entity-type constraint first, vector ranking second) — green at 1M (DEV-1290). The
  **seedless / vector-first** mode is **blocked on plan 043** and is not quoted; the report labels it so.
- **Matched recall only.** Latency and pages-touched are reported ONLY at matched recall against the
  fused oracle, via `publication_gate()` reused verbatim from `bench/wiki_h2h.py` (graph-set parity,
  timer-boundary, HNSW build health, matched recall, examined>0). A number that fails the gate is not a
  headline.

## Consequences

- The ingest tool (`tools/wikidata_ingest.py`) and both harnesses (`bench/wikidata_consistency.py`,
  `bench/wikidata_h2h.py`) reuse the wiki shard/manifest format and the h2h honesty gate unchanged —
  the spike adds ingest + query-template + edit-replay code, not a new measurement discipline.
- Because the entity's three modal representations share one dense id, the consistency harness can assert
  a torn read structurally (the three legs disagree on the same id) — no fuzzy key matching.
- **Backlink-shaped KBQA is out of scope** until the ADR-0016 reverse index lands; the spike's query set
  is subject→object only. This is a real coverage gap, stated, not worked around.
- **Scale ceiling on the standin:** the 100k slice fits this box; 1M is the standin reach for the
  filter-first leg (DEV-1290 precedent); 10M+ is GX10-gated (128 GB). The report caps each claim at the
  slice actually loaded and marks the rest GX10-gated — no claimed scale that was not loaded.
- When plan 038's typed traversal lands built on the GX10, Harness B's graph leg switches from the v0
  shim to the native typed AM; re-run and compare (the "measured the right store" discipline, ADR-0013).

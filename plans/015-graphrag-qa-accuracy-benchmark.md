# Plan 015: Real-graph + downstream-QA-accuracy benchmark (HotpotQA fullwiki)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report instead of improvising. When done,
> update this plan's status row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 3268831..HEAD -- tools/real_corpus.py tools/bench_corpus.py tools/fetch_dataset.py bench/live_report.py baseline/sm2.py baseline/harness.py scripts/bench_public.sh`
> If any in-scope file changed since this plan was written, reconcile the
> excerpts below against the live code before proceeding. A mismatch in the
> drop-in contract (`real_corpus.py` manifest / `bench_corpus.build_sql`) is a
> STOP condition — those are the seams this plan reuses.

## Status

- **Priority**: P0 (GTM make-or-break — `docs/gtm_opensource_v0.1.0.md` names this
  the single highest-leverage artifact: the answer to "Speed, but is the answer
  right?")
- **Effort**: XL (multi-phase; spans buildable-here, GX10-gated, and a new
  generation dependency)
- **Risk**: HIGH (new dataset, new graph builder, new reader-LLM dependency, and
  the headline corpus build is off-box)
- **Depends on**: 010 (TJS critical path — `tjs()` lands the graph-constrained
  scan), 012 (`term_cond` knob exposed)
- **Category**: benchmark / thesis-validation
- **Planned at**: commit `3268831`, 2026-06-27
- **Decisions locked (operator, 2026-06-27)**: dataset = **HotpotQA fullwiki**;
  metric depth = **full downstream answer EM/F1** (not retrieval-only);
  deliverable = **plan before build**.

## Why this matters

Every accuracy number TriDB reports today is measured on a graph that is a
*function of the embeddings*. `tools/real_corpus.py` is explicit about it: it
loads real SIFT vectors but **synthesizes a topical hub/fanout graph from vector
proximity**, then grades **recall@k against a numpy ANN oracle**. Two problems
for the GraphRAG thesis:

1. **The graph carries no information the vectors don't already have.** Edges are
   drawn from vector neighborhoods, so "graph-constrained vector search" is being
   validated against topology derived from the very vectors it constrains. It
   cannot show that *real* topology improves retrieval.
2. **Recall@ANN-oracle is not answer accuracy.** It measures "did the
   early-terminating scan find the true nearest *reachable* vector," not "did we
   retrieve the facts needed to answer a multi-hop question."

This plan closes both: a recognized multi-hop QA workload (**HotpotQA fullwiki**)
whose graph is the **real Wikipedia hyperlink graph** (independent of the
embeddings), graded on **downstream answer EM/F1** (and evidence recall as the
intermediate), with TriDB's `tjs()` graph-constrained retrieval measured against
a vector-only ablation and the live multi-store baseline — at fixed latency.

## Current state (seams to reuse — do NOT reinvent)

- **Drop-in corpus contract**: `tools/real_corpus.py` already defines the exact
  manifest + `#BENCH` SQL that the live harness consumes, by reusing
  `tools/bench_corpus.py:build_sql` (single source of truth). The new HotpotQA
  loader MUST emit the same manifest shape (`entities`, `dim`, `_entities`,
  `_edges`, `oracle`, `queries`, `k`, `term_cond`) so `scripts/bench_live.sh` and
  `bench/live_report.py` consume it unchanged. The ONE difference real_corpus
  introduced — real vectors can't be RNG-replayed, so they ride in `_entities` —
  applies here too.
- **Canonical query**: `tjs('entities', k, term_cond, ...)` is a C SRF, hardwired
  vector-first with early termination (TR-1). Graph-constrained retrieval = seed
  by vector top-m on the question, expand over real edges, re-rank vector-wise,
  early-terminate. No new operator; assemble from `tjs()` + the existing SQL/PGQ
  surface (golden rule 4).
- **Pinned download pattern**: `tools/fetch_dataset.py` — `Dataset{url, sha256}`,
  `.part`→rename, `--pin`/`--allow-unpinned`, `sha256_file` verify. The HotpotQA
  fetcher MUST follow this exactly (pinned URL + SHA256, `make fetch-hotpot`).
- **Live multi-store baseline**: `baseline/sm2.py` + `baseline/harness.py` drive
  Milvus+Neo4j+Postgres over a shared corpus (stack is up:
  `make baseline-up`, Postgres on host port via `BASELINE_PG_PORT`/`PGPORT` — note
  it is **5433** on this box, not 5432). Reuse the connection + load helpers.
- **Report renderer**: `bench/live_report.py` parses `#BENCH` output + manifest
  into SM-1..SM-5. The new report extends this pattern, it does not fork it.

## Hard realities of the locked options (state these honestly in the writeup)

| Reality | Consequence |
|---|---|
| **fullwiki corpus = ~5M intro paragraphs, multi-GB** | Embedding + HNSW build of the *full* corpus is **GX10-gated**. A pinned, deterministic **dev slice** (the union of all gold + distractor articles for the dev questions, ~tens of thousands of paras) is buildable here and is enough to prove the pipeline + produce a first honest number. |
| **Live `tjs()` latency is engine-gated** | The latency-at-fixed-accuracy headline runs on the engine image. Accuracy (evidence recall + EM/F1) is computable host-side TODAY via a numpy retrieval oracle, mirroring how `real_corpus.py` grades recall without the engine. Never claim the live latency from the host path. |
| **EM/F1 needs a reader LLM** | New generation dependency. Default to the Anthropic API (latest Claude per house rules) behind a thin `reader` interface so a local model can be swapped in. Pin the reader model id + prompt + decoding params in the manifest so the number is reproducible. A faster wrong answer is worth nothing — EM/F1 is the point. |

## Build phases

Each phase is independently committable (`type(scope): summary`,
`dustin/dev-NNNN` branch). Phases 1–4 + 6 are buildable on this x86 standin;
phase 5 (full-corpus + live latency) is GX10/engine-gated and must be marked
UNBUILT-HERE.

### Phase 1 — Dataset fetch + pin (`make fetch-hotpot`)  [buildable here]
- `tools/fetch_hotpot.py` mirroring `fetch_dataset.py`: pin the HotpotQA dev
  fullwiki questions (`hotpot_dev_fullwiki_v1.json`) **and** the processed
  Wikipedia intro corpus (the hyperlinked enwiki abstracts the fullwiki setting
  uses) by URL + SHA256. `--pin` first-fetch flow; `.part`→rename; verify.
- `make fetch-hotpot` target; network-gated, never run by tests/CI (match the
  `fetch-dataset` precedent).
- **Verify**: `python -m tools.fetch_hotpot --pin` prints digests; a second run
  verifies clean. STOP if the upstream URL is unpinnable — record the chosen
  mirror in the module docstring as `fetch_dataset.py` does.

### Phase 2 — Real graph builder  [buildable here]
- `tools/build_wiki_graph.py`: node = wiki article (its intro paragraph),
  edge = intro-text hyperlink article→article. This is the **embedding-independent
  topology** that is the whole point. Output: title→entity-id map + edge list in
  the manifest `_edges` field.
- Restrict to the **dev slice**: all articles referenced (gold + distractor +
  their one-hop hyperlink neighbors) by the dev questions, so the graph is real
  but the corpus is tractable here. Record the slice definition in the manifest.
- **Verify**: assert every gold supporting title resolves to a node; assert edges
  are hyperlink-derived (a unit test feeds a 3-article fixture with known links
  and checks the adjacency). STOP if >X% of gold titles are unresolvable (corpus
  ↔ question title-normalization mismatch — a real, known HotpotQA footgun).

### Phase 3 — Embed + manifest emitter (drop-in)  [dev slice here / full GX10]
- `tools/hotpot_corpus.py`: embed each paragraph with a pinned real encoder
  (dim ≥768; e.g. `bge-base`/`e5-base` class), build the `#BENCH` SQL + manifest
  by calling `tools.bench_corpus.build_sql` (do NOT hand-roll SQL — that is the
  drift the shared emitter exists to prevent). Question embedding = same encoder.
- Manifest carries `_entities` (real vectors), `_edges` (real hyperlinks),
  per-question `gold_titles`/`gold_answer`/`supporting_facts`, and the pinned
  encoder id.
- **Verify**: round-trip the manifest through `bench/live_report.py`'s loader
  unchanged (proves the drop-in contract holds). Dev-slice embed runs here;
  full-corpus embed is marked GX10-gated and NOT claimed.

### Phase 4 — Host-side accuracy harness  [buildable here]
- `bench/graphrag_report.py`: for each question, three retrievers over the dev
  slice — (a) **TriDB graph-constrained** (numpy oracle of the `tjs()` semantics:
  vector-seed → real-edge reachable set → vector re-rank → top-k), (b)
  **vector-only** ablation (no graph), (c) **multi-store baseline** via
  `baseline/graphrag.py` (Milvus ANN + Neo4j hyperlink hop + pg filter, merged
  app-side — extends `baseline/sm2.py`).
- Metrics per retriever: **supporting-fact / gold-paragraph recall@k + F1**
  (intermediate) and **downstream answer EM/F1** (headline) via a pinned reader
  LLM over the retrieved context, all **at a fixed latency / fixed context
  budget**. The ablation (a vs b) is the thesis test: does *real* topology lift
  EM/F1 the synthesized graph never could?
- **Verify**: deterministic on a fixed seed + pinned reader; a small fixture
  question with a hand-checked gold chain produces the expected recall. STOP and
  report honestly if graph-constrained does NOT beat vector-only — that is a
  finding, not a failure to paper over (house rule 3 + 7).

### Phase 5 — Live engine + full-corpus headline  [GX10/engine-gated — UNBUILT-HERE]
- `scripts/bench_graphrag.sh` (sibling of `bench_public.sh`): run the canonical
  `tjs()` graph-constrained retrieval on the live `tridb/msvbase:dev` engine over
  the corpus, capture live latency + `tjs_candidates_examined` (SM-3) at the
  operating `term_cond`, and report **EM/F1 at fixed live latency** vs the live
  multi-store baseline. Full fullwiki corpus embed + HNSW build happens on the
  GX10 (128 GB). Mark every off-box artifact UNBUILT-HERE; do not write "passes".
- **Verify (on GX10 only)**: `make graphrag` end-to-end; the live EM/F1 and
  latency match the host-side accuracy within tolerance on the shared dev slice.

### Phase 6 — Report + Make wiring + docs  [buildable here]
- `make graphrag` (host-side accuracy; guards on dataset present + reader creds)
  and `make graphrag-live` (engine-gated, guards on the image like `bench-public`).
- `docs/benchmark_graphrag_v0.1.0.md`: methodology, the graph-is-real argument,
  the vector-only ablation, EM/F1 + evidence recall at fixed latency, scaling
  note (dev slice here / fullwiki GX10), one-command repro, and an Honesty Notes
  section (what is host-side vs live, reader-LLM variance, slice vs full corpus).
- Update `docs/STATUS.md` and `docs/gtm_opensource_v0.1.0.md` (the
  "Speed, but is the answer right?" row) to point at the artifact.

## STOP conditions (summary)

- Drop-in manifest / `build_sql` contract drifted → reconcile before building.
- Gold supporting titles unresolvable against the corpus above threshold.
- Graph-constrained retrieval does not beat the vector-only ablation → STOP,
  report the number as-is, do not tune to a foregone conclusion.
- Any claim of a full-corpus or live-latency result produced off the GX10.

## Out of scope (v1)

- BM25 seam (closed for v1, golden rule 5).
- Generalizing beyond the one canonical query (golden rule 4).
- 2WikiMultiHop / MuSiQue (could reuse this harness later; not v1).

## Status row to add to `plans/README.md`

`| 015 | Real-graph + QA-accuracy benchmark (HotpotQA fullwiki) | P0 | XL | 010, 012 | TODO |`

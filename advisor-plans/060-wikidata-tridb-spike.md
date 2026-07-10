# Plan 060: Wikidata-on-TriDB spike — prove the differentiator at 10× wiki scale

> **Executor instructions**: This is a DESIGN + measured-SPIKE plan (like 016/023/043), not a
> production build. Deliverables are ingest tooling, a tri-modal load path, two measurement
> harnesses, and a report with real numbers. Honor STOP conditions. Update `advisor-plans/README.md`
> when done.
>
> **Drift check**: `git diff --stat 1f0628f..HEAD -- bench/wiki_consistency.py bench/wiki_h2h.py tools/ docs/`

## Status
- **Priority**: P1 (direction / GTM-scale evidence)
- **Effort**: L
- **Risk**: MED (dataset scale; seedless-fusion leg gated on 043)
- **Depends on**: 038 (typed traversal, IMPLEMENTED-pending-GX10) hard; 043 (seedless fusion) ONLY
  for the vector-first query mode — the primary experiments use the filter-first path, already green at 1M (DEV-1290).
- **Category**: direction / benchmark
- **Planned at**: commit `1f0628f`, 2026-07-09

## Why this matters

TriDB's demonstrated, durable value is **one-WAL cross-modal consistency + fused early-terminating
retrieval** (ADR-0017), proven at 200k on the wiki corpus (0 vs 42 torn writes; +15.6pt multi-hop
recall@5 on HotpotQA). The remaining GTM gate is **evidence at a scale a stranger can reproduce**,
on a dataset that is *natively* tri-modal and where the multi-store stack (Milvus + Neo4j + Postgres)
is what people actually run.

**Wikidata is that dataset** and it is ~16× Wikipedia:
- ~110M entities, ~1.5B **typed** statements (P-property edges) — the exact shape plan 038's
  typed/directional/source-scoped native traversal was built for; no other public corpus exercises it.
- Modalities map 1:1: label+description **embedding** (vector) / **typed statement** (graph edge) /
  structured **claims + entity metadata** (relational filter).
- It has a **public real-time edit firehose** (millions of edits/day) — a genuine *mutation* workload,
  which Wikipedia (append/read-mostly) does not have. This is what lets us extend the consistency demo
  from "42 torn writes avoided" to "torn cross-modal reads avoided at 110M under real concurrent edits."
- Its natural queries are **selective** (typed edge + entity-type constraint) → the **filter-first**
  physical path, which already measured recall 1.0 / 4.7ms at 1M (DEV-1290). So the headline
  experiments do NOT depend on the blocked seedless-fusion leg (043).

The goal of this spike is a go/no-go on a Wikidata-scale public benchmark, with two real measured
harnesses and a sizing curve, reusing the consistency + h2h machinery already in `bench/`.

## Current state (what we reuse, do not rebuild)

- `bench/wiki_consistency.py` — the one-WAL cross-modal consistency demonstrator (`torn()`, scenario
  runners, engine-vs-multistore). Adapt its scenarios to a Wikidata edit-replay driver.
- `bench/wiki_h2h.py` — the MATCHED fused-vs-multistore harness with the `publication_gate()` honesty
  gates (graph-set parity, timer-boundary, HNSW build health, matched recall). Reuse the gate verbatim.
- `bench/tjs_open_ref.py` — the blocking fused oracle (ground truth for recall@k). Reuse the pattern.
- `tools/wiki_extract*.py` / the shard/manifest convention — mirror for a Wikidata ingest.
- Typed traversal C from plan 038 (`gph_traverse_typed`, typed insert + dict) — the graph leg.
- `docs/benchmark_wiki_scale_h2h_v0.1.0.md` — the feasibility/blocker framing; write the Wikidata
  analogue as an addendum, not a rewrite.

## Deliverables (spike scope)

1. `tools/wikidata_ingest.py` — stream a Wikidata dump slice → sharded manifest (entities, typed edges,
   claims) in the existing shard/manifest format. Deterministic slice by entity-id range or by a
   seed-set BFS closure (so the graph is connected, not a random cut).
2. `docs/decisions/0018-wikidata-benchmark.md` — ADR: dataset choice, slice strategy, id mapping
   (Q-id → dense table id == graph vid; P-id → typed-edge dict), embedding model, honesty framing
   (which regime; what a number does and does not prove).
3. `bench/wikidata_consistency.py` — the edit-firehose consistency harness (Harness A).
4. `bench/wikidata_h2h.py` — the fused KBQA head-to-head (Harness B), reusing `publication_gate()`.
5. `docs/wikidata_spike_v0.1.0.md` — the report: sizing curve + the two measured results + go/no-go.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Fast layer green | `make test && make lint` | exit 0 (new tools + tests pass) |
| Dry-run ingest | `python -m tools.wikidata_ingest --limit 100000 --out data/wikidata_slice/` | manifest + shards written |
| Harness A (host or GX10) | `python -m bench.wikidata_consistency --replay <edits> --m 100000` | torn-read tally emitted |
| Harness B (GX10) | `python -m bench.wikidata_h2h oracle|tridb-emit|grade ...` | recall curve + gated headline |

## Scope

**In scope:** new ingest tool, the two new harness modules, one ADR, one report doc, and host unit
tests for the pure helpers (mirror `tests/test_wiki_consistency.py` / `test_wiki_h2h.py`). Slice sizing
starts at 1M entities on this box's reach and scales to 10M+ on the GX10 (128 GB).

**Out of scope:** loading the full 110M/1.5B graph (footprint — that is the *result* of a GO, not part
of the spike); fixing the seedless-fusion HNSW leg (that is plan 043); a public leaderboard submission;
production ingest resumability beyond a simple checkpoint.

## Steps

### Step 1: ADR + slice strategy (design)
Write `docs/decisions/0018-wikidata-benchmark.md`. Decide and record: (a) dump source — the
`latest-truthy` / `latest-all` JSON dump vs the `wikidatawiki` SQL dump; recommend **truthy JSON**
(smaller, statement-level, streamable). (b) Slice = **BFS closure from a seed entity set** (e.g. a
domain: chemistry / geography) to a target entity count, so the induced graph is connected — a random
Q-id cut produces a disconnected graph and understates traversal value. (c) id mapping: Q-id → dense
0..N-1 table id == graph vid (reuse the `gph_upsert_vertex` mapping from ADR-0013); P-id → typed-edge
dict id (plan 038). (d) embedding: label+description via the existing fastembed/BGE path; normalize-at-
write (ADR-0017 B4-interim). (e) honesty: this is still the compute-bound regime at 1M; state it.

**Verify**: ADR committed; `make lint` green.

### Step 2: `tools/wikidata_ingest.py`
Stream the dump (gzip line-delimited JSON), for each entity emit: an embedding-source row
(id, label, description), typed edges (src Q-id, P-id, dst Q-id) for statements whose value is an
entity, and a claims row (id, selected structured properties for relational filter — e.g. P31 type,
P569 date, numeric qualifiers). Write to the existing shard/manifest format. Deterministic, resumable
by a byte-offset checkpoint. **STOP if** the dump schema requires a full in-memory graph to resolve the
BFS closure at the target size — instead do a two-pass streaming closure (pass 1: collect frontier ids;
pass 2: emit) and document the memory ceiling.

**Verify**: `python -m tools.wikidata_ingest --limit 100000 --out data/wikidata_slice/` writes a valid
manifest; a host unit test on the pure parse/slice helpers (mirror `test_wiki_extract.py`) passes.

### Step 3: Harness A — edit-firehose consistency (`bench/wikidata_consistency.py`)
Generalize `bench/wiki_consistency.py`: instead of synthetic version bumps, replay a window of the
Wikidata edit stream (or a recorded sample) as concurrent cross-modal writes (an edit updates an
entity's label→embedding, adds/removes a typed statement→edge, and changes a claim→relational row).
Run the SAME workload against (a) TriDB in one transaction and (b) the Milvus+Neo4j+Postgres stack with
independent commits. Tally torn reads (the three legs disagree) under a concurrent reader. Reuse
`torn()`. **The headline number: torn cross-modal reads, TriDB vs multi-store, at M entities.**

**Verify**: host dry-run with a tiny recorded edit sample tallies 0 torn on TriDB, >0 on the simulated
multi-store; pure helpers unit-tested.

### Step 4: Harness B — fused KBQA head-to-head (`bench/wikidata_h2h.py`)
Mirror `bench/wiki_h2h.py`: an oracle (exact fused top-k from the same embeddings + induced typed
graph), a TriDB `tjs_open` emit path (filter-first mode — selective typed-edge + entity-type
constraint), and a multi-store baseline. **Reuse `publication_gate()` unchanged** — same honesty gates
(graph-set parity, matched recall, examined>0, not-censored). Query set: entity-centric KBQA (e.g.
"entities of type T linked via P to X, ranked by description similarity to Q"). Report latency +
pages-touched ONLY at matched recall.

**Verify**: `python -m bench.wikidata_h2h oracle` on the 100k slice emits a recall curve; the gate
refuses a headline until parity holds (assert it returns blockers on a deliberately mismatched meta).

### Step 5: Sizing curve + report + go/no-go
`docs/wikidata_spike_v0.1.0.md`: slice at 100k / 1M (this box / GX10) / 10M (GX10), report load time,
footprint, Harness A torn-read delta, Harness B recall + latency at matched recall. Verdict: GO (commit
to a 110M public benchmark + GTM claim) / NO-GO (with the blocking reason) / INCONCLUSIVE (what number
is missing and where it must be measured).

**Verify**: report committed; every number traces to a harness command in the doc.

## Test plan
- Host unit tests for the pure helpers of both new harnesses and the ingest slicer (mirror the existing
  `tests/test_wiki_*.py` pattern — no network, no DB). Characterization only.
- `make test && make lint` green.

## Done criteria
- [ ] ADR-0018 + `docs/wikidata_spike_v0.1.0.md` committed with a go/no-go verdict
- [ ] `tools/wikidata_ingest.py` produces a valid sharded manifest on a ≥100k slice
- [ ] Harness A emits a real torn-read delta (TriDB 0 vs multi-store >0) on a dry-run sample
- [ ] Harness B emits a recall curve and `publication_gate()` blocks a mismatched headline
- [ ] Host unit tests for the new pure helpers; `make test` / `make lint` green
- [ ] Index row DONE

## STOP conditions
- The truthy-JSON dump is unavailable/format-drifted → fall back to the `wikidatawiki` SQL dump and
  document the schema; do NOT hand-fabricate entities.
- The 1M slice does not fit this box → cap the host run at the largest slice that fits, mark the 10M
  run GX10-gated, and do NOT claim a scale you did not load.
- The seedless (vector-first) fusion mode is needed for a query and 043 is unfixed → run the
  filter-first mode only and label the vector-first mode as blocked-on-043; do NOT quote a vector-first
  number from a lucky HNSW build (the `publication_gate()` HNSW-health blocker enforces this).

## Maintenance notes
- When 038's typed traversal lands on the GX10, Harness B's graph leg switches from the v0 shim to the
  native typed AM — re-run and compare (the "measured the right store" discipline from ADR-0013).
- The Wikidata edit stream is a live dependency; record a fixed replay window so the benchmark is
  reproducible (pin the dump date + the edit-window bounds in the ADR).
- Reviewer: ensure the report states the regime honestly (compute-bound at 1M; the I/O-locality thesis
  is dead per the wiki-scale memory) and never quotes a latency win at unmatched recall.

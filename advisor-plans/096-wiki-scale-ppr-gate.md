# Plan 096: Wiki-scale PPR re-measure — held-out link prediction at 200k, the default-adoption gate

> **Executor instructions**: This is the measurement ADR-0012's 2026-07-17 addendum named as the
> gate before any default-adoption ADR. It must (a) use a scoring-agnostic gold, (b) run at a
> scale/density where `term_cond` and the graph budget actually bite (they were inert on
> HotpotQA), and (c) end in an append-only ADR-0012 addendum with a verdict. You never flip a
> default. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat ca154d3..HEAD -- src/tjs_pg/ bench/ scripts/ test/ docs/decisions/0012-tjs-open-multiseed-retrieval.md Makefile`

## Status

- **Priority**: P1 (the named gate for the PPR default decision)
- **Effort**: M–L (mostly runtime)
- **Risk**: LOW–MED (measurement + loader only; no operator changes)
- **Depends on**: 095 (merged — `tjs.graph_scoring`), 091 (typed batch insert), 077 (budget/censoring)
- **Category**: benchmark / direction
- **Planned at**: commit `ca154d3`, 2026-07-18

## Why this matters

Plan 095's GO was measured on HotpotQA: 1490 nodes, mean degree ≈ 1, where `term_cond` was
byte-inert across {8,32,128} and no query came near the graph budget — the knobs that govern
real deployments were never exercised. The wiki hyperlink graph at 200k articles is dense enough
to exercise them, and it is the corpus TriDB's public evidence already stands on. The verdict here
is the substantive input to any "make PPR the default" ADR.

## The design trap this plan avoids (read before building)

`bench/wiki_h2h.py`'s existing oracle is **membership-shaped** (exact ANN seeds ∪ hops-reachable
set, cosine-reranked). Grading PPR against it would structurally penalize exactly the deviation
PPR exists to make — a biased NO-GO machine. The scoring-agnostic gold used here instead is
**held-out link prediction**: remove a random subset of a query article's real hyperlinks from
the loaded graph, then ask whether retrieval recovers those held-out targets. Neither mode can
reach the gold via the removed edge; both see the identical remaining graph; the gold comes from
Wikipedia's editors, not from either scoring definition. Disclose the residual tilt honestly:
link prediction inherently rewards graph-structure exploitation (that is the capability under
test), and gold targets are by construction semantically related to the query.

## Assets (all local, verified)

- `data/wiki/enwiki/emb/dense_id_aligned.npy` — shape (200000, 384) float32, row i = dense
  article id i (the wiki_h2h 200k slice embeddings; BGE-class, cosine-normalized — verify
  normalization with a quick norm check and match the HNSW opclass accordingly).
- `data/wiki/enwiki/edges-*.tsv` — `src_id\tdst_id`, redirect-resolved real hyperlink graph
  (per `tools/wiki_extract.py`). Filter to both-endpoints < 200000 for the slice.
- `data/wiki/enwiki/articles-*.jsonl` + `manifest.json` — titles/ids if needed for reporting.
- Stock image `tridb/pg17-unfork:dev`; this box: 62 GB RAM (200k × 384-dim + HNSW is small).
- Loader precedents: `bench/hotpot_stock_gate.py` (095's stock-dialect loader/sweep/grader
  pattern — mirror it), COPY staging + the typed batched `gph_insert_edges(src, dsts[], type_id)`
  (plan 091) for the edge load.

## The experiment

1. **Corpus**: articles with dense id 0..199999; `paragraphs`-style table
   (`id bigint, title text, embedding vector(384)`), HNSW index with the opclass matching the
   embeddings' metric (cosine if normalized — verify, don't assume). Graph: within-slice
   hyperlink edges, single type `related_to`, inserted in BOTH directions (the precedent 095's
   hotpot gate and the reader's link graph both use undirected adjacency — disclose).
2. **Queries + gold**: deterministic (seed 42) sample of **Q = 300** articles having ≥ 8
   within-slice DISTINCT out-links; for each, hold out exactly **5** randomly chosen (same seed)
   link targets. Held-out edges are excluded from the load in BOTH directions. Gold(q) = the 5
   held-out targets. Query vector = the article's own embedding; the query article itself is
   excluded from scoring (drop id q from results before grading).
3. **Runs**: seedless `tjs_open` (`m_seeds=8`, `hops=2`, `id` column) in both
   `tjs.graph_scoring` modes over the grid: `k ∈ {10, 20}` × `term_cond ∈ {8, 32, 128}` ×
   `tjs.graph_work_budget ∈ {2048, 8192, 65536}`. Identical inputs both modes. One persistent
   container / session for the whole sweep (do NOT reload per point); load once, sweep with
   `SET`s. If total runtime becomes prohibitive (> ~2 h measured after the first mode), trim the
   grid symmetrically (e.g. drop k=20) and say so — never trim one mode only.
4. **Metrics per point**: recall@k of gold (|retrieved ∩ gold| / 5, averaged over Q), plus
   censored fraction, mean `tjs_open_graph_examined()`, `stream_end_unknown` fraction, and mean
   per-call latency (informational). Also one diagnostic row per budget: fraction of gold ids
   reachable within `hops` of the query in the LOADED graph (same for both modes — context for
   how much headroom graph scoring even has).
5. **Knob-pressure evidence** (the thing HotpotQA couldn't give): report whether recall and
   censored fraction actually MOVE across `term_cond` and budget. If they are inert again at
   200k, that is itself a major finding — say it plainly.

## Deliverables

- `bench/wiki_ppr_gate.py` (loader-SQL generator + sweep generator + grader; mirror
  `bench/hotpot_stock_gate.py`'s structure) and `scripts/wiki_ppr_gate.sh` (docker orchestration,
  fail-loud, persistent container with cleanup trap).
- `bench/results/wiki_ppr_gate.json` + `.md` (summarized table; big scratch artifacts stay
  uncommitted).
- Append-only dated addendum to `docs/decisions/0012-tjs-open-multiseed-retrieval.md`: design
  (incl. the membership-oracle trap and the link-prediction tilt disclosure), full table,
  knob-pressure findings, and a verdict: **GO-for-default-ADR / NO-GO / INCONCLUSIVE** with what
  would change it. The default stays `membership` regardless.
- Host tests for the pure parts (query/gold sampling determinism, edge filtering, grading
  reducer) in `tests/` — the sampling and grading must be unit-tested; the docker sweep is not.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Host tests | `.venv/bin/pytest tests/ -q -k wiki_ppr` | all pass |
| The gate | `bash scripts/wiki_ppr_gate.sh` | full table printed + JSON/MD written, exit 0 |
| Suites untouched | `make test && make lint` | exit 0 |

## Scope

**In scope**: `bench/wiki_ppr_gate.py` (new), `scripts/wiki_ppr_gate.sh` (new),
`tests/test_wiki_ppr_gate.py` (new), `bench/results/wiki_ppr_gate.{json,md}` (new),
`docs/decisions/0012-tjs-open-multiseed-retrieval.md` (append-only), `Makefile` (optional
`wiki-ppr-gate` target).

**Out of scope**: ANY operator/extension C or SQL change — if the measurement seems to need one,
STOP and report; flipping any default; `bench/wiki_h2h.py` and the existing wiki benches;
committed wiki data files (read-only inputs); the 7M/enwiki_html corpus (200k is the gate scale).

## Git workflow

Branch `advisor/096-wiki-ppr-gate`. Suggested commits: `bench(ppr): wiki 200k held-out link gate`,
`docs(adr): 0012 addendum — wiki-scale verdict`.

## Steps

### Step 1: Unit-tested sampling/grading core

Pure functions: slice-edge filtering (< 200k both endpoints), deterministic query/gold sampling
(seed 42; prove same output twice; reject articles with < 8 distinct within-slice out-links),
held-out-edge exclusion (both directions), recall reducer. Tests first; include a tiny synthetic
fixture where the expected sample/gold is hand-checkable.

**Verify**: `pytest -k wiki_ppr` green; determinism test runs the sampler twice.

### Step 2: Load + smoke at small scale

Generate the load SQL (COPY-staged vectors + edges; typed batch inserts per plan 091's loader
pattern; `#WPG`-style progress markers) and smoke the WHOLE pipeline end-to-end at
`--limit 5000` articles first (load + 20 queries + grade) before committing to the 200k run.
Verify the embedding-norm check picked the right opclass and that a known held-out edge is
absent from the loaded graph (`gph_neighbors`-style probe).

**Verify**: the 5k smoke produces a complete mini-table, exit 0.

### Step 3: The 200k gate

Full load + both-mode sweep per "The experiment". Capture raw transcripts to scratch; parse
strictly (n = 300 at every point or the point is invalid — no silent drops).

**Verify**: table complete; censored fractions and graph_examined reported per point; sanity —
the membership row at the loosest point should retrieve SOME gold (link prediction is hard;
absolute recall may be low — report it straight, the comparison is relative).

### Step 4: ADR addendum + verdict

Per "Deliverables". Include the HotpotQA table's headline for continuity, clearly labeled.

**Verify**: `make test && make lint && git diff --check` green; only in-scope files changed.

## STOP conditions

- The embeddings/edges don't align with dense ids (norm/alignment checks fail) — report, don't
  guess a mapping.
- The measurement appears to require an operator change (e.g. a missing counter) — report.
- Runtime is prohibitive even after the symmetric grid trim — report partial results honestly
  as INCONCLUSIVE-partial rather than cherry-picking points.
- Any existing suite breaks (`make test` regression) — this plan adds only new files.

## Maintenance notes

If the verdict is GO-for-default-ADR, the next artifact is that ADR (supersedes ADR-0020
decision 3) — it should cite BOTH tables (HotpotQA + this one) and decide alpha/r_max exposure.
If NO-GO or INCONCLUSIVE, `tjs.graph_scoring` stays a research knob and this harness remains the
re-test vehicle.

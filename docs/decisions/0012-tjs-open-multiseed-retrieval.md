# ADR-0012: `tjs_open` — multi-seed open-domain retrieval operator (v2)

Status: Proposed (v2). Build is GX10/engine-gated (fork patch). 2026-06-28.

## Context

The v1 canonical operator `tjs(table, k, term_cond, src, attr, filter, order)` (ADR-0007)
is **single-source**: given ONE source vertex `src`, it returns the vector-nearest entities
*reachable from `src`* that pass the filter. That is the AkasicDB "given entity X, find
similar related entities" query, and it wins decisively on its home turf — SM-2: 12/12 at
~15× lower latency vs a tuned Milvus+Neo4j+Postgres stack, exact parity
([[benchmark_sm2_v0.1.0]]); plus one-WAL cross-modal atomicity proven live on the GB10.

But the real-workload head-to-head ([[benchmark_h2h_v0.1.0]]) showed `tjs()` is **not an
open-domain retriever**. On HotpotQA (seedless multi-hop QA), anchoring `src` on the top
vector hit gives recall@10 **0.223 vs a tuned multi-store's 0.953** — because a single
`src` reaches only a tiny slice of a sparse graph. Confirmed structural, not tuning:
`term_cond` 0→5000 moved recall only 0.223→0.227.

Meanwhile a HOST-side prototype — **multi-seed + bridge injection** — lifts multi-hop joint
evidence recall **+15.6 pt** over vector-only ([[benchmark_graphrag_v0.1.0]]), and a Codex
LLM reader confirms a downstream **+2.5 pt EM/F1** from that better evidence. The engine
does NOT execute this in v1. v2 closes that gap: make the engine do open retrieval, so the
"Postgres-native storage layer for GraphRAG" claim is real, not a prototype.

## Decision

Add a v2 retrieval path, **`tjs_open`**, that is seedless: it derives its own seeds from
the vector leg, expands over the graph from ALL of them, and ranks the union — the
prototype, as an engine operator.

### 1. Signature (seedless; the vector leg both seeds AND ranks)

```
tjs_open(table, k, term_cond, m_seeds, hops, attr, filter, order)
```

- `m_seeds` — number of ANN seeds drawn from the vector leg (the prototype used 2).
- `hops` — graph-expansion depth from the seeds (prototype used 2).
- No `src` argument: seeds come from the HNSW ANN top-`m_seeds` on the `order` vector,
  not a caller-supplied vertex. Everything else matches `tjs()` (filter, attrs, term_cond).

Semantics (matches the validated host `retrieve_graph_inject`): ANN top-`m_seeds` →
union of their `hops`-reachable graph neighbours (the bridges) → emit, vector-ranked, with
the injected bridges guaranteed into the budget regardless of their query similarity.

### 2. Two realizations — a reference (now) and the TR-1-pure operator (the target)

- **(A) Composition reference — buildable/runnable today, NOT shippable as an operator.**
  Express the algorithm by composing existing primitives on the live engine: HNSW ANN
  (`ORDER BY embedding <-> q LIMIT m_seeds`) for seeds + `graph_store.neighbors(seed)` per
  seed + an app/SQL rerank. This MATERIALISES the seed+reachable set, so it is **blocking
  and therefore violates TR-1** — it exists only to validate value and to be the recall
  oracle the operator must match. The host `retrieve_graph_inject` is this reference; a
  SQL version runs it on the engine for an end-to-end recall-matched number.

- **(B) Fused `tjs_open` operator — the v2 product (GX10-gated C, fork patch).**
  One pass, early-terminating, NO full materialisation: run the ANN iterator to pull the
  `m_seeds`, open a multi-source graph iterator (ADR-0005) seeded by all of them, and feed
  a single Fagin-style merge that emits top-k with the VBASE `consecutive_drops` bound
  (ADR-0006/0007) — preserving **TR-1 (golden rule 1)**. The bridge-injection guarantee is
  expressed as: a graph-reachable candidate is admitted to the heap even when its vector
  rank is past the frontier, but it does NOT reset the drop counter (so termination still
  holds). This is the only form that ships as an engine operator.

**Recommendation:** land (A) now to prove the value at the engine level (recall-match the
+15.6 pt host result on the live engine), and fund (B) as the operator. Do NOT ship (A) as
a blocking operator — TR-1 is non-negotiable.

### 3. Surface

`tjs_open` is additive — `tjs()` (single-source) is unchanged and remains the canonical
v1 query. The SQL/PGQ surface (ADR-0008) gains the seedless form; v1 callers are untouched.

## Consequences

- The GTM "open GraphRAG retriever" claim becomes real once (B) ships; until then it is a
  validated prototype, and the launch leads with the source-anchored win + one-WAL
  consistency (see GTM addendum 2026-06-28).
- (B) is the harder early-termination problem: multi-source frontier + injected
  past-frontier candidates must not break the `consecutive_drops` bound. The DEV-1169
  predicate-termination fix is the precedent; this needs its own correctness curve
  (recall vs examined-% vs `m_seeds`/`hops`), reported as a curve, never a peak.
- Build is GX10/engine-gated (fork patch, like the other tjs C work). The reference (A) +
  the host oracle are buildable/testable on the x86 standin.

## Alternatives rejected

- **Make `tjs()` itself accept multiple `src`.** Overloads the canonical single-source
  semantics and its early-termination bound; a separate `tjs_open` keeps each operator's
  termination proof clean.
- **Ship the composition (A) as the operator.** Materialises the reachable set → blocking
  → violates TR-1 (golden rule 1). Reference only.
- **External re-ranker / second round-trip.** Re-introduces the cross-system tax the whole
  thesis exists to remove.

## Addendum 2026-06-29 — realization (B) ranking / termination / fusion contract (Plan 007 host-reference spike)

Status: Proposed. Pins the two algorithmic holes the original (B) left to hand-tuning
(the ad-hoc `consecutive_drops` "bridges don't reset the drop counter" rule, and the
O(1) reachability-membership graph leg). Backed by a pure-host executable reference,
`bench/tjs_open_ref.py` (+ `tests/test_tjs_open_ref.py`, `make tjs-open-ref`), which the
GX10 C operator must reproduce within tolerance (its acceptance test, like
`join_order_ref.py` → `join_order.c`). External-research audit 2026-06-28 sourced the
three results; this addendum is the contract, not the derivation.

### 1. Ranking — bounded forward-push Personalized PageRank (Andersen–Chung–Lang, FOCS 2006)

Replace the in/out reachability membership with a **graded** graph-relevance score:

- Personalization vector = ANN top-`m_seeds` (same seed CTE as realization A), uniform
  weights for v1 (optional `1/passage_count` node-specificity is the same skew signal
  plan 006's degree stats expose — source from the metapage if 006 lands first; the host
  reference uses uniform and says so).
- **Priority-queue local push**: pop max-residue node `u`; move `alpha·residue(u)` to
  `reserve(u)`; spread `(1-alpha)·residue(u)/deg(u)` over out-neighbors; never push a node
  whose residue `< r_max`. `alpha = 0.15`. **`r_max` is the operator's `term_cond` analogue**
  — work is `O(1/(alpha·r_max))`, independent of |V| (this is the TR-1 bound on the graph
  leg). The reserves vector is the graph score; read incrementally, never sorted-to-
  convergence (the HippoRAG blocking trap is rejected).
- The C operator MUST count `nodes_examined` = distinct nodes whose residue was ever
  touched, and report it as the early-termination evidence.

Host validation: bounded-push top-k Jaccard vs power-iteration oracle ≥ 0.9 on synthetic
graphs (`test_ppr_topk_matches_power_iteration`); `nodes_examined` monotone in `1/r_max`.

### 2. Termination — NRA / FR best-worst bound (Fagin–Lotem–Naor PODS 2001; Schnaitter–Polyzotis TODS 2010)

Two descending-score streams: vector (sim) and PPR-reserve. Per seen candidate `d` keep

```
W(d) = Σ over KNOWN legs of score_leg(d)               # missing legs floored at 0
B(d) = W(d) + Σ over UNSEEN legs of frontier_ceiling_leg # frontier = last score pulled from that leg
```

**Stop** when `W` of the k-th-best settled candidate ≥ `max(B(d) for every candidate
outside the current top-k, plus the all-frontier ceiling for as-yet-unseen ids)`. This is
the FR bound. **A bridge needs no special case**: it is a candidate whose vector leg is
unseen, kept alive by `B(d)` until its `W(d)` settles — replacing the ad-hoc drop-counter
exemption in the original (B). Report `candidates_examined` at stop.

Host validation: `test_fr_bound_never_stops_before_confirmable` (FR top-k == full-merge
top-k, no false early stop), `test_fr_bridge_kept_alive_without_special_case`,
`test_fr_bound_terminates_early_when_possible`.

### 3. Fusion — RRF, windowed (Cormack et al., SIGIR 2009)

`score(d) = Σ_legs 1/(c + rank_leg(d))`, `c = 60`, 1-based ranks, over the vector-rank and
PPR-reserve-rank streams. **Rank-only** because score fusion is doubly fragile on the fork
(scalar `<->` returns 0 outside an index scan; PPR mass is on an incompatible scale).
Windowed (consume each leg only to a bounded depth) to stay non-blocking; a graph-high /
vector-low bridge gets a high PPR rank and is promoted with no score arithmetic — the
bridge-injection requirement, score-free.

Host validation: `test_rrf_promotes_graph_high_vector_low_bridge`,
`test_rrf_window_is_bounded`.

### 4. Measured curves on REAL HotpotQA (DATA-GATE CLOSED, 2026-06-29)

The full-corpus run is now MEASURED. `make tjs-open-ref` against the real
`data/hotpot/manifest.json` (1490 paragraphs, 745 edges, 150 graded questions; BGE 768-d
embeddings; `m_seeds=5`, `alpha=0.15`, `vec_limit=200`) — metrics in
`bench/results/tjs_open_ref_metrics.json`, table in
`docs/benchmark_tjs_open_ref_v0.1.0.md`. These are real numbers, not the earlier
synthetic-corpus proxy. **Verdict: POSITIVE.** PPR ranking + FR termination + RRF fusion
match-or-beat the (A) blocking oracle while touching < 1 % of the corpus.

**recall@10 vs `r_max` (the TR-1 curve).** Flat across the whole `r_max` sweep
(1e-2 → 5e-5) because the HotpotQA title-mention graph is sparse (mean reach ≈ 10–11 nodes
per query), so the local push converges almost immediately regardless of the floor:

| r_max | recall@10 vec | recall@10 ppr | recall@10 FR | recall@10 RRF | nodes examined | % corpus | cand examined |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1e-2 | 0.967 | 0.980 | 0.987 | 0.983 | 10.6 | 0.71 % | 171.2 |
| 1e-3 | 0.967 | 0.980 | 0.987 | 0.983 | 11.2 | 0.75 % | 171.6 |
| 5e-5 | 0.967 | 0.980 | 0.987 | 0.983 | 11.4 | 0.77 % | 171.6 |

**recall@5 strategy comparison (best `r_max` = 1e-2):**

| strategy | recall@5 |
|---|---:|
| vector_only | 0.883 |
| ppr_only | 0.863 |
| rrf_fused | 0.907 |
| fr_fused | 0.937 |
| **A_oracle (blocking)** | **0.883** |

What the real numbers establish:

- **PPR+FR+RRF matches/beats the (A) oracle.** FR-fused recall@5 **0.937** and RRF-fused
  **0.907** both exceed the blocking (A) oracle's **0.883** (which equals vector_only here,
  because A's neighbor-union rarely changes the top-5 on this graph). At recall@10, FR
  **0.987** > vector **0.967**. The streaming, TR-1-pure composition is not just *within
  tolerance* of the blocking reference — it surpasses it. (`grade()` is fraction of the 2
  gold supporting passages found, so these are joint multi-hop recalls.)
- **The TR-1 proxy holds with large margin.** `nodes_examined` ≈ 11 of 1490 nodes
  (**≈ 0.7 % of corpus**) at every `r_max` — bounded-push PPR touches a tiny graph
  fraction, exactly the early-termination evidence (A)'s materialized reach cannot give.
- **`ppr_only` alone underperforms vector_only at recall@5 (0.863 < 0.883)** — the graph
  leg is not a standalone retriever on this sparse graph; its value is as a *fusion signal*
  that promotes the vector-far bridge (RRF/FR both beat vector once fused). This is
  consistent with the prior ablation finding (graph helps as a bridge signal, not alone).
- **Negative/caveat — the `r_max` knob is inert on this distribution.** Because the graph
  is sparse, recall and `nodes_examined` are flat across the entire `r_max` sweep; the
  bound is not exercised. On a denser graph (Wiki-scale, MuSiQue/2Wiki) `r_max` will trade
  recall against examined-% as theory predicts — the curve here is a single operating point
  in disguise. The GX10/at-scale run is where `r_max` earns its keep.
- **Termination — FR vs `consecutive_drops`.** The baseline heuristic reaches recall@10
  **0.980** at `candidates_examined` as low as 20 (term_cond=10), while the FR bound
  examines ≈ 171 candidates for **0.987**. On this small corpus (vector window 200 ≈
  corpus-bounded reranked set) the FR bound is *looser in examined-count* than a tight
  drop-counter but recovers higher recall and needs no bridge special-case. The
  examined-fraction advantage of FR only becomes decisive at scale where the vector window
  is ≪ corpus; here it is correctness-clean, not examined-cheaper.

This addendum is the spec the realization-(B) fork patch is built against; its
recall/examined curve on real HotpotQA must match `bench/tjs_open_ref.py` within tolerance
(recall@10 FR ≈ 0.987, nodes_examined ≈ 0.7 % corpus) as the acceptance test. The
denser-graph `r_max` sweep remains a GX10/at-scale follow-up.

## Addendum 2026-07-01 — realization (B) shipped first-cut

Status: Accepted (first-cut engine operator shipped; refinement pending). Realization (B), the
fused early-terminating C operator, now **ships as a first-cut engine operator**
(`scripts/patches/tridb_tjs_open_operator.patch`, merged `3888d45`) — live recall@10 0.980 on
real HotpotQA (beating vector-only 0.967) via reachability-bridge injection + VBASE early
termination. The PPR-graded + FR-bound + RRF-fusion refinement specified in the 2026-06-29
addendum above (host-validated at recall@10 0.987) is the next iteration on top of this
first cut, not a prerequisite for it.

## Addendum 2026-07-17 — plan 095 SPIKE: PPR-graded scoring on the stock bounded iterator, GATED

Status: SPIKE, verdict below. Implements a `tjs.graph_scoring = membership | ppr` opt-in on the
stock `tjs_open` seedless path (`src/tjs_pg/tjs_pg.c`), riding the SAME `gph_traverse_bounded`/
`gph_traverse_typed` pull substrate ADR-0020 established, and measures it head-to-head against
`membership` on the real local HotpotQA corpus (1490 paragraphs, 745 title-mention edges — undirected
by insertion, both directions — 150 graded questions, BGE-768). This addendum does NOT change the
default (`membership` stays default; ADR-0020 decision 3 is unmodified) and does NOT supersede
ADR-0020 — it is the "documented follow-on" ADR-0020 §6 pointed to.

### 1. Implementation summary

- **GUC**: `tjs.graph_scoring` (`membership` default | `ppr`), `PGC_USERSET`, registered in
  `_PG_init`. Not a query-language parameter — the ADR-0008 pinned `tjs_open` surface is
  unchanged.
- **Default inertness (the hard gate)**: every code path plan 095 added is gated on
  `tjs_graph_scoring == TJS_SCORING_PPR`; the `membership` path is untouched control flow (the
  `ReachEntry.reserve` field it now carries is written but never read in membership mode). Full
  stock gate set (`make stock-graph-test PG_MAJOR=17`, 15 suites including the new
  `test/tjs_ppr_test.sql`) green on PG16 and PG17; `scripts/tjs_parity_test.sh` 11/11 green.
- **PPR mode (seedless only)**: bounded forward-push Personalized PageRank
  (Andersen-Chung-Lang, FOCS'06) over ALL seedless seeds together, personalization weight
  `sim_i = 1/(1+dist_i)` normalized to sum 1 (087's nearest-in-window seed selection preserved).
  Each out-edge enumerated — via `SELECT (graph_store.gph_traverse_typed($1,$2,0,-1)).dst`, a
  target-list (not FROM-clause) SRF call over the same `gs_open`/`gs_getnext` engine
  `gph_traverse_bounded` is built on — is one edge-step charged against the shared
  `tjs.graph_work_budget` pool (ADR-0020 decision 2 accounting, reused verbatim). `hops` bounds
  expansion exactly like the membership path. `alpha = 0.15`, `r_max = 1e-3` (host reference
  defaults, `bench/tjs_open_ref.py`) — fixed constants, not new GUCs.
  - **Documented deviation 1 (push order)**: queue-based FIFO local push, not strict
    max-residue-first priority pop. Forward push's fixed point is order-independent
    (Andersen-Chung-Lang's local push is confluent); FIFO changes only the number of push
    operations en route to it, never correctness. Avoids a decrease-key priority queue for this
    spike's bounded C operator.
  - **Documented deviation 2 (fusion → finalize, not incremental NRA/FR)**: the plan's fusion
    contract is the FR composition `bench/tjs_open_ref.py` measured as the winner (score =
    min-max-normalized vector similarity + min-max-normalized PPR reserve). We reproduce that
    SCORE exactly, but via the operator's existing bounded finalize-sort machinery over
    `<= topk.n + |reach|` candidates (never the full corpus, never a global reserve-map sort)
    rather than re-implementing the host reference's incremental two-leg NRA/FR streaming merge
    — an explicit deviation the plan allows ("materializing all reserves then sorting once at
    finalize over `<=k+bridges` is fine"). The operator's own `term_cond` consecutive-drops rule
    remains the TR-1 termination signal for the vector stream in both modes, unchanged.
  - **Documented deviation 3 (unified ranking, not "bridges only")**: PPR mode ranks the WHOLE
    finalize pool (vector-pool candidates ∪ graph-reached vertices) by the fused score, not just
    the graph-sourced/bridge share — because emitting the final order by two incompatible scales
    (raw vector distance for the pool, fused score for bridges only) would produce a meaningless
    merge. Non-graph-reached candidates get `reserve = 0`, so they compete purely on
    vector-similarity terms, same as membership mode's implicit behavior. The bridge-cap
    `floor(k/2)`-min-1 guarantee (087) is preserved: graph-sourced candidates get first claim on
    up to `bridge_cap` final slots (by fused score), the rest fill from the next-best fused-score
    candidates of any origin. Ties by `(score, id)`.
- **Fixture** (`test/tjs_ppr_test.sql`): 4-vertex deterministic graph, seed `S` with three
  out-edges to `A`, `B`, `C` (all vector-distance-tied) plus a fourth edge `C -> A` that
  reinforces `A` via a second path. Hand-computed forward push (shown in the file's header
  comment) gives `reserve(A) = 0.078625 > reserve(B) = reserve(C) = 0.0425`; PPR promotes `A`
  ahead of `B` (`{0,2,1,3}`) where membership's id tie-break puts `B` first (`{0,1,2,3}`) — the
  graded order visibly differs, exactly as required. A negative control confirmed this assertion
  FAILS pre-implementation (the `ppr` GUC value is accepted as a harmless custom-GUC placeholder
  pre-095, so the query silently ran the pre-existing membership code path and returned
  `{0,1,2,3}` instead of the graded order) — the engine now matches the hand derivation exactly.
  A default-inertness assertion (no `SET`, and explicit `SET ... = membership`) both confirm
  `{0,1,2,3}`.

### 2. Recall gate — membership vs PPR on real HotpotQA (stock engine)

Loader: `bench/hotpot_stock_gate.py` (`--gen-sql`) + `scripts/hotpot_stock_gate.sh` (build +
run + parse). Corpus loaded verbatim from `data/hotpot/manifest.json` /`corpus_emb.npy`
/`query_emb.npy` (the SAME data `bench/tjs_open_ref.py` uses) into a stock PG17
(`tridb/pg17-unfork:dev`, `graph_store_am` + `tjs_pg`, no fork) image: 1490 `paragraphs` rows
(`vector(768)`, HNSW `vector_l2_ops`), 1490 typed edges (745 `_edges` pairs inserted in BOTH
directions — undirected, matching the host reference's `build_adjacency`). All 150 questions run
through seedless `tjs_open` in both `tjs.graph_scoring` modes at `k ∈ {5,10}`,
`term_cond ∈ {8,32,128}`, `m_seeds=5`, `hops=4` (fixed — the host reference's PPR is
depth-unbounded; the engine bounds by `hops`, a required argument in both modes; 4 is generous
on this sparse graph — mean degree ≈ 1 — so the bound should not be the limiting factor,
disclosed as a host-vs-engine deviation), fixed `tjs.graph_work_budget` (ADR-0020 default,
65536). Graded with `bench.graphrag_report.evidence_scores` (`recall`, `joint`) against
`gold_ids` — the same reducer the plan names.

| mode | k | term_cond | n | recall | joint | graph_examined (mean) | censored fraction |
|---|---:|---:|---:|---:|---:|---:|---:|
| membership | 5 | 8 | 150 | 0.880 | 0.767 | 46.7 | 0.000 |
| membership | 5 | 32 | 150 | 0.880 | 0.767 | 46.7 | 0.000 |
| membership | 5 | 128 | 150 | 0.880 | 0.767 | 46.7 | 0.000 |
| membership | 10 | 8 | 150 | 0.963 | 0.927 | 46.7 | 0.000 |
| membership | 10 | 32 | 150 | 0.963 | 0.927 | 46.7 | 0.000 |
| membership | 10 | 128 | 150 | 0.963 | 0.927 | 46.7 | 0.000 |
| ppr | 5 | 8 | 150 | 0.927 | 0.860 | 113.2 | 0.000 |
| ppr | 5 | 32 | 150 | 0.927 | 0.860 | 113.2 | 0.000 |
| ppr | 5 | 128 | 150 | 0.927 | 0.860 | 113.2 | 0.000 |
| ppr | 10 | 8 | 150 | 0.983 | 0.967 | 113.2 | 0.000 |
| ppr | 10 | 32 | 150 | 0.983 | 0.967 | 113.2 | 0.000 |
| ppr | 10 | 128 | 150 | 0.983 | 0.967 | 113.2 | 0.000 |

All 150 questions scored, both modes, every point (no dropped queries; `n=150` throughout).
`censored fraction = 0.000` everywhere — no query hit `tjs.graph_work_budget` (mean
`graph_examined` is 46.7 membership / 113.2 ppr edge-steps, both `< 0.1 %` of the shared
65536-edge-step budget and a small fraction of the 1490+1490-edge corpus) — the comparison is
uncensored in both modes, so the recall delta below is not an artifact of one mode being capped
tighter than the other. `term_cond` is INERT across `{8, 32, 128}` on this corpus (every row is
byte-identical across the three term_cond values within a mode) — the same "small/sparse corpus
doesn't exercise the termination knob" finding the 2026-06-29 host addendum reported for `r_max`,
now confirmed for the engine's `term_cond` too on this same corpus.

**PPR beats membership at every measured point**: recall@5 **0.927 vs 0.880** (+4.7 pt), joint@5
**0.860 vs 0.767** (+9.3 pt); recall@10 **0.983 vs 0.963** (+2.0 pt), joint@10 **0.967 vs 0.927**
(+4.0 pt). Cost: PPR's forward push touches ~2.4x the edge-steps membership's BFS does (113.2 vs
46.7 mean) — real but still `<0.1%` of the shared budget, not a practical concern at this scale.

**Host-reference context** (labeled host, not engine — `docs/decisions/0012-tjs-open-multiseed-retrieval.md`
2026-06-29 addendum, `bench/tjs_open_ref.py`, real HotpotQA, `m_seeds=5, alpha=0.15,
vec_limit=200`): recall@5 `fr_fused = 0.937` vs the blocking `A_oracle = 0.883`; recall@10
`FR = 0.987` vs `vector_only = 0.967`; `nodes_examined ≈ 11` (`≈ 0.7 %` of the corpus). These are
HOST numbers (pure Python, unbounded incremental NRA/FR merge, no engine machinery) — not
directly comparable point-for-point to the engine table above (different termination mechanism,
different `hops` treatment, different corpus load path), but they establish the CEILING the
graded composition is capable of when its incremental merge is not compressed into a bounded
finalize sort.

### 3. Verdict

**GO** (opt-in research knob; NOT a default flip). PPR-graded scoring beats reachability-membership
at every measured operating point on the real, local, engine-loaded HotpotQA corpus — recall@5
+4.7 pt, joint@5 +9.3 pt, recall@10 +2.0 pt, joint@10 +4.0 pt — with identical inputs (same
corpus, same 150 questions, same `m_seeds`/`hops`/budget), both modes uncensored throughout
(`censored fraction = 0.000`), so the delta is a genuine ranking-quality win, not a side effect
of one mode being budget-capped tighter than the other. This corroborates the 2026-06-29 host
addendum's headline (FR-fused beats vector-only/the blocking oracle) with a SEPARATE, engine-
measured, honestly-capped number — the host and engine results agree in direction even though
their magnitudes are not directly comparable (different corpus load path, different termination
mechanism, `hops`-bounded push here vs the host's unbounded push, engine's finalize-sort fusion
vs the host's incremental NRA/FR merge — see deviations 1-3 above). The cost is real (~2.4x the
graph-leg edge-steps) but negligible relative to the shared budget at this scale.

This is a SPIKE verdict, not a ship decision: `tjs.graph_scoring` stays a `PGC_USERSET` research
knob, default `membership`, ADR-0020 decision 3 unmodified. Per plan 095's explicit scope, default
adoption is its own ADR (would supersede ADR-0020 decision 3) and needs the wiki/wikidata-scale
re-measure below before anyone proposes it — HotpotQA is small (1490 nodes) and sparse (mean
degree ≈ 1), and the 2026-06-29 addendum already found `r_max` inert on this exact corpus; §2
above found `term_cond` equally inert here. Neither knob has been exercised under real pressure
yet, so this GO is "the graded composition measurably helps and costs little at HotpotQA scale,"
not "the composition is proven at the scale that would justify defaulting it."

### 4. What would change the verdict

- A GO would still need the wiki/wikidata-scale re-measure (denser graph, `ADR-0012`'s own
  caveat that `r_max` is inert on HotpotQA's sparse title-mention graph) before any default-flip
  ADR is proposed — this spike is HotpotQA-only, small-corpus, `alpha`/`r_max` fixed at host
  defaults, no sweep of those two knobs.
- If a future pass ports the host reference's incremental NRA/FR merge into the C operator
  (replacing deviation 2's bounded finalize sort), re-measure before concluding the finalize-sort
  simplification cost real recall — this spike did not isolate that variable.

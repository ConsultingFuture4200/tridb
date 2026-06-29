# Plan 007: De-risk the `tjs_open` (B) operator with a host reference — bounded forward-push PPR ranking + NRA/FR termination + RRF fusion

> **Executor instructions**: This is a **research/design spike**, not a production change. Build the
> host reference, run the measurements, write the design note. Follow each step; run every
> verification command. If a "STOP condition" occurs, stop and report. Update this plan's row in
> `advisor-plans/README.md` when done.
>
> **Drift check (run first)**:
> `git diff --stat 8b19cb5..HEAD -- bench/ docs/decisions/0012-tjs-open-multiseed-retrieval.md`
> On any change to the cited files, compare excerpts to live code before proceeding.
>
> **Hardware gate**: the *deliverable here is the host reference + measurements + design note*, all
> of which run on this x86 box (Python, `make test`). The fused C operator (`tjs_open` realization B)
> they de-risk is **GX10/engine-gated** and is explicitly NOT built in this plan — this plan exists so
> that the GX10 build, when it happens, ships a *proven* algorithm instead of hand-tuned guesses.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW (additive host code + a design note; touches no engine code, no shipped path)
- **Depends on**: none (independent of 006; complementary — 006's degree stats feed the PPR seeding
  weight described in Step 2)
- **Category**: direction / research spike
- **Planned at**: commit `8b19cb5`, 2026-06-28
- **Horizon**: v2 (de-risks the committed next build)

## Why this matters

`tjs_open` (ADR-0012) is **the committed next build**: the seedless multi-seed operator that turns
TriDB from a single-source constrained-traversal engine (HotpotQA recall@10 **0.223**) into a real
open-domain GraphRAG retriever (the tuned multi-store baseline gets **0.953**). ADR-0012 specifies the
fused, TR-1-pure realization (B) but leaves two algorithmic holes filled by hand-tuning:

1. **Termination.** It reuses VBASE `consecutive_drops` with an *ad-hoc* rule — "injected past-frontier
   bridge candidates don't reset the drop counter." That is a band-aid, and the GTM doc already warns
   `term_cond` must be pinned per-metric and never mixed (the old SM-2 headline was measured on a
   termination bug).
2. **Ranking.** It models the graph leg as an **O(1) reachability membership set** — a candidate is
   in or out. That throws away *how relevant* a reached node is, which is exactly the signal that makes
   multi-hop retrieval work.

An external-research audit (2026-06-28) found these two holes are each closed by a precise, citable
result, and the two results compose:

- **Rank-join termination theory** — Fagin–Lotem–Naor **NRA** (PODS 2001) and Schnaitter–Polyzotis
  **FR-bound / FRPA** (TODS 2010) — gives a *provable* stopping rule for a multi-source ranked merge
  with a best/worst-score bookkeeping that makes the "injected bridges" special-case unnecessary: a
  bridge is safe to emit-or-skip based on whether its best-possible aggregate can still beat the
  running k-th worst. Constant buffer, streaming → **TR-1-compatible**.
- **Bounded forward-push Personalized PageRank** — Andersen–Chung–Lang (FOCS 2006) — gives a *graded*
  graph relevance score with the ANN top-`m_seeds` as the personalization vector. Critically it is
  **local and bounded**: a residue threshold `r_max` plays the exact role of `term_cond`, work is
  `O(1/(α·r_max))` independent of graph size, and a priority-queue-over-reserves variant emits top-k
  incrementally → fits Open/Next/Close. **The trap to avoid:** HippoRAG (NeurIPS 2024) runs PPR *to
  convergence then sorts all passages* — that is blocking, forfeits TR-1, and is explicitly rejected.
- **RRF (Cormack, SIGIR 2009)** — rank-only fusion of the vector-ranked stream and the PPR-reserve
  stream. The fork quirk makes scalar `<->` return 0 outside an index scan and PPR mass lives on an
  incompatible scale, so **score-based fusion is doubly fragile**; RRF uses only ranks and naturally
  promotes a graph-high/vector-low bridge — exactly the bridge-injection requirement, without scores.

The spike answers, on real data, **before** committing GX10 C: does PPR-graded + NRA-bounded + RRF-fused
retrieval (a) match the blocking composition oracle's recall, and (b) do it while examining a bounded
fraction of the graph (the TR-1 evidence)?

## Current state

- `docs/decisions/0012-tjs-open-multiseed-retrieval.md` — the operator spec. Realization (A) is the
  **blocking composition oracle** (materializes seeds + reachable set; reference only). Realization (B)
  is the fused TR-1 operator (GX10-gated). Quote (ADR-0012 §2 B): *"a graph-reachable candidate is
  admitted to the heap even when its vector rank is past the frontier, but it does NOT reset the drop
  counter (so termination still holds)."* — this plan replaces that ad-hoc rule with the NRA/FR bound.
- `bench/v2a_open.py` — the **existing (A) host oracle**. It emits SQL composing `ANN top-seeds` ∪
  `graph_store.neighbors(seed)`, vector-reranked (`bench/v2a_open.py:33-80`), and grades recall@k vs
  gold (`grade()`, lines 102-113). It already recovers recall@10 ≈ 0.953 on HotpotQA (STATUS.md). This
  is the recall oracle the new ranking must match — reuse its manifest format and `grade()`.
- `bench/graphrag_report.py` — the graph-inject host reference (the +15.6 pt bridge-injection result).
- `data/hotpot/manifest.json` — the HotpotQA dev slice (paragraphs, questions, `gold_ids`, `_edges`,
  `corpus_emb_path`, `query_emb_path`). The manifest schema is visible in `v2a_open.py` (`m["paragraphs"]`,
  `m["questions"]` with `qid`/`gold_ids`, `m["_edges"]` as `(src,dst)` pairs).
- `bench/recall_decay.py` — a good structural model for a **pure-host** bench that grades against a
  numpy oracle (no engine), with a `make` target. Model the new module's CLI/IO after it.

Conventions to honor:
- Pure Python, `ruff`-clean, `pytest`-tested; `numpy` available; **no `ANTHROPIC_API_KEY`** here
  (do not add an LLM-reader step — recall@k against `gold_ids` is the metric, like `v2a_open.py`).
- Report a **curve, not a point** — recall vs. `r_max`/`term_cond` vs. fraction-of-graph-examined
  (the discipline the TJS-termination memory and GTM R1 demand; never a bare peak number).
- TR-1 framing: the host prototype must **count graph nodes touched** and show it stays a small
  fraction of the corpus, since "examined-%" is the in-host proxy for the engine's early termination.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Python tests | `make test` | pytest passes, exit 0 |
| Lint | `make lint` | ruff clean, exit 0 |
| Run the new spike (emit metrics) | `python -m bench.tjs_open_ref --manifest data/hotpot/manifest.json` | writes a metrics JSON + prints a recall/examined curve |
| The (A) oracle to match | `python -m bench.v2a_open --manifest data/hotpot/manifest.json --emit-sql /tmp/a.sql` then run on engine, OR reuse its host `grade()` | recall@10 ≈ 0.95 reference |

## Scope

**In scope** (create/modify):
- `bench/tjs_open_ref.py` (create) — the host reference implementing the three algorithms below.
- `tests/test_tjs_open_ref.py` (create) — unit tests for the PPR/NRA/RRF primitives.
- `Makefile` — add a `tjs-open-ref` target mirroring `recall-decay` (host-only, no image guard).
- `docs/decisions/0012-tjs-open-multiseed-retrieval.md` — **append an addendum** (do not rewrite —
  CLAUDE.md convention) recording the spike's findings and the chosen termination/ranking/fusion rule
  for realization (B). OR write a sibling design note `docs/tjs_open_ranking_v0.1.0.md` if the addendum
  would exceed ~1 screen.

**Out of scope** (do NOT touch):
- Any engine/C code (`vendor/MSVBASE/`, `src/`, `scripts/patches/`) — realization (B) is GX10-gated and
  is a *separate* build that consumes this plan's findings.
- `bench/v2a_open.py` — keep the (A) oracle intact; reuse it, don't edit it.
- The shipped `tjs()` single-source operator and its tests.

## Steps

### Step 1: Bounded forward-push PPR over the HotpotQA graph (the ranking primitive)

In `bench/tjs_open_ref.py`, load the manifest (reuse `v2a_open.py`'s manifest reading + embeddings
load). Build an adjacency dict from `m["_edges"]`. Implement **bounded forward-push PPR**
(Andersen–Chung–Lang) with:
- personalization vector = the ANN top-`m_seeds` nodes (compute via numpy: top-`m_seeds` by L2 to the
  query embedding, exactly as `v2a_open.py`'s seed CTE does in SQL), optionally weighted by node
  specificity `1/passage_count` (HippoRAG node-specificity) if available — note this is the same skew
  signal plan 006's degree stats expose; if unavailable, use uniform seed weights and say so.
- a **priority-queue variant**: repeatedly pop the node with max residue, push `α` of its residue to
  its reserve and spread `(1-α)` over its out-neighbors, stop pushing a node once its residue < `r_max`.
- α default 0.15 (standard); `r_max` is the swept knob.
- **instrument** the number of distinct nodes whose residue was ever touched (`nodes_examined`) — this
  is the TR-1 proxy.

The reserves vector is the graded graph-relevance score. Verify it matches a **blocking** PPR oracle
(power-iteration to convergence) on its top-k:

**Verify**: a unit test (`tests/test_tjs_open_ref.py`) asserting the bounded-push top-k Jaccard vs. the
power-iteration top-k ≥ 0.9 on a small synthetic graph, AND that `nodes_examined` shrinks as `r_max`
rises. `make test` → passes.

### Step 2: NRA / FR-bound termination over the (vector-rank, PPR-reserve) merge

Implement the multi-source ranked merge as two sorted streams — the vector stream (ascending L2 →
descending similarity) and the PPR-reserve stream (descending reserve) — and apply the **NRA
best/worst-score bound**: maintain, per seen candidate, a worst-score `W` (aggregate of known partial
scores, missing legs at their floor) and a best-score `B` (missing legs at the current frontier
ceiling of each stream). **Stop** when the k-th largest `W` ≥ every unseen candidate's `B` (the FR
bound). Report `candidates_examined` at stop.

Compare this principled stop to the current `consecutive_drops`/`term_cond` heuristic by implementing
both and plotting recall@10 vs candidates-examined for each. The claim to confirm or refute: **the FR
bound reaches equal-or-better recall at equal-or-lower examined**, and **bridges need no special
case** (a bridge is just a candidate whose vector leg is missing/past-frontier but whose PPR `B` keeps
it alive until its `W` is settled).

**Verify**: the spike's metrics JSON contains, for a sweep of `r_max` (and a sweep of `term_cond` for
the baseline), `{recall_at_10, nodes_examined, candidates_examined}`; a test asserts the FR-bound run
never emits a top-k member it could not have confirmed (no false early stop on a held-out small graph).

### Step 3: RRF fusion of the two streams

Implement RRF: `score(d) = Σ_legs 1/(c + rank_leg(d))` (c=60 default) over the vector-rank and
PPR-reserve-rank streams, windowed so it stays non-blocking (consume the next item from whichever leg;
bounded window). Compare joint multi-hop recall@5 of RRF-fused vs. the (A) oracle's
similarity-ranked-with-injection on HotpotQA bridge questions.

**Verify**: metrics JSON includes `recall_at_5` for `{vector_only, ppr_only, rrf_fused, A_oracle}`;
RRF-fused ≥ vector_only and within a stated tolerance of `A_oracle`. `make test` green.

### Step 4: Write the design note / ADR-0012 addendum

Append to `docs/decisions/0012-tjs-open-multiseed-retrieval.md` (or the sibling note) a short addendum
recording: the chosen ranking (bounded forward-push PPR), the chosen termination (FR/NRA bound, with
the exact `W`/`B` definitions the C operator must implement), the chosen fusion (RRF, windowed), and
the measured curves (recall vs `r_max` vs examined-%). State explicitly which numbers are host-proxy
and which require the GX10 engine to confirm. This addendum is the contract the realization-(B) fork
patch is built against.

## Test plan

- `tests/test_tjs_open_ref.py` (new, runs here): (a) bounded-push PPR top-k vs power-iteration oracle on
  a fixed small graph; (b) `nodes_examined` monotonic in `r_max`; (c) FR-bound never stops before a
  top-k member is confirmable; (d) RRF promotes a graph-high/vector-low synthetic bridge above a
  vector-mid/graph-zero distractor. Model structure after `tests/` existing bench tests.
- Verification: `make test` → all pass including the new file; `make lint` clean.

## Done criteria

ALL must hold:
- [ ] `make test` exits 0; `tests/test_tjs_open_ref.py` exists and passes.
- [ ] `make lint` exits 0.
- [ ] `python -m bench.tjs_open_ref --manifest data/hotpot/manifest.json` writes a metrics JSON with
      the recall/`r_max`/examined curve and the `{vector_only, ppr_only, rrf_fused, A_oracle}` recall@5.
- [ ] The metrics show PPR+FR+RRF recall@10 within a stated tolerance of `bench/v2a_open.py`'s (A)
      oracle while `nodes_examined` ≪ corpus (the TR-1 proxy), reported as a **curve**.
- [ ] ADR-0012 addendum (or `docs/tjs_open_ranking_v0.1.0.md`) records the chosen W/B termination
      definitions, the PPR parameters, and the RRF window — enough for a GX10 C executor to implement
      realization (B) without re-deriving the algorithm.
- [ ] No engine/C files modified (`git status`; `git diff --stat 8b19cb5..HEAD -- vendor/ src/ scripts/patches/` empty).
- [ ] `advisor-plans/README.md` status row updated.

## STOP conditions

- The `data/hotpot/manifest.json` schema differs from what `bench/v2a_open.py` reads (drift) — report;
  do not guess field names.
- Bounded-push PPR does **not** approach the convergence oracle's top-k even at small `r_max` — this
  would mean the graph is too sparse for PPR to help (consistent with the ablation finding that graph
  helps Wiki-bridge but not news); report the negative result honestly, it still decides the operator.
- The FR-bound run examines essentially the whole graph before stopping (no early termination on this
  data) — report it; it means the bound is loose on this distribution and the C operator needs a
  different aggregation, which is a design finding, not a failure to hide.

## Maintenance notes

- This host reference is the **executable specification** for the GX10 realization-(B) fork patch
  (like `join_order_ref.py` is for `join_order.c`). When (B) is built, its recall/examined curve must
  match this reference's within tolerance; wire that as the acceptance test.
- The PPR seeding weight (`1/passage_count` node specificity) overlaps plan 006's degree stats — if 006
  lands first, the engine can source the skew weight from the metapage; note the dependency.
- A reviewer should scrutinize: that the "examined" counters are honest (every node/candidate touched
  is counted), and that no step silently materializes the full reachable set (that would make the host
  reference a second copy of the (A) *blocking* oracle rather than a model of the streaming operator).
- Deferred: calibrated/learned fusion (RRF is the safe default first); the actual GX10 C operator; any
  MuSiQue/2Wiki extension (that is the separate reporting item, finding #13 in the audit).

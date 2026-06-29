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

# TriDB — Open-Source Launch & Credibility Plan (v0.1.0)

**Goal:** establish TriDB as a credible open-source project — reputation, contributors,
users — off a defensible proof. Not fundraising, not a paper (yet). 2026-06-25.

## TL;DR

The one move that is *both* proof and launch: **a reproducible benchmark on a recognized
public Omni-RAG/GraphRAG dataset, against a tuned real-world multi-store baseline, with a
one-command repro and an honest writeup.** Everything else (HN, Reddit, Twitter) points at
that artifact. Lead the story with **one-system + local-hardware (DGX Spark)**, support it
with performance, and be ruthlessly honest about scope and the answer-quality data — because
the honesty is the differentiator that survives HN.

## Addendum 2026-06-26 — gate progress (NEON + measured latency)

Two of the blockers this plan named have moved; recorded here (append, not rewrite):

- **R1 "report latency in ms at the operating point" — now has a real number (moderate scale).**
  The ARM **NEON** distance kernel ([[DEV-1234]]) landed and un-sandbagged the engine. On the
  GX10, the canonical `tjs()` query at the recall@10 = 100% operating point runs in **~1.8 ms
  median at 2.18% of the corpus examined** (20k×128); HNSW index build dropped **4.2×** (47.8 s →
  11.3 s, same corpus). HNSW build-quality `m` / `ef_construction` are now **reloptions**
  ([[DEV-1286]]) and a high-quality index builds in ~5 s. Full curve + method:
  [[benchmark_neon_sweep_v0.1.0]]. **The 100k / dim-768 headline curve is now run (NEON, GX10):**
  recall@10 **96.25% at ~36 ms / 3.3% examined** (`term_cond=20`) → **100% at ~41 ms / 4.4% examined**
  (`term_cond=1000`), every point under the 25% TR-1 ceiling — the real recall/effort/latency curve at
  scale (the "toy scale" rebuttal). **Still gated:** a *fair multi-system SM-2 head-to-head* (latency
  here is TriDB-side only), and the public-dataset value claim (below).
- **R3 "synthetic-benchmark credibility" — the public-dataset path now exists in tooling.**
  `tools/real_corpus.py` ([[DEV-1284]]) loads real embedding datasets (`.npy/.fvecs/.hdf5`),
  synthesizes the topical graph, and emits the identical canonical-query harness with an exact
  recall oracle. Still TODO: pick + pin a recognized public dataset and a tuned multi-store
  baseline, and the one-command repro (the make-or-break item below). Tooling no longer blocks it.

Net: the *mechanism + on-target latency* are now real; the *public-workload value claim* and the
*at-scale recall curve* remain the to-do list. Do not launch before the 100k/768 curve and a
public-dataset run with one-command repro.

## Where we actually stand (don't oversell this)

| Asset | State | Proof value |
|---|---|---|
| Architecture (1 Postgres, 1 txn/WAL, global top-k, early term) | Real, lineage-backed (VBASE OSDI'23 / AkasicDB SIGMOD'26 / Chimera) | High — hard to dispute |
| Builds + engine suite on GX10 (GB10, aarch64) | Done this session (47 PASS) | Medium — "it runs on a Spark" is a story |
| SM-2 latency, 100k/dim-768, GX10 | 12.6× vs baseline, 12/12 | Medium — speed thesis holds at scale |
| Answer parity vs baseline @ scale | **recall/effort curve: 58.5% → 100% exact across `term_cond`** (see R1) | **Fixed (DEV-1169) — a curve, not a point**; see Risk R1 |
| Corpus + queries | **Synthetic, self-generated** | **Low** — must be replaced for external credibility |

The honest summary: the *mechanism* is proven; the *value claim on a workload strangers
recognize* is not yet. That gap is the entire to-do list.

## Part 1 — Proof plan (the credibility ladder)

1. **Self-benchmark (done).** Establishes the mechanism. Necessary, not persuasive.
2. **Public-dataset benchmark vs tuned baseline (the unlock).** Build this next.
3. **Third-party reproduction.** A stranger running the repo and confirming. The gold standard;
   engineer for it from day one.

**The artifact to build (highest leverage):**
- **Dataset:** a recognized one — multi-hop QA (HotpotQA / 2WikiMultihop) over a real corpus,
  or a standard GraphRAG eval set. Real embeddings (dim 768+), real graph topology, real text.
- **Baseline:** the stack people actually run — LlamaIndex/LangChain multi-store, or
  Milvus+Neo4j+pgvector *tuned* (configs committed). Not a strawman. Invite others to beat it.
- **Metrics that matter to RAG builders:** recall@k and downstream answer accuracy **at fixed
  latency** — not raw latency alone. A faster wrong answer is worth nothing.
- **Repro:** one command (`make bench-public` or a Docker one-liner). Pinned data, pinned seeds.
- **Scaling curve**, not a single point.

**Preempt every attack in the writeup:**

| Attack | Neutralizer |
|---|---|
| "Synthetic corpus" | Public dataset, cited, downloaded by the harness |
| "Strawman baseline" | Real tuned multi-store, configs public, "beat it" invitation |
| "Toy scale" | GX10 100k/768 + scaling curve |
| "You wrote both sides" | One-command public repro |
| "Speed, but is the answer right?" | recall@k + QA accuracy at fixed latency; SM-4 oracle parity reported honestly |

## Part 2 — Repo readiness (open-source hygiene)

- [x] LICENSE (MIT) — done, matches MSVBASE upstream.
- [x] README as funnel — done; tighten the one-liner (below).
- [ ] **"Why does this exist vs AkasicDB?"** — one paragraph, prominent. Answer: *open,
      Postgres-native, runs locally on a Spark, fully reproducible.* Own it before a commenter writes it.
- [ ] **One-command repro** for the public benchmark. This is the make-or-break item.
- [ ] CI green + a CI badge that isn't aspirational (the workflow must actually run the buildable layer).
- [ ] CONTRIBUTING.md + good first issues (you want contributors — give them a door).
- [ ] Honest STATUS / "v1 scope & non-goals" section (one canonical query, three stores, BM25 closed).
- [ ] Architecture diagram + a 2-minute asciinema/loom of the canonical query running on the Spark.

## Part 3 — Launch sequence (do not skip step 1)

| # | Channel | Why | Gate |
|---|---|---|---|
| 1 | **Technical writeup + repro repo** | The asset everything points to | Bulletproof before anything ships |
| 2 | **r/LocalLLaMA** | Local hardware + Spark is catnip; friendly first audience | After 1 |
| 3 | **Show HN** | Systems/DB crowd; lineage + repro + Spark plays | Only if repo survives scrutiny |
| 4 | **X/Twitter** | pgvector/Postgres community (Postgres-native = built-in audience), DB systems, GraphRAG, Grace-Blackwell crowd | Rolling |
| 5 | **RAG ecosystem** (LlamaIndex/LangChain, MS GraphRAG discourse) | Position as *the storage layer for GraphRAG* | After 1 |

Postgres-native is a distribution advantage — lean on the pgvector ecosystem; it's a large,
reachable, relevant audience that already trusts the substrate.

## Messaging

- **One-liner:** "Tri-modal RAG — vector + graph + relational in one Postgres query plan,
  running on a DGX Spark."
- **The hook:** one system instead of three; the whole stack on one local GB10 box.
- **Credibility anchor (say it early):** descends from VBASE (OSDI'23), AkasicDB (SIGMOD'26),
  Chimera (PVLDB). Peer-reviewed lineage buys you the reader's next five minutes.

## Risks & non-goals

- **R1 (FIXED — but report it as a CURVE, never a headline): TJS early-termination correctness.**
  The first 100k/dim-768 GX10 run exposed a predicate-blind early-termination bug: tjs() counted
  graph/relational predicate rejections as VBASE "drops", so a selective predicate tripped
  `term_cond` before the top-k filled → empty/partial results (SM-4 = 5%). Fixed
  (`tridb_tjs_predicate_termination.patch`): a drop now means only past-frontier (PQ full AND
  distance ≥ k-th); predicate rejections don't count; a selective predicate drains the ANN stream
  instead of bailing. **This is a CORRECTNESS fix, not a performance win — frame it that way.**
  The honest result is a recall/effort **curve**, and the writeup MUST show the whole table, not
  the peak (Linus review — leading with "100%" while the default gives 58% is benchmark laundering):

  | `term_cond` | SM-4 exact-parity | SM-3 corpus examined | note |
  |---|---|---|---|
  | 50 (shipped default) | 58.5% | 3.6% | fast, approximate operating point |
  | 5000 | 97.2% | 10.9% | |
  | 10000 | 100% | 20.1% | exact, still < 25% TR-1 ceiling |

  Honest framing to publish: *before the fix the default returned empty answers (the bug); after
  the fix the default gives 58.5% recall at 3.6% examined; `term_cond` then trades recall for
  effort up to 100% recall at 20.1% examined.* Pin a `term_cond` per reported metric; never mix
  the 50-default TR-1 number with the 10000 recall number in one breath.
  **Two things still gated before any public benchmark:** (a) report **latency in ms** at the
  chosen operating point vs. a naive full-scan-filter baseline — examined-% alone won't answer the
  hostile "why not pgvector + SQL graph filter?"; (b) the steep 3.6%→20% jump is a real limitation
  of running predicates on an ANN stream (HNSW ordering doesn't track predicate-passers) — state it.
- **R2: "clone of AkasicDB."** Answer the why-it-exists question or it defines you.
- **R3: synthetic-benchmark credibility.** The public-dataset artifact retires this; don't
  launch on self-generated data.
- **Non-goals for v1 launch:** general query surface (one canonical query only), BM25,
  the literal 128 GB memory-saturation run (harness is INSERT-bound; needs COPY rework).

## Next two weeks (concrete)

1. Land the SM-4 number; write up R1 honestly either way.
2. Build the public-dataset benchmark + tuned baseline + one-command repro.
3. Draft the technical writeup (architecture → lineage → public benchmark → honest limits).
4. Repo hygiene checklist above; record the 2-min Spark demo.
5. Soft-launch r/LocalLLaMA → iterate → Show HN.

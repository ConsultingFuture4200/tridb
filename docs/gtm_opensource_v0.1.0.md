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

## Addendum 2026-06-28 — benchmark suite shipped + a POSITIONING finding (read this)

This session built and ran the public-workload benchmark suite the plan called for, and
the real-workload head-to-head (GTM #1) surfaced a finding that should reshape the launch claim.

- **Closed (real, recognized data):** GraphRAG QA on HotpotQA (real embedding-independent graph;
  graph-INJECT lifts multi-hop joint evidence recall +15.6pt vs vector-only — `benchmark_graphrag_v0.1.0.md`);
  filtered vector search GX10 SIFT-1M (recall@10=1.000, latency drops as the filter tightens —
  `benchmark_filtered_v0.1.0.md`); tri-modal fusion ablation on MultiHopRAG with query-parsed
  (no-leakage) constraint (`benchmark_ablation_v0.1.0.md`); recall-decay (no decay at scale).
- **PROVEN LIVE ON THE GB10:** one-WAL cross-modal consistency under churn (FR-7 atomicity 200-iter
  zero divergence + crash recovery) — a differentiator bolt-on Milvus+Neo4j+pg cannot match.

- **THE FINDING (GTM #1, `benchmark_h2h_v0.1.0.md`):** on HotpotQA the canonical single-`src`
  `tjs()` retrieves **recall@10 0.223 vs a tuned multi-store's 0.953** — faster (1.8 vs 6.7 ms) but
  at far lower recall. Root cause (confirmed: term_cond 0→5000 moved recall only 0.223→0.227, so it
  is NOT early-termination): **`tjs()` is a SINGLE-SOURCE constrained-traversal operator, not an
  open-domain retriever** — it ranks vectors only within one `src`'s graph-reachable set. The
  +15.6pt graph result is a HOST-side prototype (multi-seed + bridge injection) the engine does NOT
  execute in v1.

- **Positioning implication — do not launch v1 as a drop-in open GraphRAG retriever.** What v1
  actually wins:
  1. **Source-anchored tri-modal queries** ("given entity X, find vector-similar entities reachable
     from X, filtered"): SM-2 = 12/12 at median 15.1× lower latency (2k/dim-32, x86 standin) with exact parity (`benchmark_sm2_v0.1.0`).
  2. **One system, one WAL, transactional across all three stores** (proven on the GB10) — the
     consistency story bolt-on stacks can't tell.
  Lead with those. The open multi-hop retrieval claim needs a **multi-seed retrieval operator (v2)**;
  until then it is a research prototype, not an engine feature. (Decision for the operator: reframe
  the launch to source-anchored + consistency, or fund the v2 multi-seed operator first.)

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
  scale (the "toy scale" rebuttal). **SM-2 fair head-to-head now run** (2k/32, live tuned
  Milvus+Neo4j+Postgres): TriDB wins **12/12** at **median 15.1× lower latency** (2k/dim-32, x86 standin; ~1 ms vs ~16 ms) with
  **exact answer parity** (Jaccard 1.0) — [[benchmark_sm2_v0.1.0]]. (A fair head-to-head at 100k scale
  is the remaining stretch.)
- **R3 "synthetic-benchmark credibility" — the public-dataset path now exists in tooling.**
  `tools/real_corpus.py` ([[DEV-1284]]) loads real embedding datasets (`.npy/.fvecs/.hdf5`),
  synthesizes the topical graph, and emits the identical canonical-query harness with an exact
  recall oracle. **First real run done:** `sift-128-euclidean` pinned (verified SHA256) + fetched + run
  LIVE (50k slice) — recall@10 **100% at ~4% examined** (`term_cond≈1000`); the **default `term_cond=50`
  gives only 16%**, so real clustered data needs a deeper scan than the synthetic implied
  ([[benchmark_public_v0.1.0]]). `make fetch-dataset && make bench-public` is the one-command repro.
  Still TODO: the **dim-960 GIST headline** (`PUBLIC_LIMIT=100000` on a networked GX10).

Net: the *mechanism + on-target latency* are now real; the *public-workload value claim* and the
*at-scale recall curve* remain the to-do list. Do not launch before the 100k/768 curve and a
public-dataset run with one-command repro.

## Where we actually stand (don't oversell this)

| Asset | State | Proof value |
|---|---|---|
| Architecture (1 Postgres, 1 txn/WAL, global top-k, early term) | Real, lineage-backed (VBASE OSDI'23 / AkasicDB SIGMOD'26 / Chimera) | High — hard to dispute |
| Builds + engine suite on GX10 (GB10, aarch64) | Done this session (47 PASS) | Medium — "it runs on a Spark" is a story |
| SM-2 latency, 2k/dim-32, x86 standin | 15.1× vs baseline, 12/12 | Medium — speed thesis at toy scale; 100k+/GX10 head-to-head pending (DEV-1284) |
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
| "Speed, but is the answer right?" | recall@k + QA accuracy at fixed latency; SM-4 oracle parity reported honestly; **GraphRAG QA on real HotpotQA + a real (embedding-independent) graph — multi-hop joint evidence recall +15.6 pts @ k=5 vs vector-only (`docs/benchmark_graphrag_v0.1.0.md`, Plan 015)** |
| "Graph just re-encodes the vectors" | Graph is real title-mention topology (rebuild with any encoder, edges don't move); naive graph-rerank is shown to NOT help — only injecting real bridges does |

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

## Addendum 2026-07-20 — v0.2.0 shipped; the positioning blocker is closed; revised launch story

Everything below supersedes the stale premises above (append, not rewrite). State as of the
v0.2.0 release cut:

**What changed since 06-28:**

1. **The v2 multi-seed operator shipped and matured.** `tjs_open` (ADR-0012) closed the
   single-source gap (recall@10 0.980 vs the 0.223 that motivated the warning above), was
   re-homed on stock PG (ADR-0019), gained the TR-1-bounded graph leg (ADR-0020), and now
   defaults to PPR-graded scoring after winning all 18 matched points on the 200k held-out
   link-prediction gate (ADR-0021). The "do not launch as an open retriever" restriction is
   lifted — with the seedless-tail caveat below.
2. **The platform story inverted (D2 un-fork).** The launch artifact is three extensions on
   stock PostgreSQL 16/17 + pgvector; the fusion win DOUBLED off the fork (23.68x, Gate B).
   The Spark is a supporting story, not the lead. Public images:
   `ghcr.io/consultingfuture4200/tridb/postgres-trimodal:pg16|pg17`.
3. **v0.2.0 is released** (2026-07-20): public repo, GHCR images anonymously pullable,
   GitHub Release with evidence-cited notes; backup/restore, upgrade paths, single-writer
   enforcement all landed and gated.
4. **A wedge this plan never considered: the MCP agent-memory server** (`make mcp-demo`,
   `docs/mcp_agent_memory_v0.1.0.md`). Product-led entry for any MCP-capable agent.
5. **The all-Postgres baseline was run** (`docs/benchmark_allpg_baseline_v0.1.0.md`) — the
   post-unfork hostile question answered with data, and the answer reshapes the pitch:
   - Anchored class: fused 0.049 ms vs plain-SQL-one-Postgres 0.065 ms vs multi-store
     3.34 ms. The enemy is the three-system stack, not Postgres.
   - Seedless class: plain pgvector currently WINS at matched recall with 3-4x better
     tails — filed publicly as issue #30 BEFORE launch, deliberately.

**Revised messaging:**

- One-liner: "Collapse your RAG stack into one Postgres — TriDB makes tri-modal retrieval
  a first-class operator and a native graph store on stock PG 16/17."
- The funnel logic: if a reader's takeaway is "I'll just add a links table to my existing
  Postgres," the thesis still won and they are one CREATE EXTENSION from being a user.
  Never argue against that reader; recruit them.
- The credibility asset is now the published near-tie + self-filed defect, not only the
  24-60x multi-store rows. Lead the writeup with honesty as the differentiator (the
  original plan's instinct, now with sharper teeth).

**Revised launch sequence (channel-split leads):**

| # | Channel | Lead | Gate |
|---|---|---|---|
| 1 | r/LocalLLaMA | MCP agent memory, one docker run, local | READY NOW (post draft below) |
| 2 | Show HN | `docs/launch_writeup_v0.1.0.md` (the honest three-way story) | Maintainer voice-pass + 2-min demo recording |
| 3 | pgvector/Postgres X | "three extensions on stock PG" + Gate B + the near-tie | After 2 |
| 4 | RAG ecosystem | storage layer for GraphRAG; gBrain integration as the follow-up post | After gBrain adapter ships |

**Draft — r/LocalLLaMA post (edit voice before posting):**

> Title: I put my agent's memory in one Postgres — vector + graph + relational in a single
> query plan (open source, runs on a DGX Spark or any box)
>
> Body sketch: the three-database problem for agent memory -> one docker run + claude mcp
> add -> store/connect/recall demo (memory + embedding + graph edge commit atomically) ->
> the honest benchmark table incl. the plain-Postgres near-tie -> "we filed issue #30
> against ourselves before posting" -> repo + release links.

**Draft — Show HN title options:**

> Show HN: TriDB – vector, graph, and relational retrieval in one Postgres query plan
> Show HN: We un-forked our research DB into stock Postgres extensions and the speedup doubled
> Show HN: We benchmarked our DB against plain Postgres and published the tie

**Remaining pre-HN items (owner: maintainer):** voice-pass the writeup; record the 2-min
demo (asciinema of `make mcp-demo` + one fused query EXPLAIN); choose the HN title.

# TriDB — Developer Demo Brief (v0.1.0)

**For:** anyone on the team showing TriDB to an outsider (peer, prospect, hire, HN reader).
**Purpose:** show it off without getting burned. The engineering is ahead of the packaging —
this doc is the packaging. **Read it before you demo. Do not improvise the numbers.**
Date: 2026-07-03.

---

## TL;DR — the 30-second pitch

> "Omni-RAG retrieval needs three things at once — similarity, connections, filtering. Everyone
> else glues together three databases and shuffles data between them on every query. TriDB does
> all three in **one Postgres query plan, on one local box**. It's faster because it never
> materializes the intermediate set, and it **can't get out of sync with itself** — a guarantee a
> bolt-on stack structurally can't make."

Lead with **one system + consistency**. Speed is the hook; consistency is the moat.

---

## The three things to say (in this order)

1. **One system, one transaction.** Vector + graph + relational in a single query plan, one WAL.
   Commit or roll back atomically across all three stores. *A Milvus+Neo4j+Postgres stack cannot
   do this at all.* This is the claim nobody can dispute.
2. **The filter makes it faster, not slower.** Because there's no blocking operator, a selective
   predicate shrinks the working set instead of post-filtering a huge one. On real SIFT-1M,
   latency *drops* as the filter tightens.
3. **Open, Postgres-native, runs on a Spark.** Peer-reviewed lineage (VBASE OSDI'23 / AkasicDB
   SIGMOD'26 / Chimera PVLDB), but open and reproducible on one local DGX Spark.

---

## Numbers you may show — with the caveat that MUST travel with each

**Rule: never say a number without the label next to it. The label is not optional fine print —
it is part of the number.**

| # | Say this | Always attach this label | Never do this |
|---|---|---|---|
| 1 | "Zero cross-store divergence under 200 iterations of randomized churn; atomic across all three stores." | Live on GX10 (real). This is the moat — lead here. | — |
| 2 | "SIFT-1M, recall@10 = 1.000 at every selectivity; latency drops as the filter tightens." | **Real** SIFT-128 data, GX10, exact oracle. | — |
| 3 | "HotpotQA multi-hop: injecting graph bridges lifts joint evidence recall@5 by +15.6 points vs vector-only." | **Real** HotpotQA, embedding-independent graph. | Don't imply the engine executes this in v1 — the +15.6pt is host-side; the engine operator is `tjs_open` at 0.980. |
| 4 | "At 1M vectors the canonical filtered query runs ~13× faster than a correctly-tuned three-system baseline at recall 1.0." | **Synthetic** corpus. Say the word "synthetic" out loud. | **NEVER** present this as the headline. It's a labeled hook, not a proof. |
| 5 | "Open-retrieval operator `tjs_open`: recall@10 0.980 vs 0.967 vector-only." | Real HotpotQA, first-cut operator. | Don't call it finished — it's first-cut; the 0.987 refinement is host-only. |

### The one trap that will end a demo

**The TJS result is a curve, not a point.** `term_cond` trades recall for effort:
58.5% recall @ 3.6% examined → 100% recall @ 20.1% examined.
**Never quote the fast latency and the 100% recall in the same breath** — they're at different
operating points. Doing so is benchmark laundering and a sharp observer will catch it instantly.
If asked "what's the recall?", answer with the curve, then name the operating point you're quoting.

---

## Two facts you must volunteer before you're asked

Saying these first is what makes the rest credible. Hiding them is what gets you destroyed.

- **"The 1M flagship is on a synthetic corpus."** Our real-data proofs are separate (SIFT-1M,
  HotpotQA). We're honest about which is which.
- **"The headline benchmark is GX10-only."** On an x86 box you can build and run the engine and a
  smaller live benchmark, but you cannot reproduce the 13× — that needs the Spark. Don't promise a
  reproduction you can't deliver on the listener's laptop.

---

## Live demo script (what to actually run)

Works on the x86 standin — build the image once, then:

```bash
scripts/x86build.sh --docker        # build the fork image (one time)
make test-all                       # test + lint + smoke + engine suites — show it's green
make bench-live                     # live SM-1/SM-3/SM-4/SM-5 on the REAL engine
```

The money shot for a technical audience — the canonical query's plan:
```bash
# In psql against the engine: EXPLAIN the canonical query and point at the plan shape —
# Limit -> NestLoop(... IndexScan hnsw ...) with the ANN scan emitting a handful of the
# corpus (early termination, TR-1). "It stops as soon as the top-k is settled."
```

For the consistency story (the moat), run the atomicity + crash-recovery scripts and narrate
the zero-divergence result. That's the part no competitor can reproduce.

**If you have the Spark:** run the real SIFT-1M filtered bench and show latency *dropping* as the
filter tightens. That single behavior refutes "why not pgvector + a SQL filter?" better than any
slide.

---

## Questions you'll get, and the honest answer

| They ask | You say |
|---|---|
| "Isn't this just AkasicDB?" | "AkasicDB is the design we descend from. We're the open, Postgres-native, locally-runnable realization — reproducible from one repo, runs on one Spark." |
| "Synthetic corpus — did you write both sides?" | "The 13× is synthetic, yes. Our real-data numbers are separate: SIFT-1M recall 1.0 and HotpotQA +15.6pt, both on recognized public data with committed repro." |
| "Speed, but is the answer right?" | "Recall@k, reported as a curve. On SIFT-1M it's 1.0. On multi-hop, graph injection adds +15.6pt of evidence recall. We publish the whole curve, not the peak." |
| "Why not Milvus + Neo4j + pgvector?" | "Three problems: you shuffle intermediate sets across process boundaries every query, you can't get a single transaction across all three, and they can drift out of sync. We fix all three by being one system." |
| "Does the graph just re-encode the vectors?" | "No — the graph is real title-mention topology; rebuild embeddings with any encoder and the edges don't move. And naive graph-rerank doesn't help; only injecting the real low-similarity bridges does." |

---

## Not-ready-for-cold-launch checklist (context for the demoer)

These are open — know them so you don't over-promise:
- [ ] 2-minute recorded Spark demo (doesn't exist yet — the single best show-off asset).
- [ ] CI badge confirmed green on `master` (verify before pointing at it).
- [ ] Public-dataset headline with one-command repro against a tuned baseline (the launch unlock).

Until those land, this is a **guided demo**, not a public launch. Show it to people you can talk
to; don't post the repo link cold expecting it to defend itself.

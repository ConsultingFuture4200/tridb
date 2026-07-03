# TriDB — Persona Review + Database-Landscape Assessment v0.1.0

> **Date:** 2026-07-03 · **Against:** master `e0e74b3`
> **Method:** 8-agent workflow — three persona code/architecture reviews (Fabio, Linus, Liotta)
> over the repo + four research sweeps (academic literature, GitHub/OSS landscape, practitioner
> sentiment on HN/Reddit/X, benchmark-credibility standards) → adversarial synthesis, with the
> synthesis agent re-verifying load-bearing claims against the repo before inclusion.
> Full per-agent findings with sources: `docs/landscape_review_appendix_v0.1.0.md`.
> A parallel 7-category deep code audit ran the same day; its findings are tracked via
> `advisor-plans/` (see the session's plan selection).

# TriDB — Synthesis: Reviews + Landscape (2026-07-03)

Verified against the repo before inclusion: ADR-0013 explicitly confirms "nothing on the shipped path uses v1" at commit 408e852; `src/planner/join_order.c` hardwires `avg_out_degree = 0.0` at three sites; the 4.7ms/88ms numbers are in `docs/benchmark_sm2_1m_v0.2.0.md`; the patch chain is 18 files in `scripts/patches/`.

---

## 1. Findings that matter most (deduplicated)

**F1. Every published headline was measured on the v0 heap graph store — the architecture golden rule 3 rejects.** (Liotta critical; confirmed in `docs/decisions/0013-graph-store-v1-rewire.md`.) SM-2 15.1x, the 4.7ms 1M flagship, filtered-SIFT, GraphRAG +15.6pt all probed the SPI/heap extension, not the native AM. One informed reader of ADR-0013 dismantles the launch narrative. **Next action:** land ADR-0013 Stages A+B, re-run SM-2-1M + filtered-SIFT on the v1 AM, publish the delta honestly. Freeze all external publication of numbers until done. This is the cheapest high-leverage item on the board.

**F2. SQL-reachable backend crashes and leaks in the operators — one hardening batch.** Three reviewers converge: (a) `tjs(k=0)` calls `top()`/`pop()` on an empty `priority_queue` — UB/SIGSEGV in both `tjs` and `tjs_open` (only filter-first guards `k > 0`); negative k via `PG_GETARG_UINT32` makes the "bounded" heap unbounded; (b) PQ eviction never `heap_freetuple`s the discarded tuple (~6KB each at dim-768) in both bodies — the "O(batch+k) memory" claim in the patch is currently false; (c) malloc'd `TJSState` + new'd containers leak permanently on any `ereport` after init (longjmp skips destructors); (d) lowering regression: selective filter + `src.id = -1` now ERRORs because Stage-2 picks filter_first which rejects negative src; (e) unchecked 100KB `snprintf` can silently truncate SQL into a *wrong answer*. **Next action:** one focused patch-regeneration pass fixing all five + adversarial-arg regressions in ENGINE_TESTS. All are one-screen fixes; do them together to pay the patch-chain regeneration tax once.

**F3. The PG 13.4 fork is the single biggest strategic liability — and PostgreSQL 19 just changed the game.** Convergent from three independent sweeps: TriDB is the *only fork* in the entire surveyed landscape (pgvector, VectorChord, pgvectorscale, ParadeDB, AGE, OneSparse all ship as extensions); PG 13 is past EOL; practitioner sentiment punishes anything not runnable on managed Postgres ("why a fork of EOL Postgres?" is the predicted top hostile launch comment); and PG 19 beta ships SQL/PGQ natively but lowers `GRAPH_TABLE` to relational joins with no var-length paths. That converts TriDB's best end-state into "the native executor for the GRAPH_TABLE Postgres now standardizes." **Next action:** run Liotta's two-week spike — port execTJS + the graph AM onto stock PG 17+ with pgvector iterative scans, measure the recall/latency gap vs the MSVBASE relaxed iterator. Write the ADR-grade public "why a fork" answer regardless of outcome.

**F4. FR-6's cost model prices a body that no longer exists, and the literature says the failure concentrates exactly where you haven't benchmarked.** The frozen 10% threshold was rationalized for a probe-per-seed filter-first; the landed DEV-1290 body is brute-force exact L2 over the drained set, so the crossover moves with d, k, N, term_cond. `avg_out_degree` is hardwired to 0.0, so a selective filter + mega-hub src picks filter_first and drains an unbounded reachable set. Literature (arXiv 2606.16341) shows plan regret concentrates ~290x at the phase boundary; ACORN/NaviX identify a missing third strategy (in-filter traversal) at mid selectivity. **Next action:** replace the scalar threshold with the two-constant cost comparison calibrated from existing sweep data; feed graph-leg cardinality in (metapage counts exist); benchmark the sweep *across* the boundary. The adaptive mid-query switch and third body are v2, not now.

**F5. The one-WAL thesis does not currently hold on the vector leg.** The HNSW index is a per-backend private RAM cache rebuilt O(heap) from the relational store: ~3GB/backend at 1M×768, cold-start scaling with corpus size, and the root of the ADR-0014 staleness bug family. DEV-1259 Phase B (WAL-backed, shared-buffer-resident pages) retires four liabilities at once — but see Contradiction C1 before committing the quarter to it on the 13.4 fork. **Next action:** do the hnswlib-layout-to-pages spike, but sequence the platform spike (F3) first; if the fork is transitional, the shmem-fallback (once-per-postmaster rebuild) buys 80% for 20%.

**F6. Graph store has a durability time bomb and an ingest wall — fine for benchmarks, fatal for anything long-lived.** Anti-wraparound autovacuum ignores `autovacuum_enabled=false` and will chew native pages as heap; raw stored xids have no freeze path (clog truncation, 2^31 flip); `gph_locate_vertex` is O(V) chain-walk making bulk ingest O(E·V); the metapage counter serializes writers; `gstore` is PUBLIC-readable and a stray `SELECT`/`VACUUM` corrupts or crashes. **Next action this cycle:** REVOKE ALL on gstore, document the vacuum hazard in SECURITY.md, write (not implement) the freeze/vacuum design note. **Before any 100M claim:** O(1) arithmetic vid addressing (dense vids, fixed 32-byte records — the single highest-leverage line of C in the store).

**F7. Benchmark credibility gaps are enumerable and mostly cheap.** From the trust-hierarchy sweep + HN-shredding taxonomy: median-only single-client latency (no p95/p99/QPS — the standard VectorDBBench critique applies verbatim), 1M×128-d vs the field's 50M×768-d norm, GX10-only repro (breaks the ClickBench 20-minute property), baselines pre-dating Qdrant-ACORN and Neo4j v2026.02 in-index filtering, no write-path numbers despite the one-WAL thesis, no Milvus-HNSW row. Internal discipline (exact oracle, recall-as-curve, TUNING.md "beat it") is already above vendor norm — externalize it. **Next action:** add p95/p99 + multi-client to the harness; one full SM-2 run on rentable cloud ARM64/x86; pin baseline versions and add the Milvus-HNSW row; include at least one metric where the baseline wins.

**F8. The 18-patch stacked chain is the dominant maintenance tax and will not survive growth.** ~150KB of engine C exists only as order-sensitive diffs against a gitignored vendor tree; any edit forces downstream regeneration; CI verifies patches *apply* but compiles the engine only on manual dispatch, so an applies-but-doesn't-compile patch merges green. **Next action:** migrate to a TriDB-owned MSVBASE fork branch (submodule/pinned clone), keep verify_patches during transition; add a nightly cached engine-compile CI job. Time this *after* the F2 hardening batch and informed by the F3 spike outcome.

---

## 2. What the world knows that TriDB doesn't yet exploit (by leverage)

1. **PG19 native SQL/PGQ lowers to joins** — TriDB can reposition as "the native executor for standard GRAPH_TABLE" instead of "a fork with its own parser." Align the PGQ dialect with the Eisentraut/Bapat grammar now.
2. **NaviX (VLDB'25) benchmarks VBASE by name and criticizes exactly what TriDB fixed** (post-filter collapse at low selectivity, no recall knob → FR-6 + term_cond). This is a free "we fixed the lineage's known weaknesses" narrative and the evaluation template (selectivity ladder × predicate correlation × recall/QPS curves) reviewers will demand.
3. **The cross-modal benchmark doesn't exist** — UniBench/M2Bench lack a vector leg; GraphRAG evals lack the DB-operator framing. TriDB can define "TriBench" (spec + generator + exact oracle + LDBC-style disclosure + regimes where TriDB loses by design). Category-defining if the F1/F7 hygiene lands first.
4. **Kuzu is dead (Apple acqui-hire, Oct 2025) and MSVBASE upstream is dead** — the embedded graph+vector audience and the VBASE/OSDI lineage are both unclaimed. Graphiti (28k stars) + Cognee (27k stars) force users to run multi-DB stacks and lost their embedded backend; a TriDB backend adapter is the highest-leverage post-benchmark credibility artifact.
5. **Rank-join theory (HRJN, Ilyas 2008; Tziavelis SIGMOD'20)** — relaxed monotonicity gives exactly the structure to upgrade term_cond from an empirical knob to a certified bound ("no better result within epsilon"). Turns SM-4's recall curve into a guarantee-vs-latency curve; the most publishable open item and the structural answer to the DEV-1169 defect class.
6. **BM25: buy, don't build** — ParadeDB pg_search / VectorChord-bm25 are mature; document interop as the fourth-modality answer and never open the seam.
7. **PhaseGraph (2603.28886): PPR and dense-similarity scores are distributionally incomparable without calibration** — the academic articulation of why the polyglot fusion baseline is ill-posed, not just slow. Cite it in the spec/GTM.
8. **Packaging table stakes:** MCP server + GraphRAG SDK (trivial over PG wire protocol), suite docker image, living benchmark page with pinned dates/configs.
9. **GTM reframe from practitioner sentiment:** vector↔relational sync pain is abundantly attested; three-way sync is not (nobody runs all three — they drop the graph leg first). Pitch "graph traversal without adding the system you've been avoiding," and pair it with an ingestion story (star-graph recipes) because GraphRAG's attested bottleneck is graph *construction* cost, which TriDB's runtime doesn't touch.

## 3. What TriDB actually has that nobody else has (skeptical cut)

Survived the survey of 20+ systems:

- **One WAL / one transaction manager / one MVCC snapshot across vector+graph+relational on a proven engine.** No one in the cohort has it: HelixDB/SurrealDB are new engines; Neo4j/TigerGraph/NaviX have no relational SQL modality; Qdrant/Weaviate ACORN is filtered-vector only; AGE is join-lowering. *Asterisk: the vector leg is a per-backend derived cache today (F5) — the claim is architecturally true and operationally incomplete until DEV-1259 Phase B.*
- **A fused cross-store Open/Next/Close operator (tjs) with early termination spanning all three modalities.** Nothing surveyed fuses similarity+traversal+filter in one pipelined operator. *Asterisk: the filter-first body drains fully at Open (Linus) — the claim needs the honest scoping.*
- **Native adjacency-list traversal inside Postgres.** Confirmed genuinely empty niche in extension-land (AGE/OneSparse/pgRouting are all join-based or algebraic). *Asterisk: unproven at headline level until F1 lands.*
- **Standard SQL:2023 SQL/PGQ surface, no new query language** — externally validated (HelixQL was the named adoption blocker on the closest comp launch), and now standardized by PG19.

Not defensible: "tri-modal in one engine" as a category (crowded: CHASE, ARCADE, M2, SurrealDB, HelixDB), filtered vector search per se (commoditizing into pgvector), and golden rule 3 as absolutely stated (GRainDB matched a native GDBMS with RID-joins; rescope to high-fanout k-hop under early termination in an ADR).

## 4. Contradictions worth operator judgment

**C1. Quarter centerpiece: ADR-0009 Phase B on the 13.4 fork vs the PG17/19 platform spike.** Liotta calls shared-resident WAL-backed HNSW "THE priority engine work" — but also recommends the PG17 port spike, and the landscape research says the fork itself is the existential liability. Investing the quarter's biggest engine effort into the fork before the spike answers whether the fork survives is a sequencing bet only you can make. Recommendation embedded below: spike first (2 weeks), decide second.

**C2. TR-1's letter vs the flagship number.** CLAUDE.md golden rule 1 says "no blocking operators — a blocking operator forfeits the entire efficiency thesis." Linus shows the filter-first body — the source of the 4.7ms headline — completes its entire drain at Open and ignores term_cond. The research says exact top-k over a filtered set is inherently like this, and ADR-0011's addendum acknowledges the deviation. Either rescope TR-1 (bounded-memory + FR-6-gated, honestly worded) or the brand and the flagship contradict each other. Same judgment applies to golden rule 3 vs GRainDB and vs the fact that all published numbers ran on the quasi-relational v0 store.

**C3. Fabio's fork-repo migration (high severity, "before the next operator lands") vs F3's possibility that the MSVBASE fork is transitional.** If the PG17 spike succeeds, migrating 18 patches into a fork repo is effort spent on a platform being exited. Cheap resolution: do the F2 hardening batch in the patch chain as-is, delay the repo migration 2 weeks until the spike reports.

**C4. Adaptive/in-filter traversal enthusiasm (Liotta, ACORN/NaviX literature) vs the SIGMOD'26 in-DBMS study** showing per-node predicate checks inside a real engine carry prohibitive system-level overheads. If/when the third body is built: predicate bitmaps, not qual re-evaluation. Not a now-decision.

**C5. Baseline framing.** The GTM's three-way-sync pain pitch is contradicted by practitioner evidence that nearly nobody runs Milvus+Neo4j+Postgres; meanwhile the benchmark sweep says the baselines that *will* be demanded are Qdrant-ACORN, Neo4j v2026.02, and AGE+pgvector-in-one-Postgres. The AGE foil is the strongest possible demonstration of the actual thesis (native AM vs join-lowering, everything else constant).

## 5. Priority order, next 2-4 weeks

**Week 1 (parallel, both small):**
1. **F2 operator hardening batch** — k/arg validation in all three bodies, PQ eviction `heap_freetuple` (both sites), error-path cleanup for malloc'd state, src<0 lowering clamp, snprintf truncation checks; adversarial regressions into ENGINE_TESTS. One patch-regeneration pass.
2. **F1: ADR-0013 Stages A+B rewire** → re-run SM-2-1M + filtered-SIFT on the v1 native AM on the Spark/GX10. Publication freeze until this lands.
3. Cheap safety riders: REVOKE on `gstore` + operator EXECUTE grants; SECURITY.md vacuum-hazard note.

**Week 2:**
4. **F3: start the PG17 + pgvector-iterative port spike** (2 weeks, gates C1/C3 decisions).
5. **F7 benchmark hardening:** p95/p99 + multi-client in the harness; one cloud-rentable SM-2 run; pin baseline versions; Milvus-HNSW row.
6. Draft the public "why a fork" ADR (needed regardless of spike outcome).

**Weeks 3-4:**
7. **F4: FR-6 two-constant cost model** + graph-leg cardinality input + boundary sweep benchmark.
8. **Spike decision point:** commit ADR-0009 Phase B on the fork, or pivot platform — then schedule the F8 fork-repo migration accordingly; add the nightly engine-compile CI job either way.
9. Rescope golden rules 1 and 3 in a short ADR (C2) before any external write-up.

**Deferred but named:** graph freeze/vacuum design note (write before 100M-edge work), O(1) vid addressing (before any 100M claim), certified term_cond bound (publishable, post-rewire), Graphiti/Cognee adapter + TriBench (post-credibility), MCP server/SDK packaging (pre-launch).
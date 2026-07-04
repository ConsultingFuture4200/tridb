# TriDB — Hardening to serve as the gBrain memory backend (v0.1.0)

**Date:** 2026-07-04. **Context:** AgentBOX (`UMB-Advisors/AgentBOX`) now runs on the **DGX Spark
(GX10)** — the exact hardware TriDB is validated on. This retires the Jetson-era footprint/build
objections and makes TriDB a realistic backend for **gBrain**, AgentBOX's long-term memory layer.
This spec is the grounded gap analysis + a **purely additive** hardening program: every item
preserves the golden rules and the frozen cores, and regresses none of the shipped features
(TR-1, native graph AM, one-WAL/FR-7, the filter-first/vector-first `tjs()` bodies, the 1M
filter-first 13.4× / recall-1.0 result, one canonical surface, three stores).

Grounded in two code audits (TriDB `src/graph_store/`, `vendor/MSVBASE`; AgentBOX `gbrain-master/`,
`gbrain-ingest/`). Not yet built — this is the plan.

## Why this is the right fit (not just "a DB that works")

gBrain's own retrieval stack fuses four legs — vector (HNSW cosine) + BM25 + RRF + **knowledge-graph
traversal** — and its benchmark calls the graph "the load-bearing wall (+31 P@5 over vector-only)."
But gBrain stores that graph as a **relational `links` table walked by Postgres recursive CTEs**
(`gbrain-master/.../pglite-schema.ts`) — precisely the "topology as relational joins" pattern TriDB
rejects (golden rule 3). **TriDB's native adjacency-list access method + Open/Next/Close
early-terminating traversal is a direct upgrade to gBrain's most important component**, and gBrain's
hard ≤3s / non-blocking recall budget is exactly TR-1. This is a two-way win: gBrain gets a better
graph engine and one-WAL cross-store consistency; TriDB gets the **real, recognized workload** its
GTM plan (`docs/gtm_opensource_v0.1.0.md`, R3) says it lacks.

## The integration seam

gBrain has a clean storage abstraction: `interface BrainEngine` (`gbrain-master/gbrain-master/src/core/engine.ts:640`),
with two concrete impls today (`PgliteEngine`, a server-Postgres engine) selected by
`engine-factory.ts`. **A TriDB backend = a third `BrainEngine`**, materializing the DDL in
`pglite-schema.ts` / `schema-embedded.ts` and routing the graph methods to the native AM. The
external `MemoryProvider` (Hermes plugin, HTTP to `gbrain serve`) is *not* the seam — `BrainEngine`
is. Fusion (RRF) and cross-encoder rerank stay app-side, so **no new query language is needed** and
the "BM25 seam closed for v1" rule holds — gBrain keeps BM25 on stock Postgres `tsvector` (which
TriDB inherits) and fuses in the app.

## Capability gap matrix (grounded)

| # | gBrain requirement | TriDB today | Verdict | Additive close (no frozen-core edit) |
|---|---|---|---|---|
| G1 | Survive months of continuous writes | No xid-freeze path; forced anti-wraparound autovacuum walks graph pages as heap → corruption (`docs/graph_store_freeze_design_v0.1.0.md:12-28`) | **BLOCKER** | Build the designed `gph_freeze(horizon)` (uses reserved `gm_frozen_horizon`; GenericXLog page rewrite; read path untouched) + disarm forced autovacuum on `gstore` |
| G2 | Delete / re-attribution (`removeLink`, dream consolidation) | No graph mutator; but `GPH_FLAG_DELETED` is **honored on read** (`graph_am.c:647`), nothing sets it | **MISSING** | `gph_tombstone_edge/vertex` setting the flag under GenericXLog — clean additive mutator on an existing seam |
| G3 | Many typed, directional edges (`founded`, `mentions`, `attended`, inverses) | Format has `es_edge_type_id` (`gph_page.h:91`) but insert (`graph_am.c:413`) + traversal (`:649`) hardcode one type; out-only | **PARTIAL** | `type_id` arg on insert + `type_filter`/`direction`(in/out/both) on `gs_open`/`gs_getnext`, writing/reading the **existing** field — 32-byte slot untouched. Type-name dictionary table |
| G4 | Source/tenant isolation on traversal (`source_id`; a past unscoped walk was a P0 leak) | Graph ACLs exist (advisor 026); traversal not source-scoped | **PARTIAL** | `source_id` filter param on traversal (additive) |
| G5 | Edge properties (`link_source`, `context`, `origin_page_id`) | none (slot full; property co-location deferred, `gph_page.h:10-11`) | **MISSING** | Relational **side-table** keyed on native `(src_vid,dst_vid,type_id)` — stores properties, NOT topology → golden rule 3 preserved |
| G6 | Cosine metric (non-negotiable; all HNSW `vector_cosine_ops`) | Engine has IP (`<*>`, `inner_product` opclass) but canonical surface + `tjs()` bodies hardwired L2 | **PARTIAL** | (a) **normalize-at-write** → cosine rank ≡ L2 rank on unit vectors (code-free, ship now); (b) later: widen scope-guard to admit `<*>` + a **new** IP `tjs()` body variant (mirrors how filter-first was added — bodies stay frozen) + PERF-01 ARM IP kernel |
| G7 | Configurable vector dim (768–1280) + extra 1024-dim image/multimodal columns per row | pgvector-style; needs verification of multi-index/multi-dim per table | **VERIFY** | Confirm; expose dim at schema-init; multiple HNSW indexes per table |
| G8 | Match Postgres write concurrency (unlock the daemon) | Logical single-writer; all graph writes serialize on the metapage lock (`graph_am.c:417-426`) | **PARTIAL** | Move `gm_edge_count` → per-vertex `vr_out_degree` (Plan 006, reserved field) so inserts to different vertices stop contending — additive |
| G9 | HNSW durable under long-lived churn | Committed inserts survive via heap-rebuild-on-recovery; **but ~25–50 aborted inserts crash a long-lived backend** (`docs/hnsw_wal_durability_bug_analysis_v0.1.0.md:28`) | **RISK** | ADR-0009 WAL-logged HNSW (DEV-1259); interim: keep writes committing, avoid churny rollbacks |
| G10 | The `BrainEngine` surface (~120 methods) | n/a | **NEW** | A `TriDBEngine` impl: pages/chunks/facts/takes on stock Postgres; `links`/`traverseGraph`/`traversePaths`/`getBacklinks` on the native AM; `searchVector` on HNSW cosine; `searchKeyword` on tsvector |

Schema flexibility (G-misc) is already **SUPPORTED**: the relational leg is plain Postgres, so
pages/chunks/facts/takes/tags/aliases/versions are ordinary tables; only the *canonical query
template* hardcodes a shape, and gBrain bypasses it by calling `tjs()`/`neighbors()`/plain SQL.

## Phased program (all additive)

**Phase A — Long-lived-store correctness (prerequisites; a memory box runs for months).**
- **A1 · `gph_freeze` + autovacuum disarm (G1)** — designed, unbuilt. The gate: without it a
  months-old gBrain corrupts. GX10-gated build; design is done.
- **A2 · HNSW abort-durability (G9)** — land ADR-0009/DEV-1259, or ship with the interim
  "commit-don't-churn" contract documented for the gBrain ingest writer.

**Phase B — Data-model parity (additive, mostly x86-authorable, GX10-built).**
- **B1 · Graph tombstone delete (G2)** — `gph_tombstone_edge/vertex`; read path already filters.
- **B2 · Typed + directional + source-scoped traversal (G3, G4)** — type/direction/source params on
  insert + traversal, using the existing slot field.
- **B3 · Edge-property side-table (G5)** — relational, keyed on native `(src,dst,type)`.
- **B4 · Cosine (G6)** — normalize-at-write now; IP-body variant + PERF-01 later.
- **B5 · Configurable/multi-vector dims (G7)** — verify + expose.

**Phase C — The gBrain `BrainEngine` adapter (G10).**
- **C1 · `TriDBEngine`** implementing the `BrainEngine` interface as a third backend, materializing
  the DDL, routing graph traversal to the native AM (the value-add: recursive-CTE → native
  Open/Next/Close). Fusion + rerank stay app-side. This is where gBrain actually runs on TriDB.

**Phase D — Performance (the existing roadmap, `docs/perf_research_v0.1.0.md`).**
- Metapage → per-vertex out-degree (G8, PERF-adjacent), **PERF-01** (cosine kernel — already in
  flight, directly serves G6), PERF-02/03 (reuse the id-map as slug↔vid), PERF-12 (WAL-backed HNSW,
  = A2/G9).

Recommended order: **A1 → B1 → B2 → (B4 normalize) → C1 skeleton → A2/B3/B5 → D**. A1 first (nothing
long-lived is safe without it); B1/B2 unblock gBrain's delete + typed-graph semantics; a C1 skeleton
lets gBrain run read+append on TriDB early behind a feature flag; A2 and perf harden throughput.

## Non-regression contract ("without sacrificing features")

Every item above is checked against this before it ships:

- **TR-1** — new traversal stays Open/Next/Close + early-terminating; new operators are *additive
  body variants*, never edits to the frozen filter-first/vector-first bodies.
- **Native graph AM** — topology stays in the native adjacency store; edge/vertex *properties* go to
  relational side-tables (G5), never a topology join table.
- **One WAL / FR-7** — every new mutator (`gph_freeze`, tombstones, typed insert) is GenericXLog,
  atomic with the heap + HNSW legs; the FR-7 zero-divergence proof re-runs on each.
- **One canonical surface / three stores** — gBrain issues plain SQL + `tjs()`/`tjs_open()`; BM25
  stays stock `tsvector` fused app-side; no new query language, BM25-in-operator seam stays closed.
- **Shipped benchmark wins** — the 1M filter-first path (13.4×, recall 1.0) and FR-7 churn results
  run the same code; each phase re-verifies byte-identical `#SM2 RESULT` + the parity oracle.
- **Frozen cores** — `gph_page.h` 32-byte slot/metapage/32KB format; `graph_am.c` mutation/traversal
  core; the `tjs()` bodies; the join-order decision interface. Hardening uses the enumerated additive
  seams (new `gph_*` functions, traversal params, side-tables, new body variants, the id-map layer).

## Open questions for the maintainer

1. **Scale target on the appliance.** gBrain cites personal brains at 17K–186K pages (chunks ≫
   pages). That is comfortably in TriDB's range and *below* where its efficiency thesis dominates —
   so the pitch here is **consistency + native graph traversal quality**, not raw scale. Confirm the
   target brain size so we size HNSW/build.
2. **Cosine approach.** Ship **normalize-at-write** (zero engine change, order-identical) first, and
   treat true-IP (G6b + PERF-01) as a follow-on? Recommended: yes.
3. **Adapter ownership.** The `TriDBEngine` (C1) lives in the AgentBOX repo (TypeScript), not here.
   This repo hardens the engine + exposes the SQL/function surface C1 targets; the adapter is a
   cross-repo deliverable. Confirm who owns C1.
4. **Migration path.** gBrain runs PGlite today. Do we want a live PGlite→TriDB migrator, or
   greenfield a TriDB brain and re-ingest from source (the ingest pipelines are idempotent
   upsert-by-slug, so re-ingest is clean)? Re-ingest is likely simpler and lower-risk.

# TriDB Productization Roadmap — spike → architecture → extension → product

**Status:** Draft v0.1.0 (2026-07-13). Strategic roadmap, not a spec — each phase spawns its own
plan/ADR when it starts. Grounded in the current spike state (this branch, `dustin/dev-1354`).

## TL;DR

Three destinations, climbed as a **ladder** — each gates the next, and you do NOT invest in a rung
until the one below it pays off:

1. **Demonstrated Architecture** (weeks) — a stranger-reproducible benchmark + a published value
   story. Converts "spike" → "architecture with a result" without productizing anything.
2. **Installable Extension** (months) — `CREATE EXTENSION` on *current* Postgres, x86 + ARM, no
   forked binary. The structural spike-killer.
3. **Managed Tri-modal DBMS** (quarters, a team) — hosted, multi-tenant, operable, with design
   partners.

**The pivotal insight:** adopting **pgvector** for the vector leg (Destination 2) kills the two
biggest spike blockers *at once* — the MSVBASE fork dependency **and** the non-deterministic HNSW
(plan 043) — because pgvector's HNSW is mature and deterministic. That single decision de-risks the
whole path. Everything else is downstream of it and of the D1 benchmark.

---

## Why "spike" — the concrete gaps (the critical path)

Each destination consumes a subset of these cross-cutting blockers. Nothing is generic; every row
maps to a real artifact in this repo.

| ID | Blocker | Today | Gates |
|----|---------|-------|-------|
| **B1** | Seedless / vector-first HNSW non-deterministic at 1M (**plan 043**) | ~1/5 fresh builds healthy; the fork's relaxed-monotonicity iterator | seedless queries at scale; any headline that isn't filter-first |
| **B2** | It's a **fork** (MSVBASE / Postgres **13.4**, GX10-only C build) | one custom binary, builds only on the GX10 (ARM64+CUDA) | installability — a forked binary is a research artifact by construction |
| **B3** | Graph **footprint** — per-vertex-page adjacency ≈ 220 GB at 6.9M (**plan 009 csr_lite**, ADR-0013) | 1M fits (~32 GB); 6.9M does not | graph scale > ~1M |
| **B4** | Graph-leg **snapshot isolation** deferred (**DEV-1166 / plan 056**) | vector+relational share one MVCC snapshot (0 torn); graph leg is commit-visible (residual tear) | a "true 0% torn" consistency claim |
| **B5** | No **stranger-reproducible benchmark** (**plan 060** Wikidata) | value proven at 200k; 1M filter-first green (DEV-1290) but not independently reproducible | the entire value claim |
| **B6** | **Build / packaging** — x86+ARM CI, install path | GX10-only C; Docker `:gx10-*` images | installability, adoption |

---

## Destination 1 — Demonstrated Architecture (weeks)

**Goal:** a credible, stranger-reproducible result + a public value writeup. No productization.
This is 80% built already (plan 060 host-half done; `publication_gate` discipline in place).

| Phase | Work | Consumes | Exit criterion |
|-------|------|----------|----------------|
| 1.1 | **Resolve 043 for the benchmark** — do NOT block on the deep iterator fix. Formally scope the demonstrated claim to **filter-first only** (spec addendum; `publication_gate` already enforces examined>0 + HNSW-health, so the honesty is mechanical). File the seedless-at-1M fix as explicitly out-of-scope for D1. | B1 (scope, not fix) | ADR/spec addendum: "D1 = filter-first regime"; gate refuses any seedless headline |
| 1.2 | **Plan 060 GX10 measurement pass** — ingest a pinned Wikidata truthy slice (≥1M BFS closure) via `tools/wikidata_ingest.py`; embed (fastembed/BGE, normalize-at-write); load engine + isolated Milvus+Neo4j+pg; run Harness A `--live` (torn-read delta) + Harness B `report` (matched-recall latency) through the reused gate. | B5 | ≥1M filter-first fusion latency@matched-recall + torn-read delta, both gate-passing, pinned + reproducible |
| 1.3 | **Publish the value story** — writeup of ADR-0017 (fusion speed + one-WAL consistency), the matched-recall methodology, and the honest regime caveats (compute-bound, I/O-locality dead, filter-first). Tag repo **v0.1.0** as the reproducible artifact + dataset/manifest pins. | — | public writeup + tagged release; a stranger reproduces the number from the repo |

**Exit (D1 done):** "TriDB is a *demonstrated tri-modal architecture* with a reproducible ≥1M
result," not "a spike." Achievable solo, GX10-gated, in weeks.

**Kill criterion:** if the fusion win does NOT survive matched-recall at 1M reproducibly → TriDB is a
**consistency play only**; rewrite the positioning around one-WAL cross-modal ACID and do NOT proceed
to D2's fusion-centric un-fork. (This is the honest fork in the road the benchmark exists to decide.)

---

## Destination 2 — Installable Extension (months)

**Goal:** `CREATE EXTENSION` on current Postgres, installable by a stranger on x86 + ARM, no forked
binary. **Do not start until D1's benchmark justifies the investment.**

| Phase | Work | Consumes | Exit criterion |
|-------|------|----------|----------------|
| 2.1 | **Un-fork the graph AM** — port `graph_store_am` to PG **16/17** as a standalone extension (leverage the **ADR-0015 PG17 spike**); strip MSVBASE deps. | B2 | `CREATE EXTENSION graph_store_am` on stock PG 17; `graph-test` green off-GX10 |
| 2.2 | **★ Adopt pgvector for the vector leg** — replace the forked `vectordb` HNSW with pgvector (the shim is already proven, image `:gx10-v1-pgv`). Re-home `tjs_open` to co-iterate pgvector's HNSW + the native graph AM. **This kills B2's vector half AND B1 (043) — pgvector's HNSW is deterministic.** | B1, B2 | `tjs_open` runs filter-first over pgvector + graph AM on stock PG; 043 moot |
| 2.3 | **CSR footprint redesign** — packed adjacency (csr_lite, ADR-0013 rider) so >1M graphs fit in reasonable RAM. | B3 | 6.9M-scale graph loads within a commodity footprint; adjacency byte-parity vs today at 1M |
| 2.4 | **Build + packaging + CI** — x86 + ARM builds off the GX10; Docker image (`postgres + extensions`), PGXN/apt path, install docs, a test suite on stock PG 16/17. | B6 | `docker run` or apt/PGXN install works on x86 + ARM; CI matrix green on stock PG |
| 2.5 | **Portable `tjs_open` + TR-1 re-verify** — confirm the fused early-terminating operator honors Open/Next/Close + early termination on modern PG over the un-forked stores. | — | TR-1 suite green; a stranger runs a tri-modal fused query on a laptop |

**Exit (D2 done):** a stranger runs `CREATE EXTENSION` on their own Postgres (x86 or ARM), loads a
tri-modal corpus, and runs a fused filter-first query — no GX10, no fork.

**Gate B (mid-phase, after 2.2):** does `tjs_open`-over-pgvector preserve the fusion win from D1? If
the forked `vectordb` was essential to the measured advantage, the un-fork is harder and needs a
re-decision. Measure before finishing 2.3+.

---

## Destination 3 — Managed Tri-modal DBMS (quarters, a team)

**Goal:** a hosted, multi-tenant, operable product with paying design partners. This is a **company**,
not a solo project — sketched, not fully specced. **Do not start without validated demand.**

| Phase | Work | Consumes | Exit criterion |
|-------|------|----------|----------------|
| 3.1 | **Hardening** — productionize the security work (advisor **026** graph ACLs, **044** auth); land **DEV-1166** graph-leg snapshot isolation for a true 0-torn claim; validate crash/durability (**ADR-0009**) at scale. | B4 | SI test suite green (0 torn incl. graph leg); security review clean; crash-recovery validated ≥1M |
| 3.2 | **Multi-tenancy + control plane** — provisioning, per-tenant graph scoping (extend ADR-0016 source-scope to tenant-scope), resource limits, isolation. | — | N tenants isolated; per-tenant scoping enforced natively + at the surface |
| 3.3 | **Ops** — backup/restore, monitoring, HA/replication (the one-WAL design is a genuine advantage for cross-modal consistent replicas), rolling upgrades. | — | backup/restore + a replica demonstrated; upgrade path documented |
| 3.4 | **GTM** — pricing, SLAs, the reference use cases (**GraphRAG / agent memory** — the proven fits), design partners. | D1 story | ≥1 paying design partner on a real workload under an SLA |

**Exit (D3 done):** a paying design partner running a real tri-modal workload with an SLA.

**Gate C (before starting D3):** is there real demand — signed design partners or a concrete internal
workload? Do not build a managed service on spec.

---

## Critical path & sequencing

```
D1 (weeks)                 D2 (months)                         D3 (quarters)
─────────────────────────  ──────────────────────────────────  ────────────────────────
1.1 scope 043 ──┐          2.1 un-fork graph AM ──┐
1.2 GX10 bench ─┼─▶ GATE A 2.2 ★ pgvector leg ─────┼─▶ GATE B   3.1 hardening (B4/DEV-1166)
1.3 publish ────┘  (win?)  2.3 CSR footprint       │  (win      3.2 multi-tenancy
                           2.4 build/CI/package ───┤   holds?)  3.3 ops (backup/HA)
                           2.5 portable tjs_open ──┘            3.4 GTM + design partners
                                                        └─▶ GATE C (demand?) ─▶ D3
```

- **Gate A** (after D1): does the reproducible fusion win justify the un-fork? If consistency-only →
  reposition, narrow D2.
- **Gate B** (mid-D2, after 2.2): does the fusion win survive on pgvector + stock PG?
- **Gate C** (before D3): validated demand?

**The one decision that unblocks the most: Phase 2.2 (pgvector).** It simultaneously retires the fork
(B2 vector half) and 043 (B1). If it works, the seedless-HNSW rabbit hole (the deepest, most uncertain
fix) never has to be dug.

---

## Risks & kill criteria

**Risks**
- Fusion win doesn't reproduce at 1M under matched recall → consistency-only pivot (kills D2's premise).
- `tjs_open`-over-pgvector loses the advantage the forked `vectordb` gave → un-fork is costlier.
- CSR redesign (B3) is a real access-method rewrite — the hardest pure-engineering item in D2.
- Graph-leg SI (B4/DEV-1166) is a known-hard concurrency change deferred once already.

## Resolved decisions (v0.1.0 recommendations, folded in 2026-07-13)

These are the standing recommendations — treat as the plan's direction unless a gate overturns them.

**D1. Plan 043 → SCOPE OUT, don't fix; retire via pgvector.**
Fixing the fork's relaxed-monotonicity HNSW iterator is deep, uncertain engine-C work on a component
D2 will delete — wasted effort. Instead: scope the D1 claim to **filter-first** (green at 1M,
DEV-1290; `publication_gate` enforces the honesty), and let pgvector's deterministic HNSW make 043
**moot** in D2 (the seedless path returns for free). Do NOT touch the fork iterator.
- **Hedge (Gate B):** validate that pgvector's scan can feed `tjs_open`'s early termination (the VBASE
  relaxed-monotonicity mechanism the fusion win rides on). pgvector 0.8 shipped iterative index scans
  and PR #524 adds a relaxed-monotonicity mode — it's converging on exactly this. If insufficient, a
  thin relaxed-order shim over pgvector's index is the fallback (never resurrect the fork).

**D2. Vector leg → pgvector, decisively.** Keeping the forked `vectordb` (SPTAG) *is* keeping the
fork — the single biggest launch liability per the landscape review (unavailable on managed PG; PG-13
near-EOL). pgvector is the distribution gold standard, deterministic, and the only way to hold the
"still just Postgres" trust position 2025-26 sentiment rewards.
- **Scale is not a constraint:** pgvector's comfort zone (≤~1M–10M vectors) *is* TriDB's differentiator
  regime (selective, interactive fusion), and the graph footprint (B3) caps you near there anyway.
  TriDB's edge is fusion + native graph, never raw vector scale.
- **Fallback order** if Gate B shows the fusion win doesn't survive over pgvector: (a) pgvector
  iterative-scan / relaxed-order mode → (b) minimal relaxed-order shim over pgvector's index → (c)
  last resort, package `vectordb` as its own extension. Keep `vectordb` as default only if (a) and (b)
  both fail.
- **Licensing (verified clean):** pgvector is **PostgreSQL-licensed** (permissive, OSI-approved, no
  copyleft) — fully compatible with TriDB's **MIT** license (and MSVBASE's MIT lineage). Net
  neutral-to-positive: it *replaces* the SPTAG-derived forked `vectordb` with a cleanly-licensed,
  universally-packaged extension, shrinking the bespoke-derived-code surface and improving the
  "OSI-approved, still just Postgres" hygiene the landscape review calls non-negotiable. Only
  obligation: retain pgvector's notice on distribution (same as MSVBASE's). To-do: confirm the
  `LICENSE` at the pinned `$PGV_TAG` (`scripts/add_pgvector.sh`); a full IP review belongs at D3.

**D3. Resourcing → D1 solo now; D2 is not a solo sprint; D3 is a team.**
- **D1:** solo, ~80% built — the GX10 pass + writeup.
- **D2:** porting the graph AM to PG 16/17, re-homing `tjs_open` onto pgvector, the CSR footprint
  rewrite (a real AM redesign), and x86+ARM packaging = months of specialized PG-internals C. Two
  honest paths: **(preferred)** after Gate A, bring in **one experienced Postgres-extension/internals
  engineer** (contract or hire — a specialist 5×'s a generalist here); **(if solo-only)** de-scope to
  a **v0.2**: ship the graph AM extension + pgvector, **defer the CSR redesign, cap scale at ~1M**
  (which fits the regime), grow later. Do not attempt the full un-fork + CSR + packaging solo.
- **D3:** unambiguously a funded team; don't start without design partners (Gate C).

**Meta — the pivotal de-risk:** the one number that governs the whole path is **whether `tjs_open`
keeps its fusion advantage running over pgvector's HNSW (Gate B)**. Spike that EARLY in D2, before the
CSR/packaging investment.

**Recommended immediate next step:** execute **D1** (mostly built), let its benchmark answer Gate A.
043 and the vector leg are pre-decided (filter-first scope now, pgvector retire in D2), so D2 has a
clear line the moment Gate A greenlights. Everything past D1 is contingent on the number D1 produces.

---

## Addendum A1 (2026-07-14) — Gate A verdict: PASS

D1's measurement pass ran on the GX10/Spark against a pinned 1,002,331-entity Wikidata slice
(full evidence + pins: `docs/wikidata_spike_v0.2.0.md`, artifacts `bench/results/wd_1m_*`):

- **Fusion win, gated:** 0.27 ms vs 3.16 ms at matched recall (0.992 / 0.986) — **11.90×** —
  through `publication_gate` (graph parity, HNSW 3/3, examined>0, boundary parity equalized).
- **Consistency win, live:** 0.029% vs 1.33% torn reads on a real pinned edit window (46×);
  TriDB residual = graph-leg only (DEV-1166, D3 hardening as planned).

**The kill-criterion did NOT trigger** — TriDB is not a consistency-only play; D2's
fusion-centric un-fork is justified. One surface caveat feeds D2 planning: the typed
filter-first headline runs as a fused native-surface statement, because `tjs_open` has no
typed-traversal argument (plan 038 landed typed traversal as AM SRFs only). D2 phase 2.2's
re-home of the operator over pgvector should absorb the typed slot into the operator surface
(and Gate B measures it there).

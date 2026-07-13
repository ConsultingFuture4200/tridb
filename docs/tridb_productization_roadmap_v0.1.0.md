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

## Risks, kill criteria, open decisions

**Risks**
- Fusion win doesn't reproduce at 1M under matched recall → consistency-only pivot (kills D2's premise).
- `tjs_open`-over-pgvector loses the advantage the forked `vectordb` gave → un-fork is costlier.
- CSR redesign (B3) is a real access-method rewrite — the hardest pure-engineering item in D2.
- Graph-leg SI (B4/DEV-1166) is a known-hard concurrency change deferred once already.

**Open decisions (resolve before the phase that needs them)**
1. **043: fix vs scope-out.** Recommend *scope-out* for D1 (filter-first), and *retire via pgvector*
   in D2 — don't fix the fork's iterator unless D2 keeps `vectordb`.
2. **Vector leg: pgvector vs keep `vectordb`.** Recommend pgvector for installability + determinism;
   keep `vectordb` only if Gate B shows it's load-bearing for the win.
3. **Resourcing.** D1 = solo, GX10-gated, weeks. D2 = 1–2 engineers, real C work, months. D3 = a
   funded team, quarters. Don't pretend D2/D3 are solo.

**Recommended immediate next step:** execute **D1** (it's mostly built), let the benchmark answer
Gate A, and pre-decide 043 as filter-first-scope + pgvector-retire so D2 has a clear line. Everything
past D1 is contingent on the number D1 produces.

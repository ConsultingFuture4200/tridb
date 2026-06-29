# Plan 009: Spike sorted-by-dst, contiguous per-vertex adjacency (CSR-lite) for the native graph store

> **Executor instructions**: This is a **design + benchmark spike** for the biggest structural graph
> change in the audit. The deliverable is a design note + a measured prototype on the GX10 engine, NOT
> a merged production layout change. Follow each step; run every verification command; on a "STOP
> condition", stop and report. Update this plan's row in `advisor-plans/README.md` when done.
>
> **Drift check (run first)**:
> `git diff --stat 8b19cb5..HEAD -- src/graph_store/ docs/decisions/0002-adjacency-list-graph-store-layout.md docs/graph_store_v0_limitations.md`
>
> **Hardware gate**: the graph AM C compiles **only inside the MSVBASE fork on the GX10** (PG 13.4,
> `--with-blocksize=32`). Design + the layout analysis happen here; the prototype build + page-read
> benchmark run on the GX10. Do not claim the C builds/passes off-target.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: HIGH (changes the on-disk adjacency layout + the edge-append path; many downstream effects)
- **Depends on**: none to start; **enables** later compression (audit finding #14) and WCOJ, which
  must NOT be attempted before this lands.
- **Category**: tech-debt / perf (graph traversal at scale)
- **Planned at**: commit `8b19cb5`, 2026-06-28
- **Horizon**: v2 (prototype) → v3 (production)

## Why this matters

TriDB's native graph store is the BUILD half of the keystone (ADR-0001), but its adjacency layout is
the simplest thing that works: a vertex's out-edges are **append-only, unsorted** `GphEdgeSlot`s
packed into 32 KB pages, and when a vertex outgrows a page the list is **chained via
`gph_next_pageno`** — a random `ReadBuffer` per overflow hop (`gph_page.h:51`, `graph_am.c:433-465`).
Two structural consequences the 2026-06-28 research audit flagged:

1. The `tjs` graph leg is a **reachability predicate** — "is `dst` in `src`'s neighbor set?" — resolved
   today by building a hash membership set at Open. If adjacency were **sorted by `es_dst_vid`**, the
   predicate becomes a **sorted merge against the ranked vector stream** (no hash build), and
   `tjs_open`'s multi-source frontier union becomes a **linear merge** instead of a union of hash sets.
2. Sorted, **contiguous per-vertex extents** (CSR-lite — Kùzu CIDR'23; LiveGraph's Transactional Edge
   Log PVLDB'20; Sortledton's degree-adaptive containers PVLDB'22) replace random page-chain hops with
   sequential reads, which is what governs traversal cost at the 128 GB scale where buffer-pool
   residency is the bottleneck. It is also the **prerequisite** for adjacency compression (delta+VByte,
   audit finding #14) and any worst-case-optimal join — both of which need sorted neighbor lists.

`docs/graph_store_v0_limitations.md` already flags the unbounded hub-vertex / page-chain problem;
Sortledton's degree-adaptive containers are the cited fix. This plan does the *spike* — prototype the
layout, measure the traversal page-reads and predicate cost against today's chain — so the production
decision is made on GX10 numbers, not intuition.

## Current state

- `src/graph_store/gph_page.h` — page format. `GphEdgeSlot` is a fixed **32 bytes** (static-asserted,
  line 97), appended via `GphPageAppendRecord` (lines 129-138) which only ever **appends at `pd_lower`**
  (no in-page ordering). Adjacency pages chain via `GphPageSpecial.gph_next_pageno` (line 51). MVCC
  visibility is the per-slot `es_xmin` (line 92), checked by `gph_xmin_visible`.
- `src/graph_store/graph_am.c` — `gph_insert_edge` (lines 362-479) appends each edge to the **tail**
  adjacency page of `src` (`vr_adj_tail`), allocating/chaining a new page when full (lines 444-473).
  Traversal is the `gph_neighbors` scan iterator (`graph_am.c:498+`, the `cur_blk`/`cur_slot` walk over
  the chain). There is **no sorting anywhere** in the append or scan path.
- `docs/decisions/0002-adjacency-list-graph-store-layout.md` — the layout ADR this plan would amend.
- `docs/graph_store_v0_limitations.md` — open question (1) is exactly the unbounded-adjacency / layout
  concern this addresses.
- Traversal correctness depends on `es_xmin` visibility — any re-layout must preserve MVCC visibility
  semantics (a re-sort must not lose or reorder the version word relative to its slot).

Conventions to honor:
- Shared WAL only (GenericXLog); no second WAL/rmgr (`gph_page.h:8-14`).
- Graph topology stays a **native access method**, never relational join tables (CLAUDE.md golden rule
  3). A CSR-*lite* contiguous-extent layout is still a native AM — this is not a move to relational CSR
  built on the fly (which the audit explicitly rejected as blocking + against golden rule 3).
- **TR-1**: traversal stays Open/Next/Close. A sorted layout must still emit neighbors one-at-a-time
  (streaming); do **not** introduce a "sort the whole adjacency list at query time" step (that would be
  a blocking operator). Sorting happens at **write/maintenance time**, decode stays streaming.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Python tests / lint (here) | `make test` / `make lint` | pass, exit 0 |
| GX10 engine build | `scripts/gx10build.sh` | builds the fork image |
| Graph engine suite (GX10) | `make graph-test` | all graph `test/*.sql` pass |
| FR-7 atomicity (GX10) | `scripts/txn_atomicity_test.sh` + `scripts/crash_recovery_test.sh` | zero divergence |

## Scope

**In scope** (this is a SPIKE — prototype on a branch, do not merge to the shipped layout):
- `docs/graph_store_csr_lite_v0.1.0.md` (create) — the layout design + the prototype's measured
  results + the production recommendation. This note is the primary deliverable.
- A **prototype** modification to `src/graph_store/gph_page.h` + `src/graph_store/graph_am.c` on a
  spike branch: keep `GphEdgeSlot` sorted by `es_dst_vid` within a per-vertex contiguous extent.
- `test/` — a benchmark SQL script that measures traversal page-reads + reachability-predicate cost for
  sorted-extent vs the current page-chain on the engine suite (GX10).
- `tests/` — host unit tests for any pure layout/encoding logic that can be exercised without the
  engine (e.g. an extent-packing calculator).

**Out of scope** (do NOT touch in this plan):
- Adjacency **compression** (delta+VByte, audit #14) — strictly downstream; needs this to land first.
- Worst-case-optimal / leapfrog joins — depends on sorted adjacency; out of scope here.
- The vector or relational stores; the `tjs` operator body (the predicate-as-sorted-merge rewrite is a
  *consumer* of this layout and a separate plan — here, only prove the layout is faster to traverse).
- Merging the prototype into the shipped layout — that is the *production* follow-on this spike informs,
  gated on its own ADR + the FR-7 atomicity suite passing.

## Steps

### Step 1 (here): Design note — layout, write-amplification, MVCC, and the degree-adaptive container

In `docs/graph_store_csr_lite_v0.1.0.md`, specify:
- **Sorted-by-`es_dst_vid` per-vertex contiguous extents.** How an extent is allocated and grown; how
  inserts keep order (insertion into a sorted run vs. a periodic re-sort — note the write-amplification
  trade: in-place sorted insert shifts slots; a small unsorted "delta" tail merged at maintenance time
  is the LiveGraph/Sortledton approach — recommend one and justify).
- **Degree-adaptive containers (Sortledton):** small vertices inline; large/hub vertices get a
  different container (the v0-limitations open question). Specify the threshold.
- **MVCC preservation:** the `es_xmin` version word must stay attached to its slot across any re-sort;
  spell out exactly how (move the whole 32-byte slot, or split a parallel version array — note the
  audit's caution that compression later will *force* a parallel version array, so consider it now).
- **Shared-WAL impact:** sorted insert touches more of a page than an append — quantify the extra
  GenericXLog full-image cost.

**Verify**: `grep -nE '^#{2,3} ' docs/graph_store_csr_lite_v0.1.0.md` lists sections covering, at
minimum: the sorted-extent layout, the insert strategy + write-amplification, the degree-adaptive
container, MVCC (`es_xmin`) preservation, and the shared-WAL impact. The MVCC + WAL sections must each
state, in prose, how an insert keeps order without losing `es_xmin` visibility and without a second WAL
(a reviewer reads those two sections to confirm — the grep proves they exist).

### Step 2 (GX10 spike branch): Prototype sorted-extent append + streaming sorted scan

On a spike branch, modify the edge-append path so a `src` vertex's edges are kept sorted by
`es_dst_vid` within a contiguous extent (using the Step-1 design's chosen insert strategy), and adjust
`gph_neighbors` to scan the extent in sorted order, **still one slot per `Next()`** (streaming — no
whole-list sort at scan time; TR-1 preserved). Keep `es_xmin` visibility checks intact.

**Verify** (GX10): `make graph-test` passes on the spike branch (traversal correctness unchanged), and
`scripts/txn_atomicity_test.sh` + `scripts/crash_recovery_test.sh` show **zero divergence** (the
re-layout must not break FR-7 atomicity — this is the highest-risk regression).

### Step 3 (GX10 spike branch): Measure traversal page-reads + predicate cost vs the page-chain

Add a benchmark (`test/graph_layout_bench.sql` or a `scripts/` driver) that, on a corpus with a mix of
low- and high-degree vertices, measures for both layouts:
- pages read per k=5 traversal (the sequential-vs-random-hop win),
- the reachability-predicate cost (sorted merge vs hash-set membership) for a `tjs`-shaped query,
- write cost (edges/sec on bulk load) — the write-amplification price.

Record results in the design note as a table.

**Verify** (GX10): the benchmark runs and the note reports the page-read / predicate / write-cost deltas
with the corpus size and method stated (a **curve/table**, not a single number — GTM discipline).

### Step 4 (here): Production recommendation + ADR-0002 amendment proposal

In the design note, state the go/no-go: does the traversal win justify the write-amplification and the
HIGH-risk layout migration? If go, draft the **proposed ADR-0002 amendment** (append an addendum, don't
rewrite) and the staged migration (the production change is a *separate* plan gated on FR-7 passing).
If no-go (e.g. the page-chain is fine at the scales that matter), record that and close the finding so
it isn't re-audited.

## Test plan

- Host (`make test`, here): unit-test any pure packing/ordering helper (e.g. "given N sorted dst ids,
  compute extent page layout") — deterministic, no engine.
- GX10: `make graph-test` (correctness) + the atomicity/crash suites (FR-7) + the Step-3 layout
  benchmark.
- Verification: `make test` + `make lint` green here; the GX10 suites pass on the spike branch and the
  benchmark table is in the design note.

## Done criteria

ALL must hold:
- [ ] `docs/graph_store_csr_lite_v0.1.0.md` exists: layout, insert strategy, degree-adaptive container,
      MVCC + WAL impact, **measured** traversal/predicate/write deltas (GX10), and a go/no-go recommendation.
- [ ] (GX10) the spike branch passes `make graph-test` AND `scripts/txn_atomicity_test.sh` +
      `scripts/crash_recovery_test.sh` with zero divergence (FR-7 not regressed).
- [ ] (GX10) the layout benchmark reports page-reads + predicate cost + write cost for sorted-extent vs
      page-chain, as a table with stated corpus size.
- [ ] `make test` / `make lint` exit 0 here.
- [ ] The shipped graph layout is **unchanged** by this spike: run on your spike branch
      `git diff --stat 8b19cb5..HEAD -- src/graph_store/` → **empty** (the prototype C from Step 2 lives
      in the design note as a marked sketch, not in the shipped tree). The production layout change is a
      separate, later plan gated on the go decision + FR-7 — it is NOT part of closing this spike.
- [ ] `advisor-plans/README.md` status row updated.

## STOP conditions

- The sorted re-layout breaks `es_xmin` MVCC visibility or any FR-7 atomicity/crash test — STOP and
  report; correctness beats layout, always.
- Keeping edges sorted on insert causes unacceptable write amplification on bulk load (the corpus-build
  step that every benchmark depends on) — report the number; it may flip the design to a delta-tail +
  maintenance-merge approach (still in scope to recommend, but flag the change).
- The traversal win does not materialize at the scales runnable on the GX10 — report the negative; a
  no-go here saves a HIGH-risk production migration.
- Any step pulls in compression or WCOJ — STOP; those are explicitly downstream and out of scope.

## Maintenance notes

- This is the **gate** for two later items: adjacency compression (audit #14, needs sorted dst) and
  WCOJ (needs sorted neighbor lists). Neither should start until this lands and the production layout
  decision is made.
- The MVCC-version-word handling chosen here constrains the compression plan (a parallel version array
  vs inline) — record the choice prominently so #14 inherits it.
- A reviewer should scrutinize: FR-7 atomicity (the re-layout is the most likely thing to silently
  break cross-store crash consistency), and that the scan stayed streaming (no query-time full-list
  sort sneaked in — that would be a TR-1 violation).
- The production migration (if go) is a separate, higher-risk plan with its own ADR-0002 addendum and a
  data-migration story for any existing graph stores; this plan only produces the evidence and the
  recommendation.

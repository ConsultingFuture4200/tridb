# ADR-0015: PG17 platform feasibility — what binds TriDB to the 13.4 fork, and what a stock-PG port costs

> **Realized / executed.** Status: Accepted (2026-07-15). This spike's recommendation (Option C — un-fork
> the graph + planner onto stock PG, keep the fork as the vector vehicle) was executed and is superseded
> operationally by [ADR-0019](0019-tjs-open-stock-pg-rehome.md) (the `tjs_open` re-home); Gate B PASS
> (roadmap Addendum A2). The body below is the original append-only evidence — unchanged.

- **Status:** Accepted (2026-07-15) — realized by ADR-0019 + roadmap Addendum A2 (Gate B PASS)
- **Date:** 2026-07-03
- **Inputs:** advisor plan 028 spike artifacts under `spike/pg17/` — `fork_dependency_inventory.md`,
  `compile_{graph_store,planner,ext}.log` (+ `*_noassert` variants), `pgvector_iterative_probe.sql`/`.out`
- **Gates:** fork-repo migration (landscape F8), DEV-1259 Phase-B investment shape (contradiction C1),
  the launch FAQ

## Context

TriDB ships as a fork of MSVBASE's PostgreSQL **13.4** — past EOL. Per
`docs/landscape_review_v0.1.0.md` (F3): TriDB is the *only fork* in the surveyed 2026 landscape
(pgvector, VectorChord, pgvectorscale, ParadeDB, Apache AGE, OneSparse all ship as extensions on
current PG); a fork is unrunnable on managed Postgres; and "why a fork of EOL Postgres?" is the
predicted top hostile launch question. Meanwhile PostgreSQL 19 ships SQL/PGQ `GRAPH_TABLE`
natively but lowers it to relational joins — opening the reframing of TriDB as "the native
executor for the GRAPH_TABLE Postgres just standardized" (F3, C1, C3). Before the quarter's engine
effort is committed to the 13.4 fork (DEV-1259 Phase B) or the 18-patch chain is migrated to a
fork repo (F8), this spike measured what EXACTLY binds TriDB to the fork and what a stock-PG17 +
pgvector port would cost.

## Evidence (measured, spike artifacts)

### E1. What the fork actually provides (Step 1, `fork_dependency_inventory.md`)

The base MSVBASE `Postgres.patch` modifies PG core in one coherent mechanism — relaxed-monotonicity
index scans: `IndexAmRoutine.amcanrelaxedorderbyop` (AM API), `IndexScanDescData.xs_inorder` +
`xs_heaptid_orig` (scan API), `EState.is_index_inorder` (executor channel), a `create_ordered_paths`
planner hunk that forces a bounded Sort above relaxed scans, a `nodeSort.c` early-stop
(`tuplesort_heapfull`), and — critically — **disabling** stock PG's
`"index returned tuples in wrong order"` executor error. Stock PG 17 has none of these; the last
one makes a relaxed-emission AM *illegal* on stock PG through the ORDER BY path. Of the 18 TriDB
patches: 10 are fork-only mechanism (HNSW-AM hardening/durability/build), 6 are operator logic
(TJS family) written against PG-13 APIs plus two fork-added scan fields, 2 are portable as-is
(NEON kernel, vector-index seam header).

### E2. The three PGXS extensions port for free — except the block size (Step 2, compile logs)

Compile probe against stock PG 17.10 headers (Debian, gcc 14.2, 8KB BLCKSZ):

| Extension | Errors | Notes |
|---|---|---|
| `graph_store_ext` | **0** | builds `graph_store.so` clean, unmodified |
| `graph_store` (the native graph AM) | **1** | the single error is our own `StaticAssertDecl(BLCKSZ == 32768)` |
| `planner` | **1** | same single error, inherited via `gph_page.h` |
| both, assert neutralized (spike-only shim) | **0 errors, 0 warnings** | full `.so` builds |

**Measured PG 13→17 API drift for all TriDB PGXS code: zero.** The only bind is the 32KB page
size. On stock 8KB pages the AM compiles and would run, but edge capacity drops from ~1022 to
~254 slots/page — 4x the adjacency-chain page reads for high-degree vertices, which is exactly
the I/O that ADR-0002's 32KB choice avoids. A `--with-blocksize=32` self-built PG 17 restores the
layout but forfeits managed-Postgres compatibility — the main strategic reason to leave the fork
(build not performed here per the plan's STOP condition; cost recorded: a full PG source build +
own packaging/distribution, i.e. a fork-shaped deployment burden without the fork).

### E3. pgvector 0.8.1 iterative scans solve starvation but with a measured recall ceiling (Step 3)

20k×64 corpus, 1% selective predicate (200 of 20,000 rows), k=10, 20 queries, HNSW m=16/
ef_construction=64, vs exact ground truth (`pgvector_iterative_probe.out`):

| Config | recall@10 | avg index tuples scanned | starved queries (of 20) |
|---|---|---|---|
| `iterative_scan = off` (ef_search=40) | 0.040 | 40.0 | 20 |
| `relaxed_order`, `max_scan_tuples=1000` | 0.285 | 1010.6 | 3 |
| `relaxed_order`, `max_scan_tuples=5000` | 0.825 | 1110.3 | 0 |
| `relaxed_order`, `max_scan_tuples=20000` (default; = full table) | 0.965 | 1132.8 | 0 |
| `strict_order`, `max_scan_tuples=20000` | 0.860 | 1164.0 | 0 |

Reading: post-filter starvation (the problem VBASE/relaxed-monotonicity exists to solve) is real
and total at `off` — and pgvector's iterative scan does fix it, converging to 0.965 recall@10
while touching ~1.1k of 20k tuples. It is a genuinely **resumable ordered candidate stream**.

**What it does NOT give a re-hosted `execTJS` (concrete API gaps vs the fork iterator):**

1. **No per-candidate distance exposure.** The fork's TJS reads `xs_orderbyvals[0]` per tuple as
   the sole rank authority. pgvector's scan returns TIDs in (approximate) order with
   `xs_recheckorderby = false` and does not populate `xs_orderbyvals` — a ported operator must
   recompute the distance from the heap tuple per candidate (duplicate distance work), or carry a
   small pgvector patch (extension-level, S — not a core-PG patch).
2. **No `xs_heaptid_orig`.** Fork-added field. Portable workaround: the operator owns the scan
   loop (`index_getnext_tid` + explicit `index_fetch_heap`) and copies the TID itself — S, but it
   is operator-code restructuring, not a drop-in.
3. **Termination control is a GUC budget, not an operator knob.** `hnsw.max_scan_tuples` /
   `hnsw.scan_mem_multiplier` are per-session GUCs; when the budget exhausts, the stream ENDS
   (renewed starvation), and recall is capped by the budget (measured: 0.825 at 5k vs 0.965 at
   20k). The fork iterator streams until TJS's own `term_cond` (the recall knob per ADR-0007 /
   DEV-1169) decides. A ported TJS can set the GUC per query but cannot adaptively extend one
   scan mid-flight — the recall curve becomes budget-shaped rather than term_cond-shaped, and the
   SM-4 recall-curve reporting discipline must be re-measured on that basis.

## Options (honest costs; every claim traces to E1-E3)

### Option A — stay on the 13.4 fork
- Own the EOL posture: CVE monitoring/backporting for PG 13.4 forever; "EOL fork" launch answer.
- Pay the F8 fork-repo migration (18 order-sensitive patches, ~150KB engine C as diffs against a
  gitignored vendor tree) — the maintenance tax the landscape review calls dominant.
- Keeps: the relaxed executor mechanism intact (E1), 32KB pages (E2), TJS's `term_cond`-owned
  recall knob and `xs_orderbyvals[0]` rank authority (E3 gaps don't apply).
- DEV-1259 Phase B lands on a platform whose exit question stays open.

### Option B — port to stock PG 17+ as pure extensions
- Graph AM + planner + graph_store_ext: **S** (measured zero API drift, E2). The single decision
  is pages: accept 8KB (4x adjacency I/O regression, needs a traversal re-benchmark) or require a
  self-built `--with-blocksize=32` PG 17 (forfeits managed PG — largely defeating the purpose).
- Operator re-host: **M-L.** `execTJS`/`tjs_open`/filter-first move into an extension driving
  pgvector's iterative scan, closing the three E3 gaps (own the scan loop; recompute or patch-in
  per-candidate distance; budget-based termination). The `tridb_vector_index_seam` header
  (patch #17, portable by design; plan 025 lineage) is the intended seam.
- What dies: the relaxed executor mechanism (`amcanrelaxedorderbyop`, nodeSort early-stop) — on
  stock PG the equivalent behavior lives inside pgvector's AM; the MSVBASE HNSW AM and its 10
  hardening patches (superseded by pgvector's mature, WAL-logged AM — which also dissolves the
  DEV-1259 Phase-B problem class, F5); and, unless re-derived on pgvector, `term_cond`-owned
  adaptive termination (recall becomes GUC-budget-capped: measured ceiling 0.965 vs the fork's
  curve, E3.3).
- Wins: current PG, managed-PG runnable, no fork repo, PG19 GRAPH_TABLE trajectory (F3).

### Option C — hybrid: graph+planner as stock extensions now, vector leg stays forked
- Immediately de-risks the graph side (it is already portable, E2) and lets the graph AM be
  developed/tested against stock PG in CI on any box.
- The vector leg (fork HNSW + relaxed executor + TJS in-place) continues on 13.4 until the
  Option-B operator re-host proves recall/latency parity on pgvector (re-run the E3 probe per
  pgvector minor, per plan 028 maintenance note).
- Cost: two build targets and a page-size split (graph extension must drop or parameterize the
  32KB assert to be truly stock-hostable — one line plus a re-benchmark; the fork side keeps 32KB).
- This is the sequencing the landscape review recommends (spike first, decide second; C1, C3).

## The public "why a fork today" answer (citable in README / launch FAQ)

> TriDB's defining operation is a tri-modal join that streams vector candidates in
> nearest-first order *through* graph and relational predicates with early termination. Stock
> PostgreSQL's executor forbids the required index behavior: an index scan that emits
> approximately-ordered tuples is rejected by the executor
> (`"index returned tuples in wrong order"`), and no stock hook lets a top-k sort stop pulling
> from a still-streaming ANN scan. MSVBASE's PostgreSQL fork adds exactly that mechanism —
> a relaxed-ordering contract between the index AM, the executor, and bounded sort — and TriDB
> builds on it because it is the only shipped implementation of the VBASE iterator model our
> operator needs. We know the costs: PG 13.4 is EOL and a fork can't run on managed Postgres.
> Our own measurements show the exit path: our graph store and planner extensions compile on
> stock PG 17 with zero API changes, and pgvector's 0.8+ iterative scans now provide a
> resumable ordered stream that solves the same starvation problem (recall@10 0.965 in our
> probe, vs 0.04 without it) — with known, narrowing gaps in per-candidate distance exposure
> and operator-controlled termination. The fork is our launch vehicle, not our destination;
> PostgreSQL 19's native `GRAPH_TABLE` (currently lowered to relational joins) is precisely the
> surface a future TriDB executor should serve natively.

## Recommendation (advisory only)

Option **C**. The graph leg is measurably free to port (E2) and buys stock-PG CI immediately; the
vector leg's gap list (E3) is short and shrinking with each pgvector release, so keep the fork as
the vector vehicle while the operator re-host is proven, and re-run `pgvector_iterative_probe.sql`
against each pgvector minor. Sequence per the landscape review: delay the F8 fork-repo migration
until this decision is made (C3), and shape DEV-1259 Phase B as the shmem-fallback (F5) rather
than the quarter centerpiece if B/C is chosen. Plan 025's compat shims are the porting seam for
the operator re-host.

## Consequences

- If A: schedule F8 immediately; budget CVE monitoring; keep this ADR as the FAQ source.
- If B/C: the 32KB assert becomes a parameterized capability (`gph_page.h`), the E3 gap list
  becomes the work breakdown for the operator re-host, and the SM-4 recall curve must be
  re-measured under GUC-budget termination.
- Regardless: the "why a fork today" paragraph above ships with the launch materials.

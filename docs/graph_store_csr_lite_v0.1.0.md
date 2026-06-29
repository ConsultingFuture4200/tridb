# Graph store CSR-lite layout — sorted-by-dst contiguous per-vertex extents (spike v0.1.0)

> **Status: SPIKE / design note — prototype BUILT + BENCHMARKED on the x86 engine image.** This
> is the design + analysis + measured-prototype deliverable of advisor Plan 009. It proposes a
> layout change but does **not** merge it to the shipped layout: the prototype lives only on
> branch `spike/009-csr-lite-prototype`. **Update (this run): the delta-tail CSR-lite prototype
> (§5) was implemented for real in `src/graph_store/` and compiled + run inside the
> `tridb/msvbase:dev` x86_64 fork image** (PG 13.4, `--with-blocksize=32`). `make graph-test` and
> both FR-7 suites pass with zero divergence; the §7 benchmark is filled with REAL x86 numbers.
> **ISA caveat:** this is x86_64, not the GX10 (ARM64+CUDA, 128 GB); the residency-pressure regime
> the design targets is GX10-ideal and is NOT decisively exercised here (see §7, §8).
>
> **Verdict (§8): CONDITIONAL — lean NO-GO on this evidence.** Write cost, correctness, and FR-7
> are all favorable, but the traversal page-read win — the entire justification — did not (and
> structurally could not) materialize on x86 with this chain-preserving prototype. Migration,
> compression (#14), and WCOJ stay deferred pending a GX10 re-run of a contiguity-delivering
> prototype.
>
> **Author:** advisor executor (Plan 009), prototyped at branch `spike/009-csr-lite-prototype`.
> **Amends (proposed):** ADR-0002 (addendum drafted in §8.2, NOT applied — verdict is not-GO).
> **Gates downstream:** adjacency compression (audit #14) and WCOJ — neither may start until this
> lands and the production decision is made (it has not).

---

## 1. TL;DR

- **Problem.** A vertex's out-edges are today **append-only, unsorted** `GphEdgeSlot`s packed
  into 32 KB pages, and a vertex that outgrows a page is **chained** via `gph_next_pageno`
  (`gph_page.h:51`; append `graph_am.c:436-473`; scan `graph_am.c:538-593`). Two costs follow:
  random `ReadBuffer` per overflow hop on traversal, and a `tjs` reachability predicate that
  must build a **hash membership set** at Open because neighbors arrive in arbitrary order.
- **Proposed layout (CSR-lite).** Keep each vertex's out-edges **sorted by `es_dst_vid`** in a
  **contiguous extent** of 32 KB pages, with a small **unsorted delta tail** that a maintenance
  merge folds into the sorted run (the LiveGraph / Sortledton approach). Hub vertices get a
  **degree-adaptive container** (separately grown), per the v0-limitations open question.
- **Why it's still native + TR-1-safe.** A contiguous-extent adjacency list is still an
  access method, **not** relational CSR built at query time (golden rule 3). Sorting happens at
  **write/maintenance time**; the scan stays **one slot per `Next()`** (no query-time full-list
  sort — that would be a blocking operator, TR-1 violation).
- **What this note delivers here:** the layout, the insert strategy + its write-amplification
  arithmetic (host-tested in `tools/csr_extent_packing.py` /
  `tests/test_csr_extent_packing.py`), the MVCC and shared-WAL analysis, the degree-adaptive
  threshold, the prototype C sketch, the benchmark design, and a **conditional go** with the
  bar the GX10 numbers must clear.
- **Recommendation (POST-measurement, x86 spike): CONDITIONAL — lean NO-GO on this evidence.**
  The prototype is writeable (bulk load within noise of append), correct (output now sorted),
  and FR-7-safe (zero divergence on both suites). **But the page-read traversal win — the entire
  justification — did not appear, and structurally could not: the prototype keeps the page-chain
  (no physical contiguity) and x86's warm 16 MB buffer pool cannot exercise the 128 GB residency
  pressure the design targets.** A new GenericXLog 4-page-cap cost on hub merges surfaced too
  (§7.2). Migration / compression #14 / WCOJ stay deferred; a GX10 re-run of a
  contiguity-delivering prototype is required to revisit GO (§8.1).

---

## 2. Current layout (baseline being challenged)

From the shipped source:

- **Edge slot** — `GphEdgeSlot`, fixed **32 bytes**, static-asserted (`gph_page.h:86-97`):
  `{es_src_vid u64, es_dst_vid u64, es_edge_type_id u32, es_flags u32, es_xmin TransactionId,
  es_pad u32}`. The MVCC version word is the **inline** `es_xmin`, checked by
  `gph_xmin_visible` on every emitted edge (`graph_am.c:577`).
- **Append path** — `gph_insert_edge` (`graph_am.c:366-481`) appends each new edge at `pd_lower`
  of the **tail** adjacency page (`vr_adj_tail`), `O(1)`; when the tail page is full it
  `gph_extend_page`s and **chains** via `GphPageSpecialPtr(tail)->gph_next_pageno`
  (`graph_am.c:444-473`). **No ordering anywhere.**
- **Traversal** — `gs_getnext` (`graph_am.c:538-593`) reads **one adjacency page per call**,
  walks slots in physical (= insertion) order, follows `gph_next_pageno` at page end, holds no
  pin across calls. Early termination works at the **emission** level (a `LIMIT k` above stops
  before later chain pages are read).
- **Page geometry** (host-computed, `tools/csr_extent_packing.py`, pinned by test):
  `BLCKSZ 32768 − PageHeader 24 − Special 16 = 32728` usable bytes ⇒ **1022 EdgeSlots per
  adjacency page** (matches ADR-0002 Decision 2's ~1021/1022 figure).

The pain (research audit 2026-06-28, and `docs/graph_store_v0_limitations.md` open question 1):

1. **Traversal page-reads.** A high-degree (hub) vertex's list spans many chained pages whose
   block numbers are **non-contiguous** (interleaved with every other vertex's pages because
   allocation is global append order). Each hop is a random `ReadBuffer`. At 128 GB, when the
   working set exceeds buffer-pool residency, this is the dominant traversal cost.
2. **`tjs` predicate.** The graph leg of `tjs` is a reachability test "is `dst ∈ neighbors(src)`?".
   With unsorted neighbors, the operator builds a **hash set** at Open and probes it. With
   sorted neighbors it becomes a **sorted merge** against the ranked vector stream — no hash
   build, and the multi-source frontier union becomes a **linear k-way merge**. (This note only
   *proves the layout is faster to traverse*; the predicate-as-merge rewrite is a downstream
   consumer plan.)

---

## 3. Proposed layout — sorted-by-dst contiguous extents (CSR-lite)

### 3.1 Shape

For each vertex `v`, its out-edges live in a **contiguous run of adjacency pages** (an
*extent*) in which `GphEdgeSlot`s are **ordered by `es_dst_vid`** (ties broken by
`es_edge_type_id`, then insert order — irrelevant in v1 with one edge type). "Contiguous" means
the extent's pages are allocated **adjacently** (consecutive block numbers) so a multi-page
traversal is a **sequential** scan, not a pointer chase. The existing `vr_adj_head` becomes the
extent's first block; `gph_next_pageno` within an extent points to the **physically next**
block (so REDO and the scan still work unchanged structurally), but the allocator's contract
changes from "any free block" to "next block in this vertex's extent, grow the extent if full".

This is **CSR-lite**, not full CSR: we do **not** maintain a single global offset array
(`row_ptr`) that a bulk-load rebuild would require. Each vertex owns its extent; the per-vertex
sorted run is the "lite" relaxation that keeps writes incremental and TR-1-friendly.

### 3.2 Insert strategy — delta tail + maintenance merge (RECOMMENDED)

Two candidates:

| Strategy | How an insert keeps order | Per-insert cost | When sorted |
|---|---|---|---|
| **In-place sorted insert** | binary-search the run for `es_dst_vid`, memmove the tail of the run up one slot, write | shifts `run_len − position` slots (≈ `run/2` expected) **every** insert; spills across page boundary when a page is full | always |
| **Delta tail + merge** (LiveGraph TEL / Sortledton) | **append** to a small unsorted **delta tail** at the extent's end (`O(1)`, exactly today's append); a maintenance pass merges the tail into the sorted run when the tail fills | `O(1)` per insert; one merge of `sorted_run + delta_cap` slots per `delta_cap` inserts | run sorted, tail unsorted |

**Recommendation: delta tail + maintenance merge.** Rationale (arithmetic host-tested in
`tools/csr_extent_packing.py`):

- In-place sorted insert pays an expected `run/2`-slot memmove **on every edge**. For a hub of
  degree 100 000 that is ~50 000 slot moves (1.6 MB memmoved) **per inserted edge** — fatal for
  the bulk-load corpus build that every benchmark depends on (this is exactly the STOP condition
  "sorted-on-insert causes unacceptable write amplification").
- Delta-tail amortizes: with a one-page delta cap (1022 slots), the **amortized** movement per
  insert is `(sorted_run + 1022) / 1022` slots
  (`csr_extent_packing.amortized_shift_per_insert`). For `sorted_run = 100 000` that is ≈ 99
  slots/insert amortized vs ≈ 50 000 in-place — a ~500× write-amplification reduction at that
  degree, and it keeps the **insert hot path byte-for-byte identical to today's append**
  (`GphPageAppendRecord` at `pd_lower`).
- The cost moves to a **maintenance merge** (off the write hot path; can run at checkpoint /
  vacuum cadence or when a query first needs a fully-sorted read of that vertex). The scan reads
  the sorted run as a sorted stream and the delta tail as a short unsorted stream, merging them
  **one slot per `Next()`** (§5.2) — so correctness never depends on the merge having run.

### 3.3 Degree-adaptive container (Sortledton)

The v0-limitations doc flags the unbounded hub problem (a hub becomes one giant chain / toasted
varlena). Sortledton's answer: **the container type depends on degree.**

- **Small vertices** (`degree < HUB_THRESHOLD`): inline — the whole sorted run + delta tail fit
  in **one** adjacency page (no chain hop at all). With 1022 slots/page, **a vertex of degree ≤
  1021 reads in a single buffer** (one page, zero hops). The knowledge-graph fan-out ADR-0002
  cites (10–100 neighbors) is comfortably inside this.
- **Hub vertices** (`degree ≥ HUB_THRESHOLD`): the extent spans multiple **contiguous** pages.
  The sorted run is split across pages by `es_dst_vid` range, so a range-restricted or
  merge-style probe can **skip** whole pages (a min/max `es_dst_vid` in each page's special area
  — a one-line addition to `GphPageSpecial` — turns the page list into a coarse sorted index).

**Threshold:** `HUB_THRESHOLD = slots_per_page = 1022` (one page). Justification: the threshold
that matters is "does the vertex still fit in a single page after its delta tail?" Below 1022 a
vertex is single-page and pays **zero** chain hops regardless of layout, so the sorted layout's
win is purely the predicate (hash→merge), not page-reads. At/above 1022 the page-read win
appears. Making the threshold exactly one page keeps the boundary self-describing and lets the
benchmark (§6) cleanly separate the two regimes. The host calculator's `is_hub` uses this rule.

---

## 4. MVCC and shared-WAL impact (the correctness-critical part)

### 4.1 MVCC: keep `es_xmin` attached to its slot

The version word is the **inline** `es_xmin` inside the 32-byte `GphEdgeSlot`. Two ways a
re-sort could preserve it:

| Option | Mechanic | Verdict |
|---|---|---|
| **(A) Move the whole 32-byte slot** | a re-sort / merge memmoves the entire `GphEdgeSlot`, so `es_xmin` travels with its `es_dst_vid` atomically — **never** decoupled | **Recommended for the spike.** Zero new MVCC surface: `gph_xmin_visible` keeps working unchanged because the slot it reads is still a self-contained unit. |
| **(B) Parallel version array** | store `es_dst_vid` keys densely, `es_xmin` in a separate array indexed by slot | More cache-efficient for compressed dst (no 8-byte xmin between keys) **but** every re-sort/merge must permute *two* arrays in lockstep; any divergence is a silent MVCC-visibility bug |

**Decision: (A) move the whole slot — for the spike.** It keeps the highest-risk regression
(losing/reordering a version word relative to its key) structurally impossible: the slot is the
unit, and the merge is a stable sort of whole slots.

> **Forward constraint for audit #14 (compression).** Delta+VByte compression of `es_dst_vid`
> will **force** a parallel version array (you cannot VByte-pack the dst keys while leaving an
> 8-byte `es_xmin` interleaved). So option (B) is the *eventual* destination. This note records
> the choice prominently so the compression plan inherits it: **compression and the
> parallel-version-array migration are the same change and must be planned together.** Doing (A)
> now is the right spike call (lowest risk to prove the traversal thesis); (B) is deferred to
> #14, which must add a version-array-vs-key permutation invariant test.

### 4.2 Shared-WAL impact — quantified

Both the current append and a sorted insert/merge log via **GenericXLog** (shared WAL, no
second rmgr — `gph_page.h:8-14`). The question is how much *more* a sorted write logs.

- **Today's append:** dirties one page (advances `pd_lower`, writes one slot). Under
  GenericXLog the new page is logged `GENERIC_XLOG_FULL_IMAGE` (32 KB) on first touch; a
  subsequent append to an already-imaged page in the same xact logs only the **delta** region.
- **Delta-tail append (recommended):** **identical** to today's append — the insert hot path is
  unchanged, so the per-insert WAL cost is unchanged. `tools/csr_extent_packing.py`
  `wal_full_image_bytes` confirms a single in-order insert logs **at most one** 32 KB page
  image, the same page-image count as today.
- **In-place sorted insert (rejected):** the memmove dirties **every byte from the insertion
  point to the end of the page**, so GenericXLog's delta diff degenerates to a near-full-page
  image on each insert — i.e. it inflates WAL volume toward 32 KB/insert for front-of-run
  inserts. This is the second reason to reject in-place (the first being the memmove CPU cost).
- **Maintenance merge:** rewrites `sorted_run + delta_tail` slots, i.e. it dirties the whole
  extent and logs a full image **per page of the extent** — but **once per `delta_cap`
  inserts**, off the hot path. For a hub of degree 100 000 (98 pages) the merge logs ~3.2 MB of
  WAL, amortized over 1022 inserts ≈ 3.1 KB/insert amortized — well under one page-image/insert.

**Net:** the delta-tail design adds **no** per-insert WAL over today's append, and its only
extra WAL is the periodic full-extent merge, amortized below one page-image per insert. The
in-place alternative would have roughly *tripled–doubled* per-insert WAL (the bad path we avoid).

---

## 5. Prototype C (BUILT + RUN on the x86 engine image)

> **Update:** the sketch below was IMPLEMENTED for real in `src/graph_store/gph_page.h` +
> `graph_am.c` on branch `spike/009-csr-lite-prototype` and compiled + run inside the
> `tridb/msvbase:dev` x86_64 fork image (PG 13.4, `--with-blocksize=32`). The harnesses
> (`scripts/graph_am_test.sh`, `txn_atomicity_test.sh`, `crash_recovery_test.sh`,
> `scripts/graph_layout_bench.sh`) PGXS-build the extension in the image at test time, so this is
> genuinely compiled C, not a sketch. The actual implementation differs from the original sketch in
> two ways forced by the engine: (1) `GphPageSpecial` grew to 32 bytes (the skip-scan range)
> without changing slots/page (still 1022); (2) the maintenance merge writes back in ≤4-page
> GenericXLog batches because GenericXLog caps at 4 buffers/record (§7.2). Read the source on the
> branch for the authoritative version; the blocks below are the design intent.

### 5.1 Append path — delta-tail (changes `gph_insert_edge`, `graph_am.c:366-481`)

The delta-tail strategy means the **insert path is unchanged** — it still appends at the extent
tail. The only additions are (a) the allocator grows the extent **contiguously** (request the
next block, not any free block) and (b) `GphPageSpecial` gains `min_dst`/`max_dst` per page for
skip-scan. Sketch:

```c
/* gph_page.h — add a coarse per-page key range for skip-scan + a sorted-run high-water mark.
 * (es_xmin stays INLINE in GphEdgeSlot — MVCC option (A), §4.1.) */
typedef struct GphPageSpecial
{
    uint16      gph_page_type;
    uint16      gph_unused;
    BlockNumber gph_next_pageno;     /* now: next *contiguous* block of this extent */
    uint64      gph_owner_vid;
    uint64      gph_min_dst;         /* NEW: min es_dst_vid of sorted slots on this page */
    uint64      gph_max_dst;         /* NEW: max es_dst_vid of sorted slots on this page */
    uint16      gph_delta_count;     /* NEW: # of trailing unsorted (delta) slots on this page */
    uint16      gph_pad2;
} GphPageSpecial;                    /* re-check GPH_SPECIAL_SIZE / MAXALIGN after adding */

/* gph_insert_edge: hot path is byte-identical to today's append (GphPageAppendRecord at
 * pd_lower). Only difference: bump gph_delta_count; do NOT sort here (TR-1: no work that
 * scales with degree on the write path). The sorted run is produced by the maintenance
 * merge (5.3), never on insert. */
```

### 5.2 Streaming sorted scan (changes `gs_getnext`, `graph_am.c:538-593`)

TR-1 is the hard constraint: the scan must emit **one slot per `Next()`** and must **not** sort
the whole list at scan time. The scan therefore does a **streaming 2-way merge** of (i) the
sorted run (already ordered on disk) and (ii) the short delta tail. Because the delta tail is
bounded by `delta_cap` (≤ one page), reading it once at Open is `O(delta_cap)` bounded work —
**not** an `O(degree)` blocking sort — so it does not violate TR-1 (the cost is constant in the
total degree, like an index page read, not proportional to the result the LIMIT may abandon).

```c
struct GraphScanDescData
{
    GraphVertexId      src;
    GraphScanDirection direction;
    BlockNumber        cur_blk;      /* current page in the *contiguous* extent */
    uint32             cur_slot;     /* next sorted-run slot on cur_blk */
    /* NEW: bounded delta-tail cursor, materialized once at Open (<= delta_cap slots) */
    GphEdgeSlot       *delta;        /* palloc'd in the scan's ctx; freed at Close */
    uint16             delta_n;      /* sorted copy of the delta tail */
    uint16             delta_i;      /* next delta slot to consider */
};

/* gs_getnext: at each call, pick the smaller es_dst_vid head of {sorted-run head, delta head},
 * advance that one cursor, apply the SAME visibility checks (es_flags DELETED, edge_type,
 * gph_xmin_visible — unchanged from graph_am.c:573-578), emit ONE edge, return.
 * Still: one adjacency page per call for the sorted run; no pin held across calls; LIMIT k
 * still stops before later extent pages are read (sequential now, not random hops). */
```

The visibility filter is unchanged (option (A) keeps `es_xmin` in the slot), so MVCC semantics
are identical to the shipped scan.

### 5.3 Maintenance merge (new — off the hot path)

A new internal function (callable at vacuum/checkpoint cadence, or lazily when a query needs the
fully-sorted run) stable-sorts the delta slots and merges them into the sorted run, rewriting
the extent under one `GenericXLogStart/Finish`, resetting `gph_delta_count` to 0 and refreshing
`gph_min_dst`/`gph_max_dst`. Stable sort of whole 32-byte slots ⇒ `es_xmin` preserved (§4.1 A).

---

## 6. Benchmark design (IMPLEMENTED + RUN on x86 — see §7 for the numbers)

> `test/graph_layout_bench.sql`, driven by `scripts/graph_layout_bench.sh` (builds BOTH layouts in
> one container and runs the same SQL against each). The method below is what was implemented; the
> measured numbers are in §7. **ISA caveat: run on x86_64, not the GX10** — see §7's environment
> note and §8 on why the page-read regime is GX10-ideal. GTM discipline: report **tables**, not
> single numbers.

**Corpus:** a mix of low- and high-degree vertices (skewed/power-law degree — e.g. most vertices
degree 8–100, a tail of hubs at 1k / 10k / 100k) so both the single-page regime (`< 1022`) and
the multi-page hub regime (`≥ 1022`) are exercised. State `N` vertices, edge count, and degree
distribution in the report.

**Measured, both layouts (page-chain baseline vs sorted-extent), for the same corpus:**

1. **Pages read per k=5 traversal** — the sequential-vs-random-hop win. Instrument
   `ReadBuffer` count (or `pg_statio` / a `gph_visit_counter`-style page counter) over a
   workload of k=5 traversals sampled across the degree distribution. Expect ~no change in the
   single-page regime, growing win as degree crosses into multi-page hubs.
2. **Reachability-predicate cost** — for a `tjs`-shaped "is `dst ∈ neighbors(src)`" query:
   hash-set build+probe (baseline) vs sorted-merge (sorted-extent). Report time and the absence
   of the hash build.
3. **Write cost** — edges/sec on bulk load (the write-amplification price). **This is the gating
   number**: delta-tail should match the append baseline within noise; in-place would crater it.

**Verification gate (GX10):** the benchmark runs and the table below is filled with corpus size
and method stated; a curve, not a bare number.

---

## 7. Results — MEASURED on the x86 engine image (spike branch)

> **Build/run environment.** Built + run inside the `tridb/msvbase:dev` x86_64 fork image (PG
> 13.4, `--with-blocksize=32`, BLCKSZ 32768) via `scripts/graph_layout_bench.sh`, which compiles
> BOTH layouts (sorted-extent = this branch's `src/graph_store/`; page-chain = the pre-spike
> baseline, instrumented with the SAME `gph_page_reads()` probe) in one container and runs
> `test/graph_layout_bench.sql` against each. **ISA caveat: this is x86_64, not the GX10
> (ARM64+CUDA, 128 GB).** The plan framed the GX10 128 GB residency-pressure regime as the ideal;
> these numbers are informative for write cost, correctness, and the page-read *artifact
> structure*, but the sequential-vs-random-hop locality win the design targets is a buffer-pool /
> prefetch effect this single box (16 MB `shared_buffers`) cannot decisively exercise. See §8.
>
> **Corpus.** N = 20 000 vertices; 87 500 directed `:related_to` edges. Degree mix: three hubs
> (deg 5000, 1500, 1000 — multi-page extents) + a band of 5 000 low-degree vertices (deg 16 —
> single-page regime). dst ids are deliberately scattered (not ascending), so the sorted layout
> genuinely re-orders them (delta tail + maintenance merge exercised, including the >4-page hub
> merge that the GenericXLog 4-page cap forces into batches — see the finding in this section).

| Metric | Page-chain (baseline) | Sorted-extent (CSR-lite) | Corpus / method |
|---|---|---|---|
| Pages read / k=5 traversal, low-degree (deg 16, single page) | 5 | 2 | vids 150/1000/3000; `gph_neighbors(v) LIMIT 5`, `gph_page_reads` delta |
| Pages read / k=5 traversal, hub deg 1000 | 5 | 2 | vid 2 |
| Pages read / k=5 traversal, hub deg 1500 | 5 | 3 | vid 1 |
| Pages read / k=5 traversal, hub deg 5000 | 5 | 5 | vid 0 |
| Pages read, FULL hub scan (deg 5000) | 5005 | 4095 | vid 0; `count(*) FROM gph_neighbors(0)` |
| `tjs`-shaped predicate, **present** dst (ms/probe, 200 reps) | 5.03 | 5.39 | `EXISTS(… WHERE x=503)` on hub 0 |
| `tjs`-shaped predicate, **absent** dst (ms/probe, 200 reps) | 5.37 | 5.63 | `EXISTS(… WHERE x=999999)` on hub 0 |
| Bulk-load throughput (edges/sec) | 20 413 | 19 908 | 87 500 edges incl. the >4-page hub merges |
| Neighbor output order (hub 0) | insertion (`nondecreasing=f`) | **sorted asc** (`nondecreasing=t`) | correctness cross-check |
| FR-7 txn-atomicity divergence | 0 | **0** | `txn_atomicity_test.sh` (SM-5, 200 randomized iters) |
| FR-7 crash-recovery divergence | 0 | **0** | `crash_recovery_test.sh` scen. 1 (REDO) + 2 (abort) |

### 7.1 How to read these numbers (the honest interpretation)

- **Write cost is the gating number and it passes:** 19 908 vs 20 413 edges/sec ≈ **−2.5%**, within
  run-to-run noise (a second run measured 21 000 vs 21 072, i.e. −0.3%). The delta-tail hot path is
  byte-identical to the append baseline, so bulk load is NOT cratered — the classic "sorted lists
  are expensive to write" objection is neutralized, **including** the cost of the periodic
  maintenance merges (the corpus build triggers them on every hub). This validates the §3.2 / §4.2
  write-amplification analysis on real hardware.
- **The k=5 page-read numbers are dominated by a measurement artifact, not by the layout's thesis.**
  Both scans re-read a page on every `Next()` (no buffer pin held across calls — the leak-free
  early-abandon contract). So the baseline reads exactly 5 buffers for ANY k=5 (even a 1-page
  vertex re-reads the same cached page 5×). The sorted layout reads *fewer* in most cases only
  because its bounded delta tail is materialized into backend memory once at Open and then served
  from RAM — an incidental win, **not** the sequential-extent locality the design is about.
- **The sequential-vs-random-hop win the design targets does NOT appear here, by construction.**
  The prototype keeps the `gph_next_pageno` chain (it sorts WITHIN the existing page set; it does
  not yet guarantee physically-contiguous extent allocation). So the "random `ReadBuffer` per
  overflow hop → sequential extent read" improvement has no structural foothold in this prototype,
  and the warm 16 MB buffer pool on x86 would mask it even if it did. The full-hub `4095 vs 5005`
  delta is again the in-memory-delta artifact, not contiguity.
- **The predicate is a wash (5.0–5.6 ms, sorted slightly slower).** The `gph_neighbors` SRF always
  full-scans; the sorted layout's early-stop-on-merge advantage lives in the *consumer* (`tjs`
  operator), which is explicitly out of scope here. The sorted path is marginally slower because of
  the per-Open delta materialization + the 2-way merge bookkeeping. This confirms only that sorting
  does not *hurt* the predicate at the SRF level; the predicate WIN must be demonstrated by the
  downstream `tjs`-as-sorted-merge plan, not this layout spike.
- **Correctness + FR-7 are unambiguous:** hub 0 comes out globally ascending (`nondecreasing=t`)
  vs the baseline's insertion order (`f`), proving the layout actually sorts; and BOTH FR-7 suites
  (atomicity incl. 200-iter randomized cross-store SM-5, and crash-recovery REDO + abort) show
  **zero divergence** on the sorted layout. The re-layout did not break MVCC or crash atomicity.

### 7.2 GenericXLog 4-page cap — a real design finding

The first benchmark run failed with `maximum number 4 of generic xlog buffers is exceeded`:
GenericXLog registers at most `MAX_GENERIC_XLOG_PAGES = 4` buffers per WAL record, but a hub
extent re-sort rewrites every page of the extent (a deg-5000 hub ≈ 5 pages). **A full extent
re-sort therefore cannot be a single atomic WAL record under the shared-WAL-only constraint
(golden rule 2 forbids a second rmgr).** The prototype resolves this by writing the merge back in
**≤4-page batches, one GenericXLog record per batch** (`GPH_MERGE_BATCH`). Per-page slot COUNT is
preserved exactly (full pages = 1022, last = remainder, identical to pre-merge), so the slot
*multiset* — the invariant the scan and FR-7 visibility depend on — survives a crash *between*
batches; only the global *order* is briefly imperfect until the merge re-runs. This is why both
FR-7 suites still pass. But it means the maintenance merge of a hub is **not itself crash-atomic**
as a single unit — a finding the production plan must weigh (see §8).

---

## 8. Recommendation + proposed ADR-0002 addendum

### 8.1 Go / no-go — VERDICT FROM THE MEASURED x86 SPIKE

**Verdict: CONDITIONAL — lean NO-GO *on this evidence*; do NOT start the production migration
(or compression #14 / WCOJ) yet.** The spike built and ran the delta-tail CSR-lite prototype for
real on the x86 engine image and proved three of the four things it needed to, but **failed to
demonstrate the one thing that justifies the whole HIGH-risk change** — the traversal win.

What the spike PROVED (positive, real x86 numbers):

1. **Writeable.** Bulk load is within noise of the append baseline (−0.3% to −2.5% across runs),
   *including* the maintenance merges the corpus build triggers on every hub. The "sorted lists
   are expensive to write" objection is empirically dead. (§7.1)
2. **Correct + MVCC-safe.** The layout actually sorts (hub 0 `nondecreasing=t` vs baseline `f`),
   and MVCC option (A) (whole-slot moves) held: `gph_xmin_visible` is unchanged and every
   visibility check passed.
3. **FR-7 not regressed.** `txn_atomicity_test.sh` (incl. the 200-iter randomized SM-5 cross-store
   consistency check) and `crash_recovery_test.sh` (REDO of a committed tri-store row + abort of a
   crash-interrupted one) both show **zero divergence** on the spike branch. `make graph-test`
   passes end-to-end.

What the spike did NOT prove (the blocker):

4. **The traversal page-read win did not materialize on this hardware, and could not have.** The
   k=5 page-read numbers are dominated by the per-`Next()` re-read artifact (no pin across calls),
   not by sequential-vs-random locality; and **the prototype keeps the `gph_next_pageno` chain, so
   it does not even deliver physically-contiguous extents** — the exact mechanism (sequential
   extent read replacing random hops at 128 GB residency pressure) that the design rests on has no
   structural foothold in the prototype, and a warm 16 MB x86 buffer pool would mask it regardless.
   The predicate is a wash at the SRF level (the merge-win is a downstream `tjs` concern, out of
   scope). **So the central performance claim is still unproven.**

Plus a new cost surfaced: **the hub maintenance merge is not crash-atomic as a single WAL record**
(GenericXLog's 4-page cap forces ≥2 records for any extent >4 pages; §7.2). The multiset survives
a crash, but a production design wanting the merge itself atomic would need a shadow-extent swap or
a custom rmgr (the latter blocked by golden rule 2). That is extra design surface the migration
must carry.

**Decision rule applied:**

- This is the plan's stated **NO-GO branch**: "the hub page-read win does not appear at runnable
  scale → the page-chain is fine at the scales that matter → a HIGH-risk migration is unjustified."
  We reach it not because the page-chain is proven fine at 128 GB, but because **the prototype as
  built cannot deliver or measure the win**, and shipping a HIGH-risk layout change on an unproven
  performance thesis is exactly the bar the plan set to avoid.
- **This is recorded as CONDITIONAL, not a hard close**, because the *write/correctness/FR-7*
  results are favorable and the design is sound — the gap is evidentiary, not a defect. The
  finding is **parked pending a GX10 re-run of a contiguity-delivering prototype** (see the
  conditions below), not deleted. Compression (#14) and WCOJ stay deferred until then.

**To flip this to GO, a follow-up spike must (all of):**

- (a) deliver **physically-contiguous extent allocation** (a real extent allocator, not the reused
  chain), so the sequential-read mechanism actually exists to measure;
- (b) run on the **GX10 at 128 GB with residency pressure** (working set > buffer pool), where the
  random-hop penalty the design targets is real — x86 with a warm pool is the wrong instrument;
- (c) measure pages-read with a **pin-held-across-Next streaming scan** (or `pg_statio` at the
  relation level) so the per-`Next()` re-read artifact does not swamp the locality signal;
- (d) keep bulk-load within noise and both FR-7 suites at zero divergence (the spike shows this is
  achievable);
- (e) settle the hub-merge crash-atomicity question (§7.2) — shadow-extent swap vs accepting the
  multiset-preserving-but-not-order-atomic merge.

Until (a)–(c) are demonstrated, **the page-read thesis is unsubstantiated and the migration must
not proceed.**

### 8.2 Proposed ADR-0002 addendum (DRAFT — not applied; append, do not rewrite)

> To be appended to `docs/decisions/0002-adjacency-list-graph-store-layout.md` **only if GO**,
> as its own dated addendum section. Drafted here so the reviewer can see the exact proposal.
> **NOT APPLIED** — the §8.1 verdict is CONDITIONAL / lean NO-GO, so ADR-0002 is unchanged. The
> draft also still cites a "CONTIGUOUS extent" and a "GX10 benchmark" the x86 prototype did not
> deliver (it preserves the chain); a GO addendum would need the contiguity-delivering GX10
> evidence first.

```
## Addendum A (proposed, Plan 009 spike) — Sorted-by-dst CSR-lite extents

Date: <fill on acceptance>   Status: Proposed (spike evidence: docs/graph_store_csr_lite_v0.1.0.md)

Decision 2 (32KB page layout) is amended: a vertex's adjacency pages form a CONTIGUOUS
extent whose EdgeSlots are SORTED by es_dst_vid, with a bounded unsorted delta tail merged
at maintenance time (LiveGraph/Sortledton). Insert hot path is unchanged (append to delta
tail). Hub vertices (degree >= one page = 1022 slots) use the degree-adaptive multi-page
extent with per-page min/max es_dst_vid for skip-scan. es_xmin stays INLINE per slot (whole-
slot stable sort preserves MVCC); the eventual compression plan (#14) migrates to a parallel
version array and MUST be planned with that migration.

Rationale: sequential extent reads replace random page-chain hops at the 128GB residency
bottleneck; sorted dst enables the tjs predicate as a sorted-merge (no hash build) and is the
prerequisite for adjacency compression and WCOJ. Evidence: GX10 benchmark <link §7 table>.

Migration: the production change is a SEPARATE, higher-risk plan with a data-migration story
for existing graph stores, gated on the FR-7 atomicity + crash-recovery suites passing.
```

### 8.3 Staged migration (the production follow-on — separate plan, not this spike)

1. Land the layout behind the AM with both readers (chain + extent) so existing stores still
   read; new writes go to extents.
2. A one-shot **migration** that re-lays each existing vertex's chain into a sorted extent
   (offline or vacuum-style), with the FR-7 crash-recovery suite run **across** the migration.
3. Flip the default; keep the chain reader one release for rollback, then delete it (golden rule
   9: no indefinite compat shim).
4. Only after this lands: open the compression plan (#14) inheriting the §4.1 version-array
   decision, then WCOJ.

---

## 9. Scope guardrails honored

- **Native AM, not relational CSR** — contiguous extents are still an access method; no edge
  join table, no query-time CSR build (golden rule 3).
- **TR-1** — sorting is write/maintenance-time; the scan stays one-slot-per-`Next()` streaming
  merge with a bounded delta read, no `O(degree)` blocking sort at scan time.
- **Shared WAL only** — all writes via GenericXLog; no second rmgr/WAL (golden rule 2).
- **Out of scope (not touched):** compression (#14), WCOJ, the vector/relational stores, the
  `tjs` operator body, and merging the prototype into the shipped layout (that is the gated
  production follow-on).

## 10. Artifact status

| Artifact | Status |
|---|---|
| This design note (layout, insert strategy, degree-adaptive, MVCC, WAL, recommendation) | **Done** |
| `tools/csr_extent_packing.py` + `tests/test_csr_extent_packing.py` (host packing/WAL arithmetic) | **Done** — `make test`/`make lint` green (special size updated to 32 B) |
| §5 prototype C (append/scan/merge edits) in `src/graph_store/` (spike branch) | **BUILT + RUN on x86 `tridb/msvbase:dev`** — compiles, all graph suites pass |
| §6/§7 benchmark + filled results table | **DONE on x86** — `test/graph_layout_bench.sql` + `scripts/graph_layout_bench.sh`, real numbers in §7. ISA caveat: not GX10 |
| FR-7 atomicity + crash-recovery on spike branch | **PASS on x86** — `txn_atomicity_test.sh` + `crash_recovery_test.sh`, **zero divergence** |
| ADR-0002 addendum (§8.2) | **Drafted, NOT applied** — verdict is CONDITIONAL / lean NO-GO |
| Shipped layout on `master` (`gph_page.h`, `graph_am.c`) | **Unchanged** — the prototype lives only on `spike/009-csr-lite-prototype` |

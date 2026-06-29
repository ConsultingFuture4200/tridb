# Graph store CSR-lite layout — sorted-by-dst contiguous per-vertex extents (spike v0.1.0)

> **Status: SPIKE / design note.** This is the design + analysis deliverable of advisor Plan
> 009. It proposes a layout change but does **not** merge it: the shipped layout
> (`src/graph_store/gph_page.h`, `graph_am.c`) is unchanged. The prototype C in §5 is an
> **UNBUILT-HERE sketch** — the graph AM compiles only inside the MSVBASE fork on the GX10
> (PG 13.4, `--with-blocksize=32`). The measured benchmark (§6) and the FR-7 atomicity/crash
> verification are **GX10-pending** and are not claimed to pass here.
>
> **Author:** advisor executor (Plan 009), commit `8b19cb5`, 2026-06-28.
> **Amends (proposed):** ADR-0002 (addendum drafted in §8, not yet applied).
> **Gates downstream:** adjacency compression (audit #14) and WCOJ — neither may start until
> this lands and the production decision is made.

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
- **Recommendation (pre-measurement lean):** **conditional GO** to a delta-tail design — but the
  layout migration is HIGH-risk and must not merge until the GX10 benchmark (§6) shows a real
  page-read win **and** the FR-7 atomicity/crash suites pass on the spike branch. If the win
  doesn't materialize at GX10-runnable scale, this is a **NO-GO** and the finding is closed.

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

## 5. Prototype C sketch (UNBUILT-HERE — GX10-pending)

> Compiles only inside the MSVBASE fork on the GX10. The sketch below specifies the edits; it is
> **authored/specified, not built or run here**, and not claimed to pass.

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

## 6. Benchmark design (GX10-pending — not run here)

> `test/graph_layout_bench.sql` (or a `scripts/` driver). **Authored as a design here; the
> measured numbers are GX10-only** and are not filled in. This section is the method; §7's table
> is the shell the GX10 run fills. GTM discipline: report **curves/tables**, not single numbers.

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

## 7. Results (GX10-pending — to be filled by the GX10 run)

| Metric | Page-chain (baseline) | Sorted-extent (CSR-lite) | Corpus / method |
|---|---|---|---|
| Pages read / k=5 traversal (single-page regime, deg ≤ 1021) | _pending_ | _pending_ | _N=?, deg dist=?_ |
| Pages read / k=5 traversal (hub regime, deg = 1k/10k/100k) | _pending_ | _pending_ | _curve over degree_ |
| `tjs` reachability predicate (ms; hash build vs merge) | _pending_ | _pending_ | _Q=?_ |
| Bulk-load throughput (edges/sec) | _pending_ | _pending_ | _M edges_ |
| FR-7 atomicity divergence | _pending (must be 0)_ | _pending (must be 0)_ | `txn_atomicity_test.sh` |
| Crash-recovery divergence | _pending (must be 0)_ | _pending (must be 0)_ | `crash_recovery_test.sh` |

> Until these cells are filled by a GX10 run on the spike branch, **no GX10 done-criterion is
> claimed to pass** (per the hardware gate).

---

## 8. Recommendation + proposed ADR-0002 addendum

### 8.1 Go / no-go

**Lean: conditional GO to the delta-tail CSR-lite design — gated on the §6 GX10 numbers.** The
design analysis is favorable:

- The delta-tail strategy adds **zero** per-insert cost (hot path = today's append) and **zero**
  per-insert WAL over the baseline (§4.2), so the classic "sorted lists are expensive to write"
  objection is neutralized by construction (host-verified arithmetic).
- MVCC option (A) makes the highest-risk regression (version-word/key decoupling)
  **structurally impossible**.
- The traversal win is **regime-dependent**: it is ~nil for single-page vertices (deg ≤ 1021)
  and grows with hub degree. **So the go/no-go hinges entirely on whether the corpus's hubs are
  big enough, and resident-set pressure high enough, for sequential extents to beat random
  chain hops at GX10-runnable scale.**

**Decision rule for the production plan:**

- **GO** iff the GX10 benchmark shows (a) a material page-read reduction in the hub regime,
  (b) bulk-load throughput within noise of the append baseline, **and** (c) `make graph-test` +
  both FR-7 suites pass with **zero** divergence on the spike branch.
- **NO-GO** (close the finding, do not re-audit) if the hub page-read win does not appear at
  GX10-runnable scale — the page-chain is then "fine at the scales that matter" and a HIGH-risk
  migration is unjustified. Record the negative result in §7 and stop; compression (#14) and
  WCOJ, which depend on sorted dst, are then also deferred indefinitely with this as the reason.
- **STOP-and-report** (not silent re-design) if any FR-7 atomicity/crash test diverges, or if
  even the delta-tail bulk load craters throughput — correctness/writeability beat layout.

### 8.2 Proposed ADR-0002 addendum (DRAFT — not applied; append, do not rewrite)

> To be appended to `docs/decisions/0002-adjacency-list-graph-store-layout.md` **only if GO**,
> as its own dated addendum section. Drafted here so the reviewer can see the exact proposal.

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
| This design note (layout, insert strategy, degree-adaptive, MVCC, WAL, recommendation) | **Done here** |
| `tools/csr_extent_packing.py` + `tests/test_csr_extent_packing.py` (host packing/WAL arithmetic) | **Done here** — `make test`/`make lint` green |
| §5 prototype C (append/scan/merge edits) | **Authored/specified, UNBUILT-HERE (GX10-pending)** |
| §6/§7 benchmark + filled results table | **GX10-pending** — not run, not claimed to pass |
| FR-7 atomicity + crash-recovery on spike branch | **GX10-pending** — not run, not claimed to pass |
| ADR-0002 addendum (§8.2) | **Drafted, NOT applied** — apply only on GO |
| Shipped layout (`gph_page.h`, `graph_am.c`) | **Unchanged** (this is a spike) |

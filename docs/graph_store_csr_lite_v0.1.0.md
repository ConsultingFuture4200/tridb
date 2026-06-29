# Graph store CSR-lite layout — sorted-by-dst contiguous per-vertex extents (spike v0.1.0)

> **Status: SPIKE / design note — prototype BUILT + BENCHMARKED on the x86 engine image.** This
> is the design + analysis + measured-prototype deliverable of advisor Plan 009. It proposes a
> layout change but does **not** merge it to the shipped layout: the prototype lives only on the
> spike branch. **Update (contiguity follow-up run, branch `spike/009-contiguity`): the two
> mechanisms the prior run's NO-GO flagged as missing were BUILT and MEASURED** — (1) a real
> CONTIGUOUS extent allocator (`gph_extend_pages_contig` + `gph_relayout_extent_contig`): a
> vertex's adjacency pages now occupy a SEQUENTIAL run of block numbers, migrated to a fresh
> contiguous run on every grow, so contiguity holds even under interleaved ingest; and (2) a
> read-once-per-page streaming scan (bounded per-page buffer, no pin held across `Next()`)
> replacing the prior re-read-per-neighbor scan. Implemented in `src/graph_store/` and compiled +
> run inside the `tridb/msvbase:dev` x86_64 fork image (PG 13.4, `--with-blocksize=32`).
> `make graph-test` and BOTH FR-7 suites pass with zero divergence; the §7 benchmark is filled
> with REAL x86 numbers including the contiguity proof and a tiny-`shared_buffers`
> residency-pressure run.
> **ISA caveat:** this is x86_64 with a warm OS page cache, not the GX10 (ARM64+CUDA, 128 GB +
> NVMe). The page-touch COUNT win and the contiguity GUARANTEE are now demonstrated here; the
> decisive disk-seek / readahead component of the sequential-vs-random win still needs real disk
> I/O (dropped caches / O_DIRECT) or the GX10 NVMe+128GB regime (see §7, §8).
>
> **Verdict (§8): STILL-INCONCLUSIVE — leaning GO, gated on a GX10 real-I/O re-run.** The two
> blockers from the prior run are RESOLVED here: contiguity is real (consecutive block numbers,
> proven under interleaved load) and the scan reads each page once (full-hub page reads dropped
> from 5005 to 6, ~830x fewer `ReadBuffer` calls, with an ~11% wall-clock win even on a warm
> cache). Write cost stayed within noise; correctness + FR-7 are clean. What remains GX10-gated is
> only the disk-seek magnitude (a warm x86 cache cannot show readahead). Migration / compression
> (#14) / WCOJ stay deferred until the GX10 real-I/O number confirms the wall-clock win scales.
>
> **Author:** advisor executor (Plan 009), contiguity follow-up at branch `spike/009-contiguity`.
> **Amends (proposed):** ADR-0002 (addendum drafted in §8.2, NOT applied — verdict not yet a hard GO).
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
- **Recommendation (POST-measurement, contiguity follow-up): STILL-INCONCLUSIVE — leaning GO,
  gated on a GX10 real-I/O re-run.** The prototype is writeable (bulk load within noise of
  append), correct (output sorted), and FR-7-safe (zero divergence on both suites) — AND the two
  prior blockers are now resolved: (a) **physical contiguity is real** — a hub's pages are a
  consecutive block run (proven to hold even under interleaved load, where the page-chain baseline
  scatters), via a real contiguous extent allocator + migrate-on-grow; (b) **the scan reads each
  page once** — full-hub `ReadBuffer` count fell from 5005 (re-read per neighbor) to 6 (one per
  page), an ~830x reduction, with an ~11% wall-clock win under tiny `shared_buffers` even on a
  warm cache. The ONLY thing still GX10-gated is the disk-seek magnitude of the sequential vs
  random pattern (a warm x86 OS cache masks readahead). The GenericXLog 4-page-cap finding (§7.2)
  is handled (batched relayout). Migration / compression #14 / WCOJ stay deferred until the GX10
  real-I/O number confirms the wall-clock win scales (§8.1).

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

### 5.4 Contiguity follow-up — real extent allocator + read-once scan (BUILT, branch `spike/009-contiguity`)

The first run kept the `gph_next_pageno` chain and re-read a page per `Next()`, so neither physical
contiguity nor the locality measurement existed. This follow-up adds the two missing mechanisms (in
`src/graph_store/graph_am.c`):

- **Physical contiguity — `gph_extend_pages_contig(rel, n)` + `gph_relayout_extent_contig(...)`.**
  When a vertex's tail page fills (the extent must grow), instead of chaining one more `P_NEW` block
  (which interleaves with every other vertex's pages → no contiguity), the whole extent is MIGRATED
  into a fresh CONTIGUOUS run of `ceil(total_slots / slots_per_page)` blocks reserved in ONE relation
  extension (sequential block numbers guaranteed by the extension lock). All slots are collected,
  stable-sorted (so the new run is globally ordered, `delta_count = 0`), laid across the contiguous
  pages chained to the physically-next block, written under `≤GPH_MERGE_BATCH`-page GenericXLog
  records, and `vr_adj_head`/`vr_adj_tail`/`vr_adj_cap` are repointed. MVCC option (A) holds (whole
  32-byte slot copies carry `es_xmin`), so abort/crash leave the identical visible set; the old chain
  pages are orphaned dead space (compaction reclaims them). Result: a vertex's extent is a sequential
  block run even under interleaved ingest (proven §7).
- **Read-once-per-page streaming scan (`gs_read_page_into_buf` + reworked `gs_load_run_head`).** The
  scan reads each extent page EXACTLY ONCE: on first entering a page it copies that page's sorted-run
  slots into a bounded scan-local buffer (`page_buf`, ≤ slots/page) and IMMEDIATELY releases the
  buffer, then serves neighbors one-per-`Next()` from the buffer until it drains, then reads the next
  page once. No buffer pin is held across a `Next()` return — so it is LEAK-FREE on `LIMIT` early
  abandon (the common TR-1 case, where a held pin would have leaked). Only ONE page is ever buffered
  (streaming, bounded — not the whole list), so TR-1 holds and a `LIMIT k` stops before later extent
  pages are read. This is what makes the §7 full-hub page-read count drop from 5005 (re-read per
  neighbor) to 6 (one per page).

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

## 7. Results — MEASURED on the x86 engine image (contiguity follow-up, branch `spike/009-contiguity`)

> **Build/run environment.** Built + run inside the `tridb/msvbase:dev` x86_64 fork image (PG
> 13.4, `--with-blocksize=32`, BLCKSZ 32768) via `scripts/graph_layout_bench.sh`, which compiles
> BOTH layouts (sorted-extent + contiguous = this branch's `src/graph_store/`; page-chain = the
> pre-spike baseline, instrumented with the SAME `gph_page_reads()` + `gph_adj_blocks()` probes) in
> one container and runs `test/graph_layout_bench.sql` against each. **Residency-pressure regime:
> `shared_buffers` set to the Postgres minimum (512 kB = 16 buffers) so the ~100-page working set
> far exceeds the pool.** **ISA caveat: this is x86_64 with a WARM OS page cache, not the GX10
> (ARM64+CUDA, 128 GB + NVMe).** The page-touch COUNT win and the contiguity guarantee are
> demonstrated here; the disk-seek / readahead component of the win needs real disk I/O (dropped
> caches / O_DIRECT) or the GX10 NVMe regime. See §8.
>
> **Corpus.** N = 20 000 vertices; 87 500 directed `:related_to` edges. Degree mix: three hubs
> (deg 5000, 1500, 1000 — multi-page extents) + a band of 5 000 low-degree vertices (deg 16 —
> single-page regime). dst ids are deliberately scattered (not ascending), so the sorted layout
> genuinely re-orders them. PLUS an INTERLEAVED-load sub-corpus: two deg-3000 hubs (vids 9000/9001)
> grown in lockstep so their page extensions interleave — the case that distinguishes a real
> contiguous allocator from the chain (the main corpus loads each hub back-to-back, so even the
> chain happens to land contiguous there).

| Metric | Page-chain (baseline) | Sorted-extent + contiguous | Corpus / method |
|---|---|---|---|
| **Contiguity, back-to-back load** — hub deg 5000 block run | blks 21..25, `contiguous=t` | blks 31..35, `contiguous=t` | `gph_adj_blocks(0)`; both contiguous here (no interleave) |
| **Contiguity, INTERLEAVED load** — hub A (vid 9000) | first=5029 last=5033 span=4 / 3 pages → **`contiguous=f`** (5029,5031,5033 scattered) | first=5046 last=5048 span=2 / 3 pages → **`contiguous=t`** | `gph_adj_blocks(9000)`; the decisive test |
| **Contiguity, INTERLEAVED load** — hub B (vid 9001) | 5030,5032,5034 → **`contiguous=f`** | 5049,5050,5051 → **`contiguous=t`** | `gph_adj_blocks(9001)` |
| Pages read / k=5 traversal (early-term), any bucket | 5 | 2 | `gph_neighbors(v) LIMIT 5`, `gph_page_reads` delta |
| **Pages read, FULL hub scan (deg 5000)** | **5005** | **6** | vid 0; `count(*) FROM gph_neighbors(0)` — read-once-per-page |
| **Wall-clock, repeated FULL hub scan (300 reps, tiny `shared_buffers`)** | **3.32 ms/scan** | **2.97 ms/scan (−11%)** | vid 0; residency-pressure section |
| `tjs`-shaped predicate, **present** dst (ms/probe, 200 reps) | 3.22 | 2.74 | `EXISTS(… WHERE x=503)` on hub 0 |
| `tjs`-shaped predicate, **absent** dst (ms/probe, 200 reps) | 3.47 | 2.92 | `EXISTS(… WHERE x=999999)` on hub 0 |
| Bulk-load throughput (edges/sec) | 4 235 | 4 191 (**−1.0%**) | 87 500 edges incl. the contiguous relayout migrations |
| Neighbor output order (hub 0) | insertion (`nondecreasing=f`) | **sorted asc** (`nondecreasing=t`) | correctness cross-check |
| FR-7 txn-atomicity divergence | 0 | **0** | `txn_atomicity_test.sh` (SM-5, 200 randomized iters) |
| FR-7 crash-recovery divergence | 0 | **0** | `crash_recovery_test.sh` scen. 1 (REDO) + 2 (abort) |

> Note the edges/sec here (~4.2 k) is lower than the prior run's ~20 k because this run uses the
> Postgres-minimum 512 kB `shared_buffers` (residency pressure) instead of 16 MB; both layouts pay
> that cost equally, so the −1.0% RELATIVE write delta is the meaningful figure.

### 7.1 How to read these numbers (the honest interpretation)

- **Contiguity is now REAL and proven where it matters.** Under interleaved growth the page-chain
  baseline scatters each hub's pages (vid 9000 → 5029/5031/5033, `contiguous=f` — every other
  vertex's page lands in between), exactly the random-hop pathology the design names. The
  sorted-extent layout migrates each hub to a fresh contiguous run on every grow, so it stays a
  consecutive block run (5046/5047/5048, `contiguous=t`) regardless of interleaving. This is the
  §8 GO-precondition (a) — a real extent allocator, not the reused chain — delivered and verified.
- **The scan reads each page ONCE.** Full deg-5000 hub scan: **6** page reads (sorted) vs **5005**
  (chain) — the chain's `gs_getnext` re-reads a page on every `Next()` (one `ReadBuffer` per
  emitted neighbor + chain hops), the prior prototype's measurement artifact. The read-once scan
  buffers one page's run slots and serves the neighbors from RAM, touching each extent page exactly
  once. This is GO-precondition (c) — a pin/read-once scan so the per-`Next()` re-read does not
  swamp the locality signal — delivered. It stays STREAMING (one neighbor per `Next()`, only ONE
  page buffered, bounded by slots/page) and LEAK-FREE on `LIMIT` early abandon (no pin held across
  a `Next()` return, so abandoning the SRF mid-scan leaks nothing — the common TR-1 case).
- **Wall-clock win is real but modest on a warm cache, and that is the HONEST caveat.** Under the
  tiny 512 kB pool the full-hub scan is ~11% faster (2.97 vs 3.32 ms/scan) and the `tjs`-shaped
  predicate is ~15% faster (2.74 vs 3.22 ms present). This is the CPU / buffer-lookup cost of
  issuing 6 `ReadBuffer` calls instead of 5005 — NOT yet the disk-seek/readahead component. On a
  warm x86 OS page cache every page the chain re-reads is still resident, so the random-vs-
  sequential *seek* difference is masked; the contiguity layout makes the touches consecutive block
  numbers, but the OS never has to seek for them here. The decisive seek/readahead magnitude needs
  real disk pressure (dropped caches / O_DIRECT) or the GX10's NVMe+128 GB regime where the working
  set genuinely exceeds RAM. **We measured what this box can measure and do not extrapolate the
  seek win.**
- **Write cost held:** 4 191 vs 4 235 edges/sec ≈ **−1.0%**, within noise — *including* the
  contiguous-relayout migrations the corpus build triggers on every hub grow. The insert hot path
  is still a byte-identical append + a `delta_count` bump; contiguity cost is paid off the hot path
  in the migrate-on-grow relayout (which, like the prior in-place merge, rewrites the extent — same
  asymptotic cost the prior prototype already paid, now also producing a contiguous run).
- **Correctness + FR-7 are unambiguous:** hub 0 comes out globally ascending (`nondecreasing=t`) vs
  the baseline's insertion order (`f`); and BOTH FR-7 suites (atomicity incl. 200-iter randomized
  cross-store SM-5, and crash-recovery REDO + abort) show **zero divergence**. The contiguity
  relayout and the read-once scan did not break MVCC, atomicity, or crash recovery.

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

The contiguity follow-up's `gph_relayout_extent_contig` (migrate-on-grow) writes its NEW contiguous
pages under the SAME `≤GPH_MERGE_BATCH`-page GenericXLog batching, for the same reason, then
repoints `vr_adj_head`/`vr_adj_tail`/`vr_adj_cap` in a SEPARATE final GenericXLog record. **MVCC
correctness across abort/crash is preserved** because the relayout COPIES whole slots (MVCC option
A): each previously-committed edge keeps its original `es_xmin`, so it stays visible in the new run;
the edge being inserted carries the inserting xid, so on abort/crash-before-commit it is invisible
exactly as it would have been in the old layout. A post-abort scan of the vertex therefore sees the
identical visible set it had before the aborted insert — which is why `crash_recovery_test.sh`
scenario 2 (uncommitted) and the atomicity abort test both pass with zero divergence. The only
residue is the OLD chain pages, now orphaned dead space in the container relation (a future
compaction reclaims them) — a space cost, not a correctness one. As with the merge, the relayout of
a >3-page hub is not a SINGLE atomic WAL record; the per-page slot multiset is crash-invariant
across batch boundaries, so a crash mid-relayout (before the txn commits) leaves the old extent
authoritative (the repoint record had not committed) — consistent. A production design wanting the
grow itself a single atomic unit needs the shadow-extent swap / custom-rmgr discussion in §8.

---

## 8. Recommendation + proposed ADR-0002 addendum

### 8.1 Go / no-go — VERDICT FROM THE MEASURED x86 SPIKE (contiguity follow-up)

**Verdict: STILL-INCONCLUSIVE — leaning GO, gated on ONE remaining GX10 real-I/O number; do NOT
start the production migration (or compression #14 / WCOJ) until that number lands.** The prior run
reached a lean NO-GO because the prototype "could not deliver or measure the win" — it kept the
page-chain (no contiguity) and re-read a page per `Next()`. **This follow-up built BOTH missing
mechanisms and the win that was structurally absent before now appears in every measure this box
can take.** What is left is no longer a missing mechanism or an unproven correctness claim; it is a
single magnitude question (how big is the disk-seek component) that a warm x86 cache physically
cannot answer.

What this run PROVED (positive, real x86 numbers — see §7):

1. **Physical contiguity is real, including under interleaved load.** A real extent allocator
   (`gph_extend_pages_contig`, one relation extension of N consecutive blocks) + migrate-on-grow
   (`gph_relayout_extent_contig`) makes a vertex's extent a SEQUENTIAL block run. Proven decisively
   by the interleaved-load test: the page-chain baseline scatters each hub (`contiguous=f`,
   alternating block numbers) while the sorted-extent layout stays consecutive (`contiguous=t`).
   This is the prior run's GO-precondition (a) — DELIVERED.
2. **Read-once-per-page streaming scan.** Full deg-5000 hub scan drops from **5005** page reads
   (chain, re-read per neighbor) to **6** (one per extent page), STREAMING and LEAK-FREE on `LIMIT`.
   GO-precondition (c) — DELIVERED.
3. **The traversal win now appears in wall-clock too** (the thing that was entirely absent before):
   ~11% faster full-hub scan and ~15% faster `tjs`-shaped predicate under tiny `shared_buffers`,
   from issuing 6 vs 5005 `ReadBuffer` calls. This is the buffer/CPU component; the seek component
   is masked by the warm cache (the honest caveat, §7.1).
4. **Writeable.** Bulk load within noise of the append baseline (−1.0% this run), *including* the
   contiguous-relayout migrations on every hub grow. The "sorted/contiguous lists are expensive to
   write" objection stays empirically dead.
5. **Correct + MVCC-safe + FR-7 not regressed.** Output sorted (`nondecreasing=t`); whole-slot
   copies (MVCC option A) keep `es_xmin` attached, so abort/crash leave the identical visible set;
   `txn_atomicity_test.sh` (200-iter SM-5) and `crash_recovery_test.sh` (REDO + abort) both **zero
   divergence**; `make graph-test` green end-to-end.

What is STILL not proven (the single remaining gate):

6. **The disk-seek / readahead magnitude.** On this single x86 box the working set, though larger
   than the 512 kB buffer pool, is fully resident in the OS page cache, so the random-vs-sequential
   *seek* difference between the scattered chain and the contiguous extent is masked: the contiguity
   layout makes the touches consecutive block numbers, but the OS never seeks for them here. We
   therefore measured the page-touch COUNT win and the buffer/CPU wall-clock win, but NOT the seek
   win the design ultimately rests on. That needs real disk pressure (dropped caches / O_DIRECT, or
   a working set > RAM) — the GX10's NVMe + 128 GB regime — and is the ONE number that flips this to
   a hard GO.

Carried design cost (handled, but noted): **the hub maintenance merge AND the contiguous relayout
are not single atomic WAL records** (GenericXLog's 4-page cap; §7.2). The per-page slot multiset is
crash-invariant and MVCC visibility is preserved across batches (both FR-7 suites pass), and a
crash mid-relayout leaves the OLD extent authoritative — but a production design wanting the
grow/merge itself atomic as a unit needs a shadow-extent swap (golden rule 2 forbids a custom
rmgr). Plus the migrate-on-grow leaves orphaned old-chain pages (dead space) that a compaction pass
must reclaim — a space cost the production plan must own.

**Decision rule applied:**

- The prior run's NO-GO was reached **only because the prototype could not deliver or measure the
  win**. That reason is now gone: the win is delivered (contiguity + read-once scan) and measured
  to the limit this hardware allows. So the NO-GO branch no longer applies.
- We do NOT escalate to a hard GO, because a hard GO authorizes a HIGH-risk production migration and
  the *decisive* number — the seek win at residency pressure beyond RAM — is still unmeasured here.
  Calling GO now would be manufacturing the win from a warm-cache proxy. The honest status is
  **STILL-INCONCLUSIVE, leaning GO**: every gate except disk-I/O magnitude is cleared.
- Compression (#14) and WCOJ stay deferred until the GX10 real-I/O number confirms the wall-clock
  win scales with seek cost.

**To flip this to a hard GO, the GX10 re-run must (all of):**

- (a) ✅ DELIVERED here — physically-contiguous extent allocation (verified under interleaved load);
- (b) run on the **GX10 with a working set > RAM** (real residency pressure / cold cache / O_DIRECT),
  where the random-hop seek penalty is real — x86 with a warm OS cache is the wrong instrument for
  the seek magnitude;
- (c) ✅ DELIVERED here — read-once-per-page scan so the per-`Next()` re-read artifact does not swamp
  the signal (the §7 numbers are taken with it);
- (d) ✅ DELIVERED here — bulk-load within noise and both FR-7 suites at zero divergence;
- (e) settle the hub merge/relayout crash-atomicity question (§7.2) — shadow-extent swap vs accepting
  the multiset-preserving-but-not-unit-atomic relayout — and the orphan-page compaction.

Until (b) is measured, **the seek magnitude is unsubstantiated and the migration must not proceed —
but the structural blockers are resolved and the evidence now leans GO.**

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
| Contiguity follow-up: `gph_extend_pages_contig` + `gph_relayout_extent_contig` (real extent allocator + migrate-on-grow) | **BUILT + RUN** — branch `spike/009-contiguity`; contiguity proven under interleaved load (§7) |
| Read-once-per-page streaming scan (bounded per-page buffer, no pin across `Next()`, leak-free on `LIMIT`) | **BUILT + RUN** — full-hub page reads 5005→6 (§7) |
| §6/§7 benchmark + filled results table (incl. contiguity proof + tiny-`shared_buffers` residency run) | **DONE on x86** — `test/graph_layout_bench.sql` + `scripts/graph_layout_bench.sh` + `gph_adj_blocks()` probe, real numbers in §7. Caveat: warm-cache x86, seek win still GX10-gated |
| FR-7 atomicity + crash-recovery on spike branch | **PASS on x86** — `txn_atomicity_test.sh` + `crash_recovery_test.sh`, **zero divergence** |
| ADR-0002 addendum (§8.2) | **Drafted, NOT applied** — verdict is STILL-INCONCLUSIVE / leaning GO (GX10 real-I/O number outstanding) |
| Shipped layout on `master` (`gph_page.h`, `graph_am.c`) | **Unchanged** — the prototype lives only on the spike branch |

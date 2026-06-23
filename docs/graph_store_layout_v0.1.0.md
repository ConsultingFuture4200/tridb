# Graph Store On-Disk Layout — v0.1.0

**Issue:** DEV-1163
**Status:** Design complete; implementation GX10-gated (DEV-1164)
**Related:** DEV-1165 (traversal iterator), DEV-1166 (FR-7 txn atomicity test)

---

## 1. Scope and Anti-Requirements

This document specifies the on-disk layout and access-method contract for TriDB's native
adjacency-list graph store. It is a first-class PostgreSQL 13.4 access method (AM) — not a
schema of relational tables, not an external system.

**Anti-requirements (non-negotiable):**

- No relational join tables for graph topology. Every traversal step hits the adjacency-list
  AM pages directly; it never issues a SeqScan or IndexScan on a heap relation.
- No second WAL. All writes go through the shared PostgreSQL WAL (`XLogInsert`/`XLogFlush`).
  The graph AM registers its own REDO handler but uses the existing WAL infrastructure.
- No second transaction manager. The shared `TransactionManager` / MVCC machinery
  (snapshots, visibility, tuple versions) governs graph reads and writes identically to
  relational tuples.

---

## 2. Page Format — 32KB Blocks

MSVBASE is compiled with `--with-blocksize=32` (32 768-byte pages). All graph pages use
the standard `PageHeaderData` (24 bytes) so the buffer manager, WAL, and checkpointer treat
graph pages identically to heap and index pages.

### 2.1 Vertex Page Layout

A vertex page holds a **vertex directory** (slot array at the page tail, growing downward)
and **vertex records** (growing upward from the end of the page header). This mirrors
PostgreSQL's heap page layout so existing page utilities work without modification.

```
Offset    Size     Field
------    ------   -----
0         24       PageHeaderData  (pd_lsn, pd_checksum, pd_flags, pd_lower,
                                    pd_upper, pd_special, pd_pagesize_version)
24        2        gph_page_type   (0x0001 = VERTEX_PAGE)
26        2        gph_vertex_count
28        4        gph_reserved    (pad to 8-byte alignment; future use)
32        ...      Vertex records  (variable-length, packed upward)
...       ...      Free space
(pd_upper - N*4)  N*4  SlotDirectory: N entries of (uint16 offset, uint16 flags)
                         flags bit 0 = DEAD, bit 1 = HAS_PROPS_REF
32736     32       SpecialSpace    (reserved; holds AM OID + format version)
```

Total usable payload per vertex page: 32 768 − 24 (PageHeader) − 8 (gph header) − 32
(SpecialSpace) = **32 704 bytes**.

#### Vertex Record (VertexRecord)

```c
typedef struct VertexRecord {
    uint64  vr_vid;           /* vertex ID (monotone uint64, assigned by sequence) */
    uint32  vr_label_id;      /* label OID; 0 = unlabeled; v1 uses 1 = "entity"    */
    uint32  vr_prop_offset;   /* byte offset of co-located PropBlock within same page,
                                 0xFFFFFFFF = props on overflow page                 */
    uint32  vr_prop_length;   /* byte length of PropBlock (0 if no props)           */
    uint32  vr_adj_pageno;    /* page number of first adjacency page for this vertex */
    uint16  vr_adj_slot;      /* slot index within adj page (for O(1) landing)      */
    uint16  vr_flags;         /* bit 0 = DELETED (MVCC soft-delete marker)          */
    /* PropBlock follows immediately if vr_prop_offset points here */
} VertexRecord;
/* sizeof(VertexRecord) = 32 bytes */
```

`vr_vid` is assigned by a dedicated sequence (`graph_vid_seq`) backed by the shared
sequence manager. It is opaque to callers; the graph AM owns the keyspace.

### 2.2 Adjacency Page Layout

An adjacency page stores the **outgoing edge list** for one or more vertices. Each vertex's
outgoing edges occupy a contiguous EdgeSlot array within the page. When a vertex acquires
more edges than fit on one page, a chain of adjacency pages is threaded via `gph_next_pageno`.

```
Offset    Size     Field
------    ------   -----
0         24       PageHeaderData
24        2        gph_page_type   (0x0002 = ADJ_PAGE)
26        2        gph_edge_count  (total EdgeSlots on this page)
28        4        gph_next_pageno (0 = no overflow; follows adjacency chain)
32        8        gph_owner_vid   (uint64; vertex this page was first allocated for; informational)
40        4        gph_reserved    (pad to 8-byte alignment; future use)
44        ...      EdgeSlot array, packed
...       ...      EdgePropBlock area (co-located edge properties, packed after EdgeSlots)
32736     32       SpecialSpace
```

Usable payload: 32 768 − 24 (PageHeader) − 2 (gph_page_type) − 2 (gph_edge_count) −
4 (gph_next_pageno) − 8 (gph_owner_vid) − 4 (gph_reserved) − 32 (SpecialSpace) =
**32 692 bytes**.

#### EdgeSlot

```c
typedef struct EdgeSlot {
    uint64  es_src_vid;       /* source vertex ID                              */
    uint64  es_dst_vid;       /* destination vertex ID                         */
    uint32  es_edge_type_id;  /* edge type OID; v1: 1 = "related_to" only
                                 SEAM #3: additional values reserved for typed
                                 edge support in a future minor version         */
    uint32  es_prop_offset;   /* byte offset of EdgePropBlock within this page,
                                 0xFFFFFFFF = overflow page                     */
    uint32  es_prop_length;   /* byte length of EdgePropBlock (0 if no props)  */
    uint32  es_flags;         /* bit 0 = DELETED; bit 1 = REVERSED (back-edge
                                 present in DST's adj list)                     */
} EdgeSlot;
/* sizeof(EdgeSlot) = 32 bytes */
```

Capacity: 32 692 / 32 = **1 021 EdgeSlots per adjacency page** (before property storage).
In practice, property blocks reduce this; the AM splits pages once `gph_edge_count` would
exceed the threshold leaving < 256 bytes of free space.

### 2.3 Property Storage — Co-location Decision

**Decision: properties are co-located in graph pages** (vertex properties in vertex pages,
edge properties in adjacency pages), not stored as relational tuples in a heap relation.

Rationale:

1. **Cache locality.** The traversal iterator (Open/Next/Close; see Section 5) fetches a
   page to advance along an adjacency list. If property data for filtering (`WHERE timestamp
   IN :range`) lives on the same or immediately adjacent page, no second buffer-pool lookup
   is required. Relational storage would force a heap fetch per qualifying edge — eliminating
   the intermediate-result reduction that SM-1 depends on.
2. **No cross-subsystem MVCC impedance.** Co-located properties are written in the same
   WAL record as the edge; their visibility epoch is identical. Referencing a relational
   tuple introduces a second ctid lookup with its own snapshot check.
3. **Simplicity for v1.** With a single edge type (`related_to`) and a small property set
   (`timestamp`, `weight`), overflow is rare. The overflow-page escape hatch (`0xFFFFFFFF`
   sentinel) handles the exceptional case without complicating the common path.

**Overflow:** If `vr_prop_length` or `es_prop_length` exceeds the remaining free space on
the owning page, the property blob is written to a dedicated `PROP_OVERFLOW_PAGE`
(page type `0x0003`). When the `_prop_offset` field contains the sentinel `0xFFFFFFFF`,
the `_prop_length` field is repurposed to hold the overflow page number (`BlockNumber`),
and the byte offset within that page is zero (the PROP_OVERFLOW_PAGE stores exactly one
blob per page, beginning immediately after its own 32-byte header). The AM checks the
sentinel first; if set, it reads `_prop_length` as a `BlockNumber` and ignores the offset.
Note: the `_prop_offset` and `_prop_length` fields are each `uint32`; there is no `uint64`
packing. The previous encoding description (page × 2^32 | slot) was incorrect and is
withdrawn.

### 2.4 PropBlock Wire Format

```
Offset   Size    Field
------   ------  -----
0        2       pb_num_attrs
2        N*6     AttrDirectory: N × (uint16 attr_id, uint32 value_offset)
N*6+2    ...     Value bytes (variable; UTF-8 text, int64 LE, float64 LE)
```

`attr_id` is the OID of a graph attribute registered in `pg_graph_attribute` (a catalog
table the AM creates at `CREATE EXTENSION tridb`). This keeps PropBlock self-describing
without embedding type names inline.

---

## 3. Vertex + Edge ID Allocation

Vertex IDs are `uint64` values from `graph_vid_seq`. They are dense within a single graph
instance but not globally contiguous (gaps arise from rolled-back INSERTs). Edge identity
is the pair `(es_src_vid, es_dst_vid, es_edge_type_id)` — no separate edge sequence. The
AM enforces uniqueness via a B-tree index over that triple (Section 4.2).

---

## 4. Secondary B-tree Indexes

The native graph pages are optimized for traversal (vid → adjacency page → edge list). Two
B-tree secondary indexes support attribute-based access and the canonical query's
`WHERE timestamp IN :range` filter.

### 4.1 Vertex Attribute Index (`graph_vertex_attr_idx`)

Built on a synthetic index relation whose tuples are `(label_id, attr_id, attr_value_bytes,
vid)`. Each PropBlock attribute emits one index entry. The index uses the standard
`btree` AM on top of the graph storage layer via a thin adapter that reads `pg_graph_attribute`
for type information.

### 4.2 Edge Attribute Index (`graph_edge_attr_idx`)

Tuples: `(edge_type_id, attr_id, attr_value_bytes, src_vid, dst_vid)`.

For v1, the `timestamp` attribute is indexed here. The canonical query's time-range filter
resolves to an IndexScan on `graph_edge_attr_idx` returning `(src_vid, dst_vid)` pairs,
which the traversal iterator then uses as seeds — avoiding a full adjacency-list scan when
the timestamp filter is selective.

### 4.3 Edge Identity Uniqueness Index (`graph_edge_unique_idx`)

Tuples: `(src_vid, dst_vid, edge_type_id)`. Unique constraint. Used by `INSERT` to detect
duplicate edges in O(log n).

### 4.4 WAL Integration for Indexes

Index pages use `XLogInsert` with resource manager ID `RM_GRAPH_BTREE_ID` (registered at
AM init). REDO handler replays index page splits and tuple insertions using the same
`GenericXLogFinish` path as core btree where possible, diverging only for the
`graph_edge_unique_idx` uniqueness check.

---

## 5. Transaction Manager Integration — No Second WAL

All writes to graph pages call `GenericXLogStart` / `GenericXLogRegisterBuffer` /
`GenericXLogFinish` (or their direct `XLogInsert` equivalents for custom REDO). This places
graph WAL records in the same WAL stream as heap and index records. Recovery replays them
in LSN order, maintaining consistency across all three stores (relational, vector, graph)
without a separate log.

**MVCC visibility:** Each `VertexRecord` and `EdgeSlot` carries an implicit `xmin`/`xmax`
pair stored in the page's item identifier flags (reusing `ItemIdData` conventions). The
existing `HeapTupleSatisfiesVisibility` machinery is **not** called directly on graph
tuples; instead, the graph AM implements `GraphTupleSatisfiesSnapshot(snapshot, flags_word)`
which checks the same `TransactionIdPrecedes` / `XidInMVCCSnapshot` predicates using the
caller's snapshot. This keeps one txn manager, one snapshot, full MVCC.

**Locking:** The AM uses PostgreSQL's `LockBuffer` (shared for reads, exclusive for
writes). No graph-specific lock table is introduced. Deadlock detection is handled by the
existing `DeadLockCheck`.

---

## 6. Open/Next/Close Traversal-Iterator Contract (DEV-1165)

The graph traversal iterator is a Volcano-model iterator. It satisfies TR-1: no blocking
operators; early termination at any `Close` call releases all held buffer pins immediately.

### 6.1 Iterator State

```c
typedef struct GraphScanState {
    /* Volcano base */
    PlanState   ps;               /* must be first */

    /* Seed */
    uint64      gss_src_vid;      /* vertex to traverse from */
    uint32      gss_edge_type;    /* edge type filter; 0 = all */
    Snapshot    gss_snapshot;     /* caller's snapshot */

    /* Cursor */
    Buffer      gss_cur_buf;      /* pinned adjacency page; InvalidBuffer = exhausted */
    uint16      gss_cur_slot;     /* next EdgeSlot index to read on gss_cur_buf */
    uint32      gss_next_pageno;  /* gph_next_pageno for chain advance */

    /* Projection */
    TupleTableSlot *gss_slot;

    /* Early-termination flag */
    bool        gss_done;
} GraphScanState;
```

### 6.2 Open

```
GraphOpen(GraphScanState *state, uint64 src_vid, Snapshot snapshot):
    1. Resolve src_vid → VertexRecord via vertex page lookup:
         vpage = ReadBuffer(vertex_pageno_for(src_vid))
         LockBuffer(vpage, BUFFER_LOCK_SHARE)
         Read vr_adj_pageno, vr_adj_slot from VertexRecord.
         LockBuffer(vpage, BUFFER_LOCK_UNLOCK)
         ReleaseBuffer(vpage)          /* vertex page pin released immediately */
    2. Pin the first adjacency page: gss_cur_buf = ReadBuffer(vr_adj_pageno).
    3. Set gss_cur_slot = vr_adj_slot; gss_next_pageno from gph_next_pageno on that page.
    4. Return; no tuple emitted.
```

### 6.3 Next

```
GraphNext(GraphScanState *state) -> TupleTableSlot* | NULL:
    loop:
        if gss_done: return NULL
        if gss_cur_slot >= gph_edge_count on current page:
            if gss_next_pageno == 0:
                gss_done = true; UnpinBuffer(gss_cur_buf); return NULL
            else:
                UnpinBuffer(gss_cur_buf)
                gss_cur_buf = ReadBuffer(gss_next_pageno)
                gss_cur_slot = 0
                continue
        slot = EdgeSlot[gss_cur_slot++] on gss_cur_buf
        if slot.es_flags & DELETED: continue
        if gss_edge_type != 0 and slot.es_edge_type_id != gss_edge_type: continue
        if not GraphTupleSatisfiesSnapshot(state->gss_snapshot, slot.es_flags): continue
        project slot fields into gss_slot
        return gss_slot
```

**Early termination:** the caller (e.g., the TJS operator or a LIMIT node) calls `Close`
at any point. `Close` releases `gss_cur_buf` immediately; no further pages are read.

### 6.4 Close

```
GraphClose(GraphScanState *state):
    if BufferIsValid(gss_cur_buf): UnpinBuffer(gss_cur_buf)
    gss_done = true
```

### 6.5 Interaction with the HNSW Iterator (DEV-1168)

The canonical query drives the graph iterator with `src_vid` values produced by the HNSW
relaxed-monotonicity iterator. Those arrive one at a time via a pipelined `Next` call on the
vector scan node; there is no materialisation of the full HNSW result set before graph
traversal begins. This preserves TR-1 end-to-end.

---

## 7. Typed-Edge Seam — Spec Marker #3

v1 supports exactly one edge type: `related_to` (`es_edge_type_id = 1`). The seam for
future typed edges is:

- `es_edge_type_id` is already a `uint32` — the field is present and populated in every
  EdgeSlot; `GraphNext` already filters on it.
- `pg_graph_edge_type` catalog table (created at `CREATE EXTENSION tridb`) holds `(typeid
  uint32, typname name, reverse_typid uint32)`. v1 inserts one row: `(1, 'related_to', 0)`.
- Adding a new edge type is a catalog INSERT + no page format change. Typed edge predicates
  in `MATCH` patterns map to `gss_edge_type` filter values at plan time.
- **No schema migration required** to add edge types after v1 ships; the format is forward-
  compatible.

---

## 8. FR-7 Transaction Atomicity Test Plan (DEV-1166)

FR-7: Graph writes and relational/vector writes within the same transaction must be atomic —
either all commit or all abort, with no partial visibility.

### 8.1 Test T1 — Commit Atomicity

```sql
BEGIN;
  INSERT INTO graph_vertices (label, props) VALUES ('entity', '{"name":"A"}');
  -- simultaneously insert a relational tuple
  INSERT INTO chunks (id, text) VALUES ('c1', 'hello world');
COMMIT;
-- Verify: both visible, neither missing
SELECT count(*) FROM graph_vertices WHERE props->>'name' = 'A';  -- expect 1
SELECT count(*) FROM chunks WHERE id = 'c1';                     -- expect 1
```

### 8.2 Test T2 — Abort Atomicity

```sql
BEGIN;
  INSERT INTO graph_vertices (label, props) VALUES ('entity', '{"name":"B"}');
  INSERT INTO chunks (id, text) VALUES ('c2', 'abort me');
ROLLBACK;
-- Verify: neither visible
SELECT count(*) FROM graph_vertices WHERE props->>'name' = 'B';  -- expect 0
SELECT count(*) FROM chunks WHERE id = 'c2';                     -- expect 0
```

### 8.3 Test T3 — Crash Recovery

1. Begin a transaction, insert a vertex and a relational row, flush WAL buffers
   (`pg_switch_wal()`), then kill the postmaster with `SIGKILL` before `COMMIT`.
2. Restart PostgreSQL; run recovery.
3. Assert neither the graph vertex nor the relational row is visible. WAL replay must not
   leave a partial committed state.

Implementation: test harness in `tests/fr7_atomicity/` (populated at DEV-1166). Runs on
GX10 against the live MSVBASE fork.

### 8.4 Test T4 — Concurrent Isolation

Two sessions concurrently insert overlapping edges. Verify that the uniqueness index
(`graph_edge_unique_idx`) serializes them correctly: exactly one succeeds, the other gets a
duplicate-key error or serialization failure. No lost-update anomaly.

### 8.5 Test T5 — MVCC Snapshot Isolation

Session A opens a long-running read transaction (acquires snapshot). Session B inserts a new
vertex and commits. Session A must not see session B's vertex (`GraphTupleSatisfiesSnapshot`
returns false for xmin > A's snapshot horizon).

---

## 9. Page-Count Estimates (Orientation)

At 1M vertices with average 10 properties at 32 bytes each:
- Vertex pages: 1M × 32 bytes (VertexRecord) + 1M × 320 bytes (PropBlock avg) ≈ 352 MB
  → ~11 000 vertex pages
- Adjacency pages at 10 edges/vertex: 10M × 32 bytes (EdgeSlot) ÷ 32 692 bytes/page
  ≈ 9 790 adjacency pages

At 32KB pages these fit comfortably within the GX10's 128GB. The buffer pool hit rate for
the canonical query's k=5 traversal (touching O(50) adjacency pages) will be high once
the working set is warm.

---

## 10. File Locations (Implementation Targets — DEV-1164)

| File | Purpose |
|------|---------|
| `src/graph_store/gph_page.h` | Page format constants and struct definitions |
| `src/graph_store/gph_am.c` | Table AM handler (`graph_am_handler`) |
| `src/graph_store/gph_scan.c` | Open/Next/Close iterator implementation |
| `src/graph_store/gph_insert.c` | Vertex and edge INSERT path + WAL records |
| `src/graph_store/gph_index.c` | B-tree adapter for attribute and uniqueness indexes |
| `src/graph_store/gph_redo.c` | WAL REDO handler registration |
| `src/catalog/pg_graph_edge_type.h` | Edge-type catalog (`es_edge_type_id` keyspace) |
| `tests/fr7_atomicity/` | FR-7 test suite (DEV-1166) |

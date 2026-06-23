# ADR-0002 — Adjacency-List Graph Store: Co-location and 32KB Page Layout

**Date:** 2026-06-23
**Status:** Accepted
**Issue:** DEV-1163
**Authors:** PostgresDBA agent
**Supersedes:** none
**Superseded by:** none

---

## Context

TriDB requires a native graph store for topology traversal inside the PostgreSQL process.
The system must satisfy:

- TR-1: Volcano-model iterator with early termination — no blocking operators.
- One transaction manager / one WAL — no second log, no cross-system XA.
- No relational join tables for graph topology (anti-requirement, non-negotiable).
- 32KB block size (MSVBASE compiled with `--with-blocksize=32`).
- Traversal of `(src:entity)-[:related_to]->(dst:entity)` with `WHERE timestamp IN range`
  and `ORDER BY src_embedding <-> :question_embedding LIMIT 5` (canonical query, v1).

Two decisions required independent rationale:

1. **Where to store vertex and edge properties** (co-located in graph pages vs. heap relation).
2. **How to lay out adjacency data within a 32KB block**.

---

## Decision 1 — Property Co-location in Graph Pages

Properties (vertex attributes such as `embedding`, `name`; edge attributes such as
`timestamp`, `weight`) are stored **co-located** in the same graph AM pages as the structural
records (`VertexRecord`, `EdgeSlot`), not in a separate heap relation.

### Rationale

**Cache locality is the primary argument.** The canonical query's `WHERE timestamp IN
:range` filter must be evaluated per edge during traversal. If property data lived in a
heap relation, each qualifying `EdgeSlot` would require a second buffer-pool lookup (a heap
`ctid` fetch) before the filter could be applied. At k=5 with a graph fan-out of O(10), that
doubles the buffer reads on the hot path and defeats SM-1 (>=5x intermediate-result
reduction).

**MVCC consistency is the secondary argument.** A property stored in the same page as its
owning `EdgeSlot` shares the same WAL record and the same `xmin`/`xmax` visibility epoch.
A property stored in a heap relation would require a separate snapshot check, introducing
a window where the edge is visible but its property is not (or vice versa) during
concurrent writes. Co-location eliminates this impedance entirely.

**Simplicity for v1.** With one edge type and a small fixed property set, overflow is
exceptional. An overflow sentinel (`0xFFFFFFFF` in the 32-bit offset field) handles large
properties without complicating the common path.

### Rejected Alternative — Properties in a Heap Relation

Storing properties as columns in a relational table (e.g., `graph_edge_props(src_vid,
dst_vid, timestamp, weight)`) would allow using existing PG statistics, autovacuum, and
planner machinery. It was rejected because:

- It reintroduces a relational join on the hot traversal path — exactly what the
  adjacency-list AM exists to avoid.
- It creates a two-phase commit problem within a single PG process: the edge write and the
  property write land in different heap pages. Under MSVBASE's transaction manager they
  stay atomic, but recovery ordering becomes subtler with no benefit.

---

## Decision 2 — 32KB Page Layout with Slot Directory

Graph pages use the standard `PageHeaderData` (24 bytes) and a slot directory growing
downward from the page tail (mirroring PG heap pages), with records growing upward.
`SpecialSpace` is 32 bytes at the page end (inside the 32KB block), used for AM OID and
format version.

### Key sizing outcomes

| Struct | Size | Max structural records/page (no co-located props) |
|--------|------|---------------------------------------------------|
| `VertexRecord` | 32 bytes | ~1 022 per vertex page (reduced in practice by co-located PropBlocks) |
| `EdgeSlot` | 32 bytes | ~1 021 per adjacency page (reduced in practice by co-located EdgePropBlocks) |

At 32 bytes each, both structs are aligned to cache-line boundaries (64-byte lines on
ARM64/GX10). The 1 022 figure is the structural ceiling with zero co-located property
storage; real capacity is lower once PropBlocks are packed onto the same page. For the
canonical query's knowledge-graph fan-out (10–100 neighbors per vertex), a single
adjacency page holds one vertex's complete edge list in the common case even after
property storage is accounted for.

### Rationale

- **Alignment with existing PG buffer manager.** Using `PageHeaderData` means `ReadBuffer`,
  `LockBuffer`, `MarkBufferDirty`, and `GenericXLogFinish` all work without modification.
  The graph AM does not need a custom I/O path.
- **32KB is non-negotiable.** It is baked into the MSVBASE fork at build time. All layouts
  must fit within 32 768 bytes. The 1 022-slot capacity is calculated for exactly this
  block size; layouts assuming 8KB standard pages would overflow.
- **Slot directory enables O(1) vertex lookup within a page.** Given a slot index (stored
  in `VertexRecord.vr_adj_slot`), the adjacency page landing point is a two-byte read from
  the slot directory — no linear scan of the page.

---

## Decision 3 — Typed-Edge Seam (Spec Marker #3)

v1 ships with one edge type (`related_to`, `es_edge_type_id = 1`). The `uint32` type field
in `EdgeSlot` and the `pg_graph_edge_type` catalog table constitute the seam. Adding edge
types after v1 requires a catalog row insert only — no page format change, no migration.

---

## Consequences

**Positive:**
- Single buffer pin per traversal step; no cross-subsystem heap fetch on the hot path.
- Full MVCC coverage of graph writes via the shared WAL and snapshot machinery.
- Typed-edge extensibility without format churn.
- FR-7 atomicity (graph + relational writes in one transaction) is structurally guaranteed,
  not bolted on.

**Negative / accepted trade-offs:**
- Property overflow pages add implementation complexity for large attribute blobs.
  Accepted: uncommon in v1; overflow sentinel handles it without touching the common path.
- Co-located properties are not directly queryable by the PG planner's statistics or
  autovacuum. Accepted: v1 property access goes through the graph AM's B-tree attribute
  indexes (`graph_vertex_attr_idx`, `graph_edge_attr_idx`), not heap scanners.

---

## References

- `docs/graph_store_layout_v0.1.0.md` — full byte-offset spec, iterator contract, FR-7
  test plan
- DEV-1164 — adjacency-list access method implementation (GX10-gated)
- DEV-1165 — graph traversal iterator (GX10-gated)
- DEV-1166 — FR-7 txn atomicity verification (GX10-gated)

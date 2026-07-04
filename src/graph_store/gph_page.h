/*
 * gph_page.h — On-disk page format for TriDB's native adjacency-list graph store (DEV-1164).
 *
 * Implements the 32KB page layout from docs/graph_store_layout_v0.1.0.md §2, adapted for the
 * v1 CORE increment: fixed-size records (no per-record property co-location yet), packed via
 * standard PostgreSQL page mechanics (PageInit + a special area + pd_lower-tracked records) so
 * the SHARED buffer manager, the SHARED WAL (GenericXLog), the checkpointer, and page checksums
 * treat graph pages identically to heap/index pages. No private buffer pool, no second WAL.
 *
 * Scope vs the full DEV-1163 spec (deferred to follow-ups, documented in the AM source):
 *   - property co-location / PropBlocks / overflow pages   (§2.3-2.4)
 *   - secondary B-tree indexes (vertex/edge attr, unique)  (§4)
 *   - per-tuple xmin/xmax MVCC                              (§5)  -- v1 uses txn-level + GenericXLog
 *   - custom rmgr REDO handler                             (§4.4) -- v1 uses GenericXLog's generic REDO
 *
 * Built ONLY inside the MSVBASE fork (PG 13.4, --with-blocksize=32 => BLCKSZ 32768); the
 * static asserts below fail the build if that does not hold.
 */
#ifndef TRIDB_GRAPH_STORE_GPH_PAGE_H
#define TRIDB_GRAPH_STORE_GPH_PAGE_H

#include "postgres.h"
#include "storage/bufpage.h"
#include "storage/block.h"

/* The fork must be built with 32KB pages (docs/graph_store_layout_v0.1.0.md §2). */
StaticAssertDecl(BLCKSZ == 32768, "graph store requires --with-blocksize=32 (BLCKSZ 32768)");

#define GPH_MAGIC        0x47504831   /* "GPH1" */
#define GPH_VERSION      1
#define GPH_META_BLKNO   0            /* block 0 is always the metapage */
#define GPH_EDGE_TYPE_RELATED_TO  1  /* v1: the single edge label :related_to */

/* Page types (stored in the special area). */
#define GPH_PAGE_META    0x0000
#define GPH_PAGE_VERTEX  0x0001
#define GPH_PAGE_ADJ     0x0002

/* Record flag bits. */
#define GPH_FLAG_DELETED 0x0001       /* tombstone (set by gph_tombstone_edge/vertex, plan 037) */

/*
 * Per-page special area. Lives in pd_special; identifies the page type and carries the
 * chain pointer (vertex-page chain, or a vertex's adjacency-page chain) and, for adjacency
 * pages, the owning vertex id.
 */
typedef struct GphPageSpecial
{
	uint16		gph_page_type;	/* GPH_PAGE_* */
	uint16		gph_unused;		/* pad to 4-byte boundary */
	BlockNumber	gph_next_pageno;/* next page in this chain; InvalidBlockNumber = end */
	uint64		gph_owner_vid;	/* adjacency pages: owning vertex; else 0 */
} GphPageSpecial;

/* Metapage payload (block 0 main area). One per graph store. */
typedef struct GphMeta
{
	uint32		gm_magic;		/* GPH_MAGIC */
	uint32		gm_version;		/* GPH_VERSION */
	uint64		gm_next_vid;	/* next vertex id to assign (dense, monotone) */
	uint32		gm_vertex_count;
	TransactionId gm_frozen_horizon;	/* highest completed gph_freeze() horizon; 0 (== Invalid)
										 * = never frozen. Repurposes the former uint32 gm_reserved
										 * slot — TransactionId is uint32 on PG 13, so NO page-layout
										 * change and no GPH_VERSION bump; old stores read as 0
										 * (advisor plan 036 / DEV-1347). */
	uint64		gm_edge_count;	/* store-wide directed-edge count (FR-6 avg_out_degree source) */
	BlockNumber	gm_first_vertex_blk;	/* head of the vertex-page chain (Invalid if none) */
	BlockNumber	gm_last_vertex_blk;		/* tail of the vertex-page chain (append target) */
} GphMeta;

/* gm_frozen_horizon must reuse gm_reserved's slot with NO layout change (advisor 036). */
StaticAssertDecl(sizeof(GphMeta) == 40,
				 "GphMeta size must be unchanged (gm_frozen_horizon repurposes gm_reserved)");

/*
 * Vertex record. Dense uint64 vid; points at the head/tail of this vertex's adjacency-page
 * chain (tail kept for O(1) edge append). 32 bytes (matches the spec's VertexRecord size;
 * property fields are reserved in v1 core).
 */
typedef struct GphVertexRecord
{
	uint64		vr_vid;			/* dense vertex id */
	uint32		vr_label_id;	/* 1 = entity (v1) */
	uint32		vr_flags;		/* GPH_FLAG_* */
	BlockNumber	vr_adj_head;	/* first adjacency page; InvalidBlockNumber = no edges */
	BlockNumber	vr_adj_tail;	/* last adjacency page (append target) */
	TransactionId vr_xmin;		/* inserting xid (MVCC visibility; abort => invisible) */
	TransactionId vr_xmax;		/* deleting xid (plan 037): the tombstone is honored only when
								 * GPH_FLAG_DELETED is set AND this xid is visible, so a delete
								 * from an aborted/in-progress txn is ignored — same size (uint32,
								 * was vr_pad), keeps the record at 32 bytes, no layout change */
} GphVertexRecord;

/*
 * Edge slot. One directed :related_to edge. 32 bytes (matches the spec's EdgeSlot).
 */
typedef struct GphEdgeSlot
{
	uint64		es_src_vid;
	uint64		es_dst_vid;
	uint32		es_edge_type_id;	/* GPH_EDGE_TYPE_RELATED_TO in v1 */
	uint32		es_flags;			/* GPH_FLAG_* */
	TransactionId es_xmin;			/* inserting xid (MVCC visibility; abort => invisible) */
	TransactionId es_xmax;			/* deleting xid (plan 037): the tombstone is honored only when
									 * GPH_FLAG_DELETED is set AND this xid is visible, so a delete
									 * from an aborted/in-progress txn is ignored — same size (uint32,
									 * was es_pad), keeps the slot at 32 bytes, no layout change */
} GphEdgeSlot;

StaticAssertDecl(sizeof(GphVertexRecord) == 32, "GphVertexRecord must be 32 bytes");
StaticAssertDecl(sizeof(GphEdgeSlot) == 32, "GphEdgeSlot must be 32 bytes");

/* Size of the special area, MAXALIGNed as the page machinery expects. */
#define GPH_SPECIAL_SIZE  MAXALIGN(sizeof(GphPageSpecial))

/* First usable byte of a graph page's record area (right after the page header). */
#define GphPageRecordBase(page)  ((char *) (page) + SizeOfPageHeaderData)

/* The special area. */
#define GphPageSpecialPtr(page)  ((GphPageSpecial *) PageGetSpecialPointer(page))

/*
 * Records are packed fixed-size from the record base up to pd_lower (which we advance as we
 * append), so the live record count is derivable from pd_lower — no redundant counter to
 * keep in sync. Free slots remain between pd_lower and pd_special.
 */
static inline uint32
GphPageRecordCount(Page page, Size record_size)
{
	uint32 lower = ((PageHeader) page)->pd_lower;
	if (lower <= SizeOfPageHeaderData)
		return 0;
	return (lower - SizeOfPageHeaderData) / record_size;
}

static inline bool
GphPageHasRoom(Page page, Size record_size)
{
	PageHeader p = (PageHeader) page;
	return (uint32) p->pd_lower + record_size <= (uint32) p->pd_upper;
}

/* Append one fixed-size record at the end of the packed area; advances pd_lower. */
static inline void
GphPageAppendRecord(Page page, const void *record, Size record_size)
{
	PageHeader p = (PageHeader) page;
	char	  *dst = (char *) page + p->pd_lower;

	memcpy(dst, record, record_size);
	p->pd_lower += record_size;
}

/* Pointer to record i (0-based) in the packed area. */
static inline void *
GphPageGetRecord(Page page, uint32 i, Size record_size)
{
	return (void *) (GphPageRecordBase(page) + (Size) i * record_size);
}

/*
 * Capacity, in EdgeSlots, of one adjacency page — the upper bound on the number of slots a single
 * page can hold given the host packing geometry (page header + special area subtracted). Used by
 * the read-once traversal scan to size its per-page in-memory slot buffer. With BLCKSZ 32768,
 * header 24, special 32 => 32712/32 = 1022 slots/page. Pure geometry arithmetic; carries no
 * layout assumption (no sorted run / delta tail — the chained page format is unchanged).
 */
static inline uint32
GphEdgeSlotsPerPage(void)
{
	return (uint32) ((BLCKSZ - SizeOfPageHeaderData - GPH_SPECIAL_SIZE) / sizeof(GphEdgeSlot));
}

#endif							/* TRIDB_GRAPH_STORE_GPH_PAGE_H */

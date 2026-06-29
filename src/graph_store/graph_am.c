/*
 * graph_am.c — TriDB native adjacency-list graph store, v1 CORE (DEV-1164).
 *
 * Stores graph topology in native 32KB pages (gph_page.h, per docs/graph_store_layout_v0.1.0.md)
 * managed DIRECTLY through PostgreSQL's SHARED buffer manager (ReadBufferExtended) and SHARED
 * WAL (GenericXLog). No private buffer pool, no second WAL, no second transaction manager:
 * every mutation is WAL-logged in the host stream, so graph writes are crash-safe and commit/
 * abort atomically alongside relational + vector writes (this is the FR-7 substrate, DEV-1166).
 *
 * The pages live in the blocks of a plain container relation `graph_store.gstore` (autovacuum
 * disabled; never accessed as a heap). Using a real relation's storage gives us the buffer
 * manager, the WAL, the checkpointer, and the relfilenode lifecycle for free — which is exactly
 * the "use Postgres's shared buffer manager / write through the existing WAL" requirement.
 *
 * Block 0 is the metapage (next vid + vertex-page-chain head/tail). Vertex pages hold
 * GphVertexRecords (dense vid -> adjacency-chain head/tail). Adjacency pages hold packed
 * GphEdgeSlots, chained when a vertex's edge list overflows a page. Traversal (gph_neighbors)
 * reads edge slots ONE per Next() from one pinned page at a time, so a LIMIT above it stops
 * before later chain pages are ever read (TR-1 storage-level early termination).
 *
 * DEFERRED to follow-ups (documented in gph_page.h): property co-location, secondary B-tree
 * indexes, per-tuple xmin/xmax MVCC, a custom rmgr REDO handler, and the formal
 * `CREATE ACCESS METHOD ... HANDLER` TableAmRoutine vtable. v1 uses GenericXLog's generic REDO
 * and transaction-level atomicity, which satisfies the DEV-1164 acceptance criteria.
 *
 * CONCURRENCY CONTRACT (v1 core): physical page allocation is race-safe (extends hold the
 * relation extension lock). The LOGICAL graph structure assumes a SINGLE WRITER — the Phase-0
 * seed loader is single-connection. Concurrent writers appending to the SAME vertex can still
 * lose an adjacency update; concurrent multi-writer isolation (and the per-tuple snapshot
 * machinery it needs) is DEV-1166, not this issue. Reads are always MVCC-visibility-filtered.
 */
#include "postgres.h"

#include "access/generic_xlog.h"
#include "access/relation.h"
#include "access/transam.h"
#include "access/xact.h"
#include "catalog/namespace.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "storage/bufmgr.h"
#include "storage/lmgr.h"
#include "utils/builtins.h"
#include "utils/rel.h"

#include "gph_page.h"
#include "graphstore.h"

PG_MODULE_MAGIC;

#define GPH_SCHEMA  "graph_store"
#define GPH_RELNAME "gstore"

/*
 * Minimal MVCC visibility for a graph record's inserting xid. PostgreSQL has no undo: an
 * aborted INSERT leaves its bytes on the page, so reads MUST filter by xmin. A record is
 * visible iff it was inserted by the current transaction (our own uncommitted write) or by a
 * transaction that committed. In-progress (other txn) and aborted xids are invisible — which
 * is exactly what makes graph writes roll back atomically on ABORT and after a crash (FR-7).
 *
 * v1 scope: this is commit/abort + crash visibility, NOT full cross-session snapshot isolation
 * (the concurrent-isolation case is DEV-1166 / the deferred per-tuple xmin/xmax §5 work).
 */
static inline bool
gph_xmin_visible(TransactionId xmin)
{
	if (!TransactionIdIsValid(xmin))
		return false;
	if (TransactionIdIsCurrentTransactionId(xmin))
		return true;
	return TransactionIdDidCommit(xmin);
}

/*
 * Per-backend traversal-step counter (one increment per edge EMITTED by gph_neighbors).
 * Demonstrates that pulling K of N neighbors under LIMIT K does ~K units of work, not N —
 * the TR-1 early-termination probe (mirrors the v0 graph_visits()). Backend-local and
 * monotonic for the life of the backend: read DELTAS (v1 - v0), never the absolute value.
 */
static int64 gph_visit_counter = 0;

/* ------------------------------------------------------------------ */
/* Relation + metapage helpers                                         */
/* ------------------------------------------------------------------ */

/* Open the container relation by qualified name. */
static Relation
gph_open_store(LOCKMODE lockmode)
{
	RangeVar   *rv = makeRangeVar(GPH_SCHEMA, GPH_RELNAME, -1);
	Oid			relid = RangeVarGetRelid(rv, lockmode, false);

	return relation_open(relid, NoLock);	/* RangeVarGetRelid already took lockmode */
}

/*
 * Allocate a fresh block at the end of the relation and return it pinned + EXCLUSIVE-locked.
 * Holds the relation extension lock across the extend so two backends can't grab the same new
 * block (physical-corruption safety, independent of the single-writer logical contract).
 */
static Buffer
gph_extend_page(Relation rel)
{
	Buffer		buf;

	LockRelationForExtension(rel, ExclusiveLock);
	buf = ReadBufferExtended(rel, MAIN_FORKNUM, P_NEW, RBM_NORMAL, NULL);
	LockBuffer(buf, BUFFER_LOCK_EXCLUSIVE);
	UnlockRelationForExtension(rel, ExclusiveLock);
	return buf;
}

/*
 * Ensure the metapage (block 0) exists and is initialized. Extends the relation by one block
 * on first use. Caller holds a write lock on the relation.
 */
static void
gph_ensure_meta(Relation rel)
{
	Buffer		buf;
	Page		page;
	GenericXLogState *state;
	GphMeta    *meta;
	GphPageSpecial *special;

	if (RelationGetNumberOfBlocks(rel) > 0)
		return;

	/* Double-checked under the extension lock: only one backend bootstraps block 0. */
	LockRelationForExtension(rel, ExclusiveLock);
	if (RelationGetNumberOfBlocks(rel) > 0)
	{
		UnlockRelationForExtension(rel, ExclusiveLock);
		return;
	}
	buf = ReadBufferExtended(rel, MAIN_FORKNUM, P_NEW, RBM_NORMAL, NULL);
	Assert(BufferGetBlockNumber(buf) == GPH_META_BLKNO);
	LockBuffer(buf, BUFFER_LOCK_EXCLUSIVE);
	UnlockRelationForExtension(rel, ExclusiveLock);

	state = GenericXLogStart(rel);
	page = GenericXLogRegisterBuffer(state, buf, GENERIC_XLOG_FULL_IMAGE);
	PageInit(page, BLCKSZ, GPH_SPECIAL_SIZE);

	special = GphPageSpecialPtr(page);
	special->gph_page_type = GPH_PAGE_META;
	special->gph_unused = 0;
	special->gph_next_pageno = InvalidBlockNumber;
	special->gph_owner_vid = 0;

	meta = (GphMeta *) GphPageRecordBase(page);
	meta->gm_magic = GPH_MAGIC;
	meta->gm_version = GPH_VERSION;
	meta->gm_next_vid = 0;
	meta->gm_vertex_count = 0;
	meta->gm_reserved = 0;
	meta->gm_edge_count = 0;
	meta->gm_first_vertex_blk = InvalidBlockNumber;
	meta->gm_last_vertex_blk = InvalidBlockNumber;
	((PageHeader) page)->pd_lower += MAXALIGN(sizeof(GphMeta));

	GenericXLogFinish(state);
	UnlockReleaseBuffer(buf);
}

/* Read a copy of the metapage struct (shared lock). */
static void
gph_read_meta(Relation rel, GphMeta *out)
{
	Buffer		buf = ReadBufferExtended(rel, MAIN_FORKNUM, GPH_META_BLKNO, RBM_NORMAL, NULL);

	LockBuffer(buf, BUFFER_LOCK_SHARE);
	memcpy(out, GphPageRecordBase(BufferGetPage(buf)), sizeof(GphMeta));
	UnlockReleaseBuffer(buf);

	if (out->gm_magic != GPH_MAGIC)
		ereport(ERROR,
				(errmsg("graph_store: bad metapage magic 0x%08x (store not initialized?)",
						out->gm_magic)));
}

/*
 * Locate the vertex record for `vid`: scan the vertex-page chain and return the (block, slot)
 * of its GphVertexRecord, plus a copy of the record. Returns false if not found. Read-only
 * (no lock held on return).
 */
static bool
gph_locate_vertex(Relation rel, uint64 vid, BlockNumber *out_blk, uint32 *out_slot,
				  GphVertexRecord *out_rec)
{
	GphMeta		meta;
	BlockNumber	blk;

	if (RelationGetNumberOfBlocks(rel) == 0)
		return false;			/* store not initialized => no vertices */

	gph_read_meta(rel, &meta);
	blk = meta.gm_first_vertex_blk;

	while (blk != InvalidBlockNumber)
	{
		Buffer		buf;
		Page		page;
		uint32		count,
					i;
		BlockNumber	next;
		bool		found = false;

		CHECK_FOR_INTERRUPTS();
		buf = ReadBufferExtended(rel, MAIN_FORKNUM, blk, RBM_NORMAL, NULL);
		LockBuffer(buf, BUFFER_LOCK_SHARE);
		page = BufferGetPage(buf);
		count = GphPageRecordCount(page, sizeof(GphVertexRecord));
		for (i = 0; i < count; i++)
		{
			GphVertexRecord *vr = GphPageGetRecord(page, i, sizeof(GphVertexRecord));

			/* Skip records whose inserting txn aborted / is not visible to us. */
			if (vr->vr_vid == vid && gph_xmin_visible(vr->vr_xmin))
			{
				*out_blk = blk;
				*out_slot = i;
				memcpy(out_rec, vr, sizeof(GphVertexRecord));
				found = true;
				break;
			}
		}
		next = GphPageSpecialPtr(page)->gph_next_pageno;
		UnlockReleaseBuffer(buf);

		if (found)
			return true;
		blk = next;
	}
	return false;
}

/* ------------------------------------------------------------------ */
/* Mutation: insert vertex / insert edge                               */
/* ------------------------------------------------------------------ */

PG_FUNCTION_INFO_V1(gph_insert_vertex);

/* gph_insert_vertex() RETURNS bigint — assign a dense vid, append its vertex record. */
Datum
gph_insert_vertex(PG_FUNCTION_ARGS)
{
	Relation	rel = gph_open_store(RowExclusiveLock);
	GenericXLogState *state;
	Buffer		metabuf,
				vbuf;
	Page		metapage,
				vpage;
	GphMeta    *meta;
	GphVertexRecord vr;
	BlockNumber	vblk;
	uint64		vid;

	gph_ensure_meta(rel);

	metabuf = ReadBufferExtended(rel, MAIN_FORKNUM, GPH_META_BLKNO, RBM_NORMAL, NULL);
	LockBuffer(metabuf, BUFFER_LOCK_EXCLUSIVE);
	metapage = BufferGetPage(metabuf);
	meta = (GphMeta *) GphPageRecordBase(metapage);

	vid = meta->gm_next_vid;

	memset(&vr, 0, sizeof(vr));
	vr.vr_vid = vid;
	vr.vr_label_id = 1;			/* entity */
	vr.vr_flags = 0;
	vr.vr_adj_head = InvalidBlockNumber;
	vr.vr_adj_tail = InvalidBlockNumber;
	vr.vr_xmin = GetCurrentTransactionId();

	vblk = meta->gm_last_vertex_blk;

	if (vblk == InvalidBlockNumber)
	{
		/* Case 1: first vertex — allocate the first vertex page. */
		vbuf = gph_extend_page(rel);
		vblk = BufferGetBlockNumber(vbuf);

		state = GenericXLogStart(rel);
		metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
		meta = (GphMeta *) GphPageRecordBase(metapage);
		vpage = GenericXLogRegisterBuffer(state, vbuf, GENERIC_XLOG_FULL_IMAGE);

		PageInit(vpage, BLCKSZ, GPH_SPECIAL_SIZE);
		GphPageSpecialPtr(vpage)->gph_page_type = GPH_PAGE_VERTEX;
		GphPageSpecialPtr(vpage)->gph_unused = 0;
		GphPageSpecialPtr(vpage)->gph_next_pageno = InvalidBlockNumber;
		GphPageSpecialPtr(vpage)->gph_owner_vid = 0;
		GphPageAppendRecord(vpage, &vr, sizeof(GphVertexRecord));

		meta->gm_first_vertex_blk = vblk;
		meta->gm_last_vertex_blk = vblk;
		meta->gm_next_vid = vid + 1;
		meta->gm_vertex_count += 1;

		GenericXLogFinish(state);
		UnlockReleaseBuffer(vbuf);
		UnlockReleaseBuffer(metabuf);
	}
	else
	{
		vbuf = ReadBufferExtended(rel, MAIN_FORKNUM, vblk, RBM_NORMAL, NULL);
		LockBuffer(vbuf, BUFFER_LOCK_EXCLUSIVE);

		if (GphPageHasRoom(BufferGetPage(vbuf), sizeof(GphVertexRecord)))
		{
			/* Case 2: room on the current tail vertex page. */
			state = GenericXLogStart(rel);
			metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
			meta = (GphMeta *) GphPageRecordBase(metapage);
			vpage = GenericXLogRegisterBuffer(state, vbuf, 0);

			GphPageAppendRecord(vpage, &vr, sizeof(GphVertexRecord));
			meta->gm_next_vid = vid + 1;
			meta->gm_vertex_count += 1;

			GenericXLogFinish(state);
			UnlockReleaseBuffer(vbuf);
			UnlockReleaseBuffer(metabuf);
		}
		else
		{
			/* Case 3: tail vertex page full — chain a new vertex page. */
			Buffer		newbuf = gph_extend_page(rel);
			BlockNumber	newblk = BufferGetBlockNumber(newbuf);
			Page		newpage;

			state = GenericXLogStart(rel);
			metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
			meta = (GphMeta *) GphPageRecordBase(metapage);
			vpage = GenericXLogRegisterBuffer(state, vbuf, 0);
			newpage = GenericXLogRegisterBuffer(state, newbuf, GENERIC_XLOG_FULL_IMAGE);

			PageInit(newpage, BLCKSZ, GPH_SPECIAL_SIZE);
			GphPageSpecialPtr(newpage)->gph_page_type = GPH_PAGE_VERTEX;
			GphPageSpecialPtr(newpage)->gph_unused = 0;
			GphPageSpecialPtr(newpage)->gph_next_pageno = InvalidBlockNumber;
			GphPageSpecialPtr(newpage)->gph_owner_vid = 0;
			GphPageAppendRecord(newpage, &vr, sizeof(GphVertexRecord));

			GphPageSpecialPtr(vpage)->gph_next_pageno = newblk;
			meta->gm_last_vertex_blk = newblk;
			meta->gm_next_vid = vid + 1;
			meta->gm_vertex_count += 1;

			GenericXLogFinish(state);
			UnlockReleaseBuffer(newbuf);
			UnlockReleaseBuffer(vbuf);
			UnlockReleaseBuffer(metabuf);
		}
	}

	relation_close(rel, RowExclusiveLock);
	PG_RETURN_INT64((int64) vid);
}

PG_FUNCTION_INFO_V1(gph_insert_edge);

/* gph_insert_edge(src bigint, dst bigint) — append one directed :related_to edge. */
Datum
gph_insert_edge(PG_FUNCTION_ARGS)
{
	uint64		src = (uint64) PG_GETARG_INT64(0);
	uint64		dst = (uint64) PG_GETARG_INT64(1);
	Relation	rel = gph_open_store(RowExclusiveLock);
	GenericXLogState *state;
	Buffer		metabuf,
				vbuf,
				abuf;
	Page		metapage,
				vpage,
				apage;
	GphMeta    *meta;
	BlockNumber	vblk,
				ablk,
				dst_blk;
	uint32		vslot,
				dst_slot;
	GphVertexRecord src_rec,
				dst_rec,
			   *vr;
	GphEdgeSlot	es;

	/* Both endpoints must exist. Locate dst first (validate only), src last so
	 * vblk/vslot/src_rec describe the source vertex we are about to update. The
	 * ereport(ERROR) aborts the txn before any page is locked or mutated, so a
	 * rejected edge never bumps gm_edge_count (correct by construction). */
	if (!gph_locate_vertex(rel, dst, &dst_blk, &dst_slot, &dst_rec))
		ereport(ERROR,
				(errmsg("graph_store: destination vertex " UINT64_FORMAT " does not exist", dst)));
	if (!gph_locate_vertex(rel, src, &vblk, &vslot, &src_rec))
		ereport(ERROR,
				(errmsg("graph_store: source vertex " UINT64_FORMAT " does not exist", src)));

	memset(&es, 0, sizeof(es));
	es.es_src_vid = src;
	es.es_dst_vid = dst;
	es.es_edge_type_id = GPH_EDGE_TYPE_RELATED_TO;
	es.es_flags = 0;
	es.es_xmin = GetCurrentTransactionId();

	/*
	 * Lock the metapage FIRST (block GPH_META_BLKNO), then the src vertex page. This matches the
	 * metapage -> vertex page -> adjacency page lock order of gph_insert_vertex (graph_am.c) and
	 * avoids a buffer-lock deadlock between the two writers. The store-wide gm_edge_count counter
	 * lives on the metapage, so every edge insert serializes on this single buffer — acceptable
	 * for the v1 bulk-load-then-query workload (ADR-0007); see the per-vertex vr_out_degree escape
	 * hatch noted in plan 006 if concurrent ingest ever makes this a hot spot.
	 */
	metabuf = ReadBufferExtended(rel, MAIN_FORKNUM, GPH_META_BLKNO, RBM_NORMAL, NULL);
	LockBuffer(metabuf, BUFFER_LOCK_EXCLUSIVE);

	/* Lock the src vertex page (we will update vr_adj_head/tail) and RE-READ the record under
	 * the lock: gph_locate_vertex released its share lock, so the cached src_rec.vr_adj_tail can
	 * be stale. The slot index is stable (records never move), so vslot is still valid. */
	vbuf = ReadBufferExtended(rel, MAIN_FORKNUM, vblk, RBM_NORMAL, NULL);
	LockBuffer(vbuf, BUFFER_LOCK_EXCLUSIVE);
	src_rec = *(GphVertexRecord *) GphPageGetRecord(BufferGetPage(vbuf), vslot,
												   sizeof(GphVertexRecord));

	if (src_rec.vr_adj_tail == InvalidBlockNumber)
	{
		/* First edge for src: allocate its first adjacency page. */
		abuf = gph_extend_page(rel);
		ablk = BufferGetBlockNumber(abuf);

		state = GenericXLogStart(rel);
		metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
		meta = (GphMeta *) GphPageRecordBase(metapage);
		vpage = GenericXLogRegisterBuffer(state, vbuf, 0);
		apage = GenericXLogRegisterBuffer(state, abuf, GENERIC_XLOG_FULL_IMAGE);

		PageInit(apage, BLCKSZ, GPH_SPECIAL_SIZE);
		GphPageSpecialPtr(apage)->gph_page_type = GPH_PAGE_ADJ;
		GphPageSpecialPtr(apage)->gph_unused = 0;
		GphPageSpecialPtr(apage)->gph_next_pageno = InvalidBlockNumber;
		GphPageSpecialPtr(apage)->gph_owner_vid = src;
		GphPageAppendRecord(apage, &es, sizeof(GphEdgeSlot));

		vr = GphPageGetRecord(vpage, vslot, sizeof(GphVertexRecord));
		vr->vr_adj_head = ablk;
		vr->vr_adj_tail = ablk;

		meta->gm_edge_count += 1;
	}
	else
	{
		abuf = ReadBufferExtended(rel, MAIN_FORKNUM, src_rec.vr_adj_tail, RBM_NORMAL, NULL);
		LockBuffer(abuf, BUFFER_LOCK_EXCLUSIVE);

		if (GphPageHasRoom(BufferGetPage(abuf), sizeof(GphEdgeSlot)))
		{
			state = GenericXLogStart(rel);
			metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
			meta = (GphMeta *) GphPageRecordBase(metapage);
			apage = GenericXLogRegisterBuffer(state, abuf, 0);
			GphPageAppendRecord(apage, &es, sizeof(GphEdgeSlot));
			/* vertex record unchanged; do not register vbuf. metabuf IS registered so the
			 * gm_edge_count increment is WAL-logged atomically with the edge slot. */
			meta->gm_edge_count += 1;
		}
		else
		{
			/* Tail page full: chain a new adjacency page. */
			Buffer		newbuf = gph_extend_page(rel);
			BlockNumber	newblk = BufferGetBlockNumber(newbuf);
			Page		newpage,
						tailpage;

			state = GenericXLogStart(rel);
			metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
			meta = (GphMeta *) GphPageRecordBase(metapage);
			vpage = GenericXLogRegisterBuffer(state, vbuf, 0);
			tailpage = GenericXLogRegisterBuffer(state, abuf, 0);
			newpage = GenericXLogRegisterBuffer(state, newbuf, GENERIC_XLOG_FULL_IMAGE);

			PageInit(newpage, BLCKSZ, GPH_SPECIAL_SIZE);
			GphPageSpecialPtr(newpage)->gph_page_type = GPH_PAGE_ADJ;
			GphPageSpecialPtr(newpage)->gph_unused = 0;
			GphPageSpecialPtr(newpage)->gph_next_pageno = InvalidBlockNumber;
			GphPageSpecialPtr(newpage)->gph_owner_vid = src;
			GphPageAppendRecord(newpage, &es, sizeof(GphEdgeSlot));

			GphPageSpecialPtr(tailpage)->gph_next_pageno = newblk;
			vr = GphPageGetRecord(vpage, vslot, sizeof(GphVertexRecord));
			vr->vr_adj_tail = newblk;

			meta->gm_edge_count += 1;

			GenericXLogFinish(state);
			UnlockReleaseBuffer(newbuf);
			UnlockReleaseBuffer(abuf);
			UnlockReleaseBuffer(vbuf);
			UnlockReleaseBuffer(metabuf);
			relation_close(rel, RowExclusiveLock);
			PG_RETURN_VOID();
		}
	}

	GenericXLogFinish(state);
	UnlockReleaseBuffer(abuf);
	UnlockReleaseBuffer(vbuf);
	UnlockReleaseBuffer(metabuf);
	relation_close(rel, RowExclusiveLock);
	PG_RETURN_VOID();
}

/* ------------------------------------------------------------------ */
/* Traversal: Open / Next / Close incremental iterator                 */
/* ------------------------------------------------------------------ */

/*
 * GraphScanDescData — the concrete body of the opaque GraphScanDesc declared in graphstore.h.
 * Holds only the cursor (current adjacency page + next slot) plus the source/direction; it does
 * NOT retain the container Relation or a buffer pin, so the iterator is leak-free under early
 * abandon (LIMIT) and the caller manages the Relation lifetime (passed to gs_getnext).
 */
struct GraphScanDescData
{
	GraphVertexId		src;		/* source vertex of this traversal */
	GraphScanDirection	direction;	/* v1: GRAPH_SCAN_OUTGOING only */
	BlockNumber			cur_blk;	/* current adjacency page; Invalid = exhausted */
	uint32				cur_slot;	/* next EdgeSlot index on cur_blk */
};

/*
 * gs_open — position a traversal scan before src's first out-edge (the Open of Open/Next/Close).
 * `rel` is BORROWED (used only to locate the vertex; not retained). Returns true if `src` exists
 * (scan positioned, possibly over an empty adjacency list); false if `src` is absent. Policy is
 * the caller's: the SQL SRFs treat an absent source as an empty result, while a direct C consumer
 * (e.g. the TJS operator) may raise. v1 supports GRAPH_SCAN_OUTGOING only.
 */
static bool
gs_open(GraphScanDesc *scan, Relation rel, GraphVertexId start, GraphScanDirection direction)
{
	GphVertexRecord src_rec;
	BlockNumber		vblk;
	uint32			vslot;

	if (direction != GRAPH_SCAN_OUTGOING)
		ereport(ERROR,
				(errmsg("graph_store: only GRAPH_SCAN_OUTGOING is supported in v1 (got %d)",
						(int) direction)));

	scan->src = start;
	scan->direction = direction;
	scan->cur_blk = InvalidBlockNumber;
	scan->cur_slot = 0;

	if (!gph_locate_vertex(rel, start, &vblk, &vslot, &src_rec))
		return false;			/* absent => caller decides (empty scan vs raise) */
	scan->cur_blk = src_rec.vr_adj_head;	/* Invalid if src has no edges => empty scan */
	return true;
}

/*
 * gs_getnext — advance the scan by ONE visible :related_to edge (the Next of Open/Next/Close).
 * Reads at most one adjacency page per call from `rel` and holds no buffer pin across calls, so a
 * LIMIT above stops before later chain pages are ever read (TR-1 storage-level early termination)
 * and abandoning the scan early leaks nothing. Fills *out and returns true; on exhaustion sets
 * out->kind = GRAPH_ELEM_NONE and returns false.
 */
static bool
gs_getnext(Relation rel, GraphScanDesc *scan, GraphElement *out)
{
	out->kind = GRAPH_ELEM_NONE;

	while (scan->cur_blk != InvalidBlockNumber)
	{
		Buffer		buf;
		Page		page;
		uint32		count;
		GphEdgeSlot	slot;
		bool		have_edge = false;

		CHECK_FOR_INTERRUPTS();
		buf = ReadBufferExtended(rel, MAIN_FORKNUM, scan->cur_blk, RBM_NORMAL, NULL);
		LockBuffer(buf, BUFFER_LOCK_SHARE);
		page = BufferGetPage(buf);
		count = GphPageRecordCount(page, sizeof(GphEdgeSlot));

		if (scan->cur_slot < count)
		{
			memcpy(&slot, GphPageGetRecord(page, scan->cur_slot, sizeof(GphEdgeSlot)),
				   sizeof(GphEdgeSlot));
			scan->cur_slot++;
			have_edge = true;
		}
		else
		{
			scan->cur_blk = GphPageSpecialPtr(page)->gph_next_pageno;
			scan->cur_slot = 0;
		}
		UnlockReleaseBuffer(buf);

		if (have_edge)
		{
			if (slot.es_flags & GPH_FLAG_DELETED)
				continue;
			if (slot.es_edge_type_id != GPH_EDGE_TYPE_RELATED_TO)
				continue;
			if (!gph_xmin_visible(slot.es_xmin))
				continue;		/* edge from an aborted/uncommitted txn */

			out->kind = GRAPH_ELEM_EDGE;
			out->edge_id = GRAPHSTORE_INVALID_ID;	/* v1 edge slots carry no stored id */
			out->edge_src = slot.es_src_vid;
			out->edge_dst = slot.es_dst_vid;
			out->vertex_id = slot.es_dst_vid;		/* convenience: the reached neighbor */
			out->payload = NULL;
			out->payload_len = 0;
			gph_visit_counter++;	/* one unit of traversal work, per edge emitted */
			return true;
		}
		/* else: advanced to next chain page (or exhausted); loop again */
	}
	return false;
}

/* gs_close — release a traversal scan (the Close of Open/Next/Close). The scan holds no buffer
 * pin or Relation, so this only resets the cursor; idempotent on NULL. */
static void
gs_close(GraphScanDesc *scan)
{
	if (scan == NULL)
		return;
	scan->cur_blk = InvalidBlockNumber;
	scan->cur_slot = 0;
}

PG_FUNCTION_INFO_V1(gph_neighbors);

/*
 * gph_neighbors(src bigint) RETURNS SETOF bigint — yield src's out-neighbor vids one per Next(),
 * over the shared gs_open/gs_getnext/gs_close engine. The relation is opened/closed per call and
 * no buffer pin is held across calls, so abandoning the scan early (LIMIT) leaks nothing.
 */
Datum
gph_neighbors(PG_FUNCTION_ARGS)
{
	FuncCallContext *funcctx;
	GraphScanDesc  *scan;
	Relation		rel;
	GraphElement	elem;

	if (SRF_IS_FIRSTCALL())		/* === Open === */
	{
		MemoryContext oldctx;
		GraphVertexId src = (GraphVertexId) PG_GETARG_INT64(0);

		funcctx = SRF_FIRSTCALL_INIT();
		oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);

		scan = (GraphScanDesc *) palloc0(sizeof(GraphScanDesc));
		rel = gph_open_store(AccessShareLock);
		(void) gs_open(scan, rel, src, GRAPH_SCAN_OUTGOING);	/* absent src => empty (lenient SRF) */
		relation_close(rel, AccessShareLock);

		funcctx->user_fctx = scan;
		MemoryContextSwitchTo(oldctx);
	}

	funcctx = SRF_PERCALL_SETUP();	/* === Next === */
	scan = (GraphScanDesc *) funcctx->user_fctx;

	rel = gph_open_store(AccessShareLock);
	if (gs_getnext(rel, scan, &elem))
	{
		relation_close(rel, AccessShareLock);
		SRF_RETURN_NEXT(funcctx, Int64GetDatum((int64) elem.edge_dst));
	}
	relation_close(rel, AccessShareLock);	/* === Close === */
	gs_close(scan);
	SRF_RETURN_DONE(funcctx);
}

PG_FUNCTION_INFO_V1(gph_traverse);

/*
 * gph_traverse(src bigint) RETURNS TABLE(src bigint, dst bigint) — the edge-emitting traversal:
 * one :related_to edge per Next() (not just the bare neighbor vid), so a caller can surface the
 * edge endpoints and join dst back to its relational/vector payload (the canonical query's
 * COLUMNS projection). Same shared engine and early-termination property as gph_neighbors.
 *
 * MUST be used as a target-list / ProjectSet SRF (not a FROM-clause FunctionScan): a FROM-clause
 * SRF is materialized to a tuplestore before LIMIT applies, which forfeits early termination.
 * v1 edge slots carry no stored edge id, so only (src, dst) are surfaced.
 */
Datum
gph_traverse(PG_FUNCTION_ARGS)
{
	FuncCallContext *funcctx;
	GraphScanDesc  *scan;
	Relation		rel;
	GraphElement	elem;

	if (SRF_IS_FIRSTCALL())		/* === Open === */
	{
		MemoryContext oldctx;
		TupleDesc	tupdesc;
		GraphVertexId src = (GraphVertexId) PG_GETARG_INT64(0);

		funcctx = SRF_FIRSTCALL_INIT();
		oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);

		if (get_call_result_type(fcinfo, NULL, &tupdesc) != TYPEFUNC_COMPOSITE)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("gph_traverse must be called in a context that expects a record")));
		funcctx->tuple_desc = BlessTupleDesc(tupdesc);

		scan = (GraphScanDesc *) palloc0(sizeof(GraphScanDesc));
		rel = gph_open_store(AccessShareLock);
		(void) gs_open(scan, rel, src, GRAPH_SCAN_OUTGOING);	/* absent src => empty (lenient SRF) */
		relation_close(rel, AccessShareLock);

		funcctx->user_fctx = scan;
		MemoryContextSwitchTo(oldctx);
	}

	funcctx = SRF_PERCALL_SETUP();	/* === Next === */
	scan = (GraphScanDesc *) funcctx->user_fctx;

	rel = gph_open_store(AccessShareLock);
	if (gs_getnext(rel, scan, &elem))
	{
		Datum		values[2];
		bool		nulls[2] = {false, false};
		HeapTuple	tup;

		relation_close(rel, AccessShareLock);
		values[0] = Int64GetDatum((int64) elem.edge_src);
		values[1] = Int64GetDatum((int64) elem.edge_dst);
		tup = heap_form_tuple(funcctx->tuple_desc, values, nulls);
		SRF_RETURN_NEXT(funcctx, HeapTupleGetDatum(tup));
	}
	relation_close(rel, AccessShareLock);	/* === Close === */
	gs_close(scan);
	SRF_RETURN_DONE(funcctx);
}

/* ------------------------------------------------------------------ */
/* Probes                                                              */
/* ------------------------------------------------------------------ */

PG_FUNCTION_INFO_V1(gph_visits);

/* gph_visits() RETURNS bigint — per-backend traversal-step counter (TR-1 probe). */
Datum
gph_visits(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT64(gph_visit_counter);
}

PG_FUNCTION_INFO_V1(gph_vertex_count);

/*
 * gph_vertex_count() RETURNS bigint — count of VISIBLE vertices. Scans the vertex-page chain
 * applying MVCC visibility (the raw metapage counter is not abort-aware), so an aborted insert
 * is not counted.
 */
Datum
gph_vertex_count(PG_FUNCTION_ARGS)
{
	Relation	rel = gph_open_store(AccessShareLock);
	GphMeta		meta;
	BlockNumber	blk;
	int64		n = 0;

	if (RelationGetNumberOfBlocks(rel) == 0)
	{
		relation_close(rel, AccessShareLock);
		PG_RETURN_INT64(0);
	}

	gph_read_meta(rel, &meta);
	blk = meta.gm_first_vertex_blk;
	while (blk != InvalidBlockNumber)
	{
		Buffer		buf;
		Page		page;
		uint32		count,
					i;
		BlockNumber	next;

		CHECK_FOR_INTERRUPTS();
		buf = ReadBufferExtended(rel, MAIN_FORKNUM, blk, RBM_NORMAL, NULL);
		LockBuffer(buf, BUFFER_LOCK_SHARE);
		page = BufferGetPage(buf);
		count = GphPageRecordCount(page, sizeof(GphVertexRecord));
		for (i = 0; i < count; i++)
		{
			GphVertexRecord *vr = GphPageGetRecord(page, i, sizeof(GphVertexRecord));

			if (gph_xmin_visible(vr->vr_xmin))
				n++;
		}
		next = GphPageSpecialPtr(page)->gph_next_pageno;
		UnlockReleaseBuffer(buf);
		blk = next;
	}

	relation_close(rel, AccessShareLock);
	PG_RETURN_INT64(n);
}

PG_FUNCTION_INFO_V1(gph_edge_count);

/*
 * gph_edge_count() RETURNS bigint — the store-wide directed-edge count carried on the metapage
 * (gm_edge_count), read under a share lock. This is the raw counter, not an MVCC-visible count:
 * v1 has no edge-delete path so the counter only grows, and it is maintained under GenericXLog so
 * a crashed/aborted txn's increments roll back with the page image (full-image WAL). Like
 * gph_vertex_count's metapage counter it is NOT abort-aware for the in-process-abort case — but
 * there is no edge analogue of gph_vertex_count's per-record visibility scan because edge slots are
 * not enumerated here; this exposes gm_edge_count directly for the avg_out_degree derivation and the
 * crash-recovery assertion. (See plan 006 "Abort accounting caveat".)
 */
Datum
gph_edge_count(PG_FUNCTION_ARGS)
{
	Relation	rel = gph_open_store(AccessShareLock);
	GphMeta		meta;

	if (RelationGetNumberOfBlocks(rel) == 0)
	{
		relation_close(rel, AccessShareLock);
		PG_RETURN_INT64(0);
	}

	gph_read_meta(rel, &meta);
	relation_close(rel, AccessShareLock);
	PG_RETURN_INT64((int64) meta.gm_edge_count);
}

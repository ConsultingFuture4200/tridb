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
#include "access/multixact.h"
#include "access/relation.h"
#include "access/transam.h"
#include "access/xact.h"
#include "catalog/namespace.h"
#include "commands/vacuum.h"
#include "executor/spi.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "storage/bufmgr.h"
#include "storage/lmgr.h"
#include "storage/procarray.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/hsearch.h"
#include "utils/inval.h"
#include "utils/memutils.h"
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
 * Is a record (vertex or edge) tombstoned AND is that tombstone visible to us? A record is
 * invisible-by-delete iff GPH_FLAG_DELETED is set AND the DELETING xid (es_xmax / vr_xmax) is
 * current-or-committed. A tombstone written by a txn that later aborts (or is still in progress)
 * has an xmax that gph_xmin_visible rejects, so the record stays LIVE — which is exactly what
 * makes gph_tombstone_edge/vertex roll back atomically with the host txn (FR-7), reusing the same
 * xid-visibility mechanism that gph_xmin_visible gives INSERTs (a bare flag would have no
 * transaction stamp and so could not be rolled back — GenericXLog has no in-process UNDO).
 *
 * ADDITIVE: for every record written before plan 037, GPH_FLAG_DELETED is clear (nothing set it)
 * and the repurposed xmax field is 0 (InvalidTransactionId, from the insert-path memset), so this
 * returns false and the read paths behave byte-identically to pre-037.
 */
static inline bool
gph_deleted_visible(uint32 flags, TransactionId xmax)
{
	return (flags & GPH_FLAG_DELETED) && gph_xmin_visible(xmax);
}

/*
 * Per-backend traversal-step counter (one increment per edge EMITTED by gph_neighbors).
 * Demonstrates that pulling K of N neighbors under LIMIT K does ~K units of work, not N —
 * the TR-1 early-termination probe (mirrors the v0 graph_visits()). Backend-local and
 * monotonic for the life of the backend: read DELTAS (v1 - v0), never the absolute value.
 */
static int64 gph_visit_counter = 0;

/*
 * Per-backend adjacency-page-read counter (one increment per adjacency page READ by a traversal
 * scan, NOT per neighbor emitted). Backend-local and monotonic for the life of the backend: read
 * DELTAS (v1 - v0), never the absolute value. With the read-once scan a degree-D hub on P chained
 * pages costs ~P page reads instead of ~D (one ReadBuffer per emitted neighbor) — this counter is
 * the probe that demonstrates that reduction.
 */
static int64 gph_page_read_counter = 0;

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
	meta->gm_frozen_horizon = InvalidTransactionId;	/* never frozen (advisor 036) */
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

			/* Skip records whose inserting txn aborted / is not visible to us, and vertices
			 * tombstoned by a visible gph_tombstone_vertex (plan 037). */
			if (vr->vr_vid == vid && gph_xmin_visible(vr->vr_xmin) &&
				!gph_deleted_visible(vr->vr_flags, vr->vr_xmax))
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

/*
 * gph_locate_vertex_dense — O(1) DENSE vertex locate (DEV-1354, the batched-edge fast path).
 *
 * The linear gph_locate_vertex above walks the vertex-page chain and costs O(V) at chain position
 * V — which makes a whole-graph edge load O(E*V) (hours at 1M/39M). This is the constant-time
 * alternative that makes the batched load O(E): when vertices are materialized dense-in-order with
 * NO adjacency pages interleaved (the wiki bulk-load precondition — every gph_insert_vertex before
 * the first edge), the vertex pages are physically CONTIGUOUS and fully packed, so vid V is at
 * block gm_first_vertex_blk + V/perpage, slot V%perpage. One page read, no chain walk.
 *
 * SAFETY (never write to a mis-computed vertex — the golden guard): the computed page is HARD-
 * verified to (a) be a GPH_PAGE_VERTEX page and (b) actually carry vr_vid == vid at the computed
 * slot. If either fails the layout is NOT dense (sparse ids, or a vertex inserted AFTER edges broke
 * contiguity) and we ereport ERROR rather than proceed — so a non-dense caller gets a hard failure,
 * never silent corruption. `meta` is a caller-provided snapshot (bounds + gm_first_vertex_blk).
 * Returns false only for the benign "vid never existed / store empty / tombstoned" cases (caller
 * decides); the layout-violation cases ERROR.
 */
static bool
gph_locate_vertex_dense(Relation rel, uint64 vid, const GphMeta *meta,
						BlockNumber *out_blk, uint32 *out_slot, GphVertexRecord *out_rec)
{
	uint32		perpage = GphVerticesPerPage();
	BlockNumber	blk;
	uint32		slot;
	Buffer		buf;
	Page		page;
	GphVertexRecord *vr;

	if (meta->gm_first_vertex_blk == InvalidBlockNumber || vid >= meta->gm_next_vid)
		return false;			/* store empty / vid never assigned => benign miss */

	blk = meta->gm_first_vertex_blk + (BlockNumber) (vid / perpage);
	slot = (uint32) (vid % perpage);
	if (blk >= RelationGetNumberOfBlocks(rel))
		ereport(ERROR,
				(errmsg("graph_store: dense locate for vid " UINT64_FORMAT " computed block %u past EOF "
						"(non-dense layout)", vid, blk)));

	buf = ReadBufferExtended(rel, MAIN_FORKNUM, blk, RBM_NORMAL, NULL);
	LockBuffer(buf, BUFFER_LOCK_SHARE);
	page = BufferGetPage(buf);

	if (GphPageSpecialPtr(page)->gph_page_type != GPH_PAGE_VERTEX ||
		slot >= GphPageRecordCount(page, sizeof(GphVertexRecord)))
	{
		UnlockReleaseBuffer(buf);
		ereport(ERROR,
				(errmsg("graph_store: dense locate for vid " UINT64_FORMAT " hit page %u slot %u that is "
						"not a packed vertex slot (non-dense layout — refusing to write)", vid, blk, slot)));
	}

	vr = GphPageGetRecord(page, slot, sizeof(GphVertexRecord));
	if (vr->vr_vid != vid)
	{
		uint64		got = vr->vr_vid;

		UnlockReleaseBuffer(buf);
		ereport(ERROR,
				(errmsg("graph_store: dense locate mismatch on page %u slot %u: found vid " UINT64_FORMAT
						", wanted " UINT64_FORMAT " (non-dense layout — refusing to write to a mis-computed "
						"vertex)", blk, slot, got, vid)));
	}

	/* Same visibility filter as the linear path: an aborted/tombstoned vertex is a benign miss. */
	if (!gph_xmin_visible(vr->vr_xmin) || gph_deleted_visible(vr->vr_flags, vr->vr_xmax))
	{
		UnlockReleaseBuffer(buf);
		return false;
	}

	*out_blk = blk;
	*out_slot = slot;
	memcpy(out_rec, vr, sizeof(GphVertexRecord));
	UnlockReleaseBuffer(buf);
	return true;
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

/*
 * gph_insert_edge(src bigint, dst bigint [, type_id integer]) — append one directed edge.
 *
 * Backs BOTH SQL declarations (the original 2-arg and the plan-038 3-arg overload) at the same C
 * symbol; PG_NARGS() selects the path. The 2-arg form is byte-identical to the pre-038 body: no
 * third arg => type defaults to GPH_EDGE_TYPE_RELATED_TO, the exact constant the code hardcoded
 * before. The 3-arg form writes the caller's dictionary type id into the existing es_edge_type_id
 * field (no slot-layout change). Type ids are validated by the graph_store.edge_type dictionary at
 * registration time; the native store trusts the id it is handed (topology is native, the type
 * name<->id mapping is relational — golden rule 3).
 */
Datum
gph_insert_edge(PG_FUNCTION_ARGS)
{
	uint64		src = (uint64) PG_GETARG_INT64(0);
	uint64		dst = (uint64) PG_GETARG_INT64(1);
	uint32		type_id = GPH_EDGE_TYPE_RELATED_TO;
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

	/* Optional 3rd arg (plan 038): the dictionary edge type id. Absent => default RELATED_TO
	 * (2-arg overload => byte-identical to the pre-038 hardcode). The 3-arg overload is STRICT,
	 * so a passed arg is never NULL. */
	if (PG_NARGS() >= 3)
		type_id = (uint32) PG_GETARG_INT32(2);

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
	es.es_edge_type_id = type_id;	/* default RELATED_TO (2-arg) or the caller's dictionary id (3-arg) */
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

PG_FUNCTION_INFO_V1(gph_insert_edges);

/*
 * gph_insert_edges(src bigint, dst bigint[]) RETURNS bigint — the BATCHED edge-append entry point
 * (DEV-1354 / spec §2 "bulk edge loader"). Appends a whole adjacency run for ONE source in a single
 * call, so the wiki-scale graph (1M vertices / 39M induced edges) loads in minutes instead of the
 * O(E*V) hours that N x gph_insert_edge cost (each scalar call did TWO O(V) linear vertex locates).
 * Returns the number of edges appended (== array length).
 *
 * The result is BYTE-IDENTICAL to N x gph_insert_edge(src, dst[i]) fed in array order (design §2
 * parity contract): same append order, same slot layout, same page chaining. Only two things change
 * vs the scalar path — both O(1) instead of O(V):
 *   - src is located with gph_locate_vertex_dense (dense fast path; hard-verified, never corrupts);
 *   - dst existence + visibility is a metapage bounds check (0 <= dst < gm_next_vid) followed by
 *     gph_locate_vertex_dense (advisor plan 046): under the dense-in-order load ext_id == vid, so
 *     a dst is a candidate vertex iff it is in [0, next_vid), and the dense locate's visibility
 *     filter rejects a tombstoned dst the same way the scalar gph_locate_vertex does — this keeps
 *     scalar/batch parity (a tombstoned dst is rejected either way) while staying O(1) per dst.
 *
 * WAL (the delicate part): GenericXLog caps at MAX_GENERIC_XLOG_PAGES (4) buffers per record, and a
 * multi-page adjacency run needs meta + vertex + old-tail + new-page = exactly 4 to chain ONE page.
 * So a run that spans many new pages is committed one GenericXLogFinish PER new page (never a single
 * record over the whole run). meta + the src vertex page are held EXCLUSIVE for the whole batch and
 * re-registered in each record; gm_edge_count is bumped by that record's slot count, so every record
 * is self-consistent and a crash mid-batch replays only completed page diffs. Abort atomicity is the
 * same as the scalar path: every slot is stamped es_xmin = current xid, so a rolled-back batch's
 * slots are all filtered out by gph_xmin_visible on read (=> zero visible edges), FR-7.
 *
 * Single-writer contract (graph_am.c header): the bulk loader is single-connection, so holding meta
 * + the vertex page locked across the run, and reading gm_next_vid once for the bounds check, are
 * safe (no concurrent vertex insert can move gm_next_vid mid-batch).
 */
Datum
gph_insert_edges(PG_FUNCTION_ARGS)
{
	uint64		src = (uint64) PG_GETARG_INT64(0);
	ArrayType  *dst_arr = PG_GETARG_ARRAYTYPE_P(1);
	Relation	rel = gph_open_store(RowExclusiveLock);
	GphMeta		meta0;
	GphVertexRecord src_rec;
	BlockNumber	vblk,
				tailblk;
	uint32		vslot;
	uint32		slotcap = GphEdgeSlotsPerPage();
	Buffer		metabuf,
				vbuf,
				tailbuf;
	int64	   *dsts;
	int			nelems,
				k;
	int64		appended = 0;
	TransactionId xid;
	GenericXLogState *state;
	Page		metapage,
				vpage,
				tailpage,
				newpage;
	GphMeta    *meta;
	GphVertexRecord *vr;

	/* Array shape guards: 1-D, no NULL elements (the loader never produces either). int8 elements
	 * are 8-byte, pass-by-value, stored inline and unaligned-safe to index as int64. */
	if (ARR_NDIM(dst_arr) > 1)
		ereport(ERROR,
				(errmsg("graph_store: gph_insert_edges expects a 1-dimensional dst array")));
	if (ARR_HASNULL(dst_arr))
		ereport(ERROR,
				(errmsg("graph_store: gph_insert_edges dst array must not contain NULLs")));
	nelems = (ARR_NDIM(dst_arr) == 0) ? 0
		: ArrayGetNItems(ARR_NDIM(dst_arr), ARR_DIMS(dst_arr));
	dsts = (int64 *) ARR_DATA_PTR(dst_arr);

	if (nelems == 0)
	{
		relation_close(rel, RowExclusiveLock);
		PG_RETURN_INT64(0);
	}

	gph_ensure_meta(rel);
	gph_read_meta(rel, &meta0);		/* snapshot: bounds (gm_next_vid) + gm_first_vertex_blk */

	/*
	 * O(1) dst existence + visibility (advisor plan 046): the scalar gph_insert_edge locates
	 * BOTH endpoints via gph_locate_vertex, which is visibility-checked (rejects a tombstoned
	 * dst). The batch path previously only bounds-checked dst against gm_next_vid, so a live
	 * src could append edges to a tombstoned dst — phantom adjacency. gph_locate_vertex_dense
	 * is the O(1) dense-layout equivalent of gph_locate_vertex (same visibility filter, same
	 * hard-ERROR on a non-dense layout as the src locate below), so this restores scalar/batch
	 * parity without giving up the O(1) dense fast path.
	 */
	for (k = 0; k < nelems; k++)
	{
		BlockNumber	dst_blk;
		uint32		dst_slot;
		GphVertexRecord dst_rec;

		if (dsts[k] < 0 || (uint64) dsts[k] >= meta0.gm_next_vid)
			ereport(ERROR,
					(errmsg("graph_store: destination vertex %lld out of dense range [0," UINT64_FORMAT ")",
							(long long) dsts[k], meta0.gm_next_vid)));
		if (!gph_locate_vertex_dense(rel, (uint64) dsts[k], &meta0, &dst_blk, &dst_slot, &dst_rec))
			ereport(ERROR,
					(errmsg("graph_store: destination vertex %lld does not exist (or has been deleted)",
							(long long) dsts[k])));
	}
	if (src >= meta0.gm_next_vid)
		ereport(ERROR,
				(errmsg("graph_store: source vertex " UINT64_FORMAT " out of dense range [0," UINT64_FORMAT ")",
						src, meta0.gm_next_vid)));

	/* O(1) dense src locate (hard-verified; ERRORs on a non-dense layout — never mis-writes). */
	if (!gph_locate_vertex_dense(rel, src, &meta0, &vblk, &vslot, &src_rec))
		ereport(ERROR,
				(errmsg("graph_store: source vertex " UINT64_FORMAT " not found (dense locate)", src)));

	xid = GetCurrentTransactionId();

	/* Lock meta + the src vertex page for the whole batch (meta -> vertex -> adj lock order). */
	metabuf = ReadBufferExtended(rel, MAIN_FORKNUM, GPH_META_BLKNO, RBM_NORMAL, NULL);
	LockBuffer(metabuf, BUFFER_LOCK_EXCLUSIVE);
	vbuf = ReadBufferExtended(rel, MAIN_FORKNUM, vblk, RBM_NORMAL, NULL);
	LockBuffer(vbuf, BUFFER_LOCK_EXCLUSIVE);
	src_rec = *(GphVertexRecord *) GphPageGetRecord(BufferGetPage(vbuf), vslot,
												   sizeof(GphVertexRecord));

	k = 0;
	tailblk = src_rec.vr_adj_tail;

	if (tailblk == InvalidBlockNumber)
	{
		/* src has no edges yet: allocate its first adjacency page and fill it. meta+vbuf+new = 3. */
		Buffer		newbuf = gph_extend_page(rel);
		BlockNumber	newblk = BufferGetBlockNumber(newbuf);
		uint32		fill = Min((uint32) (nelems - k), slotcap);
		uint32		i;

		state = GenericXLogStart(rel);
		metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
		meta = (GphMeta *) GphPageRecordBase(metapage);
		vpage = GenericXLogRegisterBuffer(state, vbuf, 0);
		newpage = GenericXLogRegisterBuffer(state, newbuf, GENERIC_XLOG_FULL_IMAGE);

		PageInit(newpage, BLCKSZ, GPH_SPECIAL_SIZE);
		GphPageSpecialPtr(newpage)->gph_page_type = GPH_PAGE_ADJ;
		GphPageSpecialPtr(newpage)->gph_unused = 0;
		GphPageSpecialPtr(newpage)->gph_next_pageno = InvalidBlockNumber;
		GphPageSpecialPtr(newpage)->gph_owner_vid = src;
		for (i = 0; i < fill; i++)
		{
			GphEdgeSlot	es;

			memset(&es, 0, sizeof(es));
			es.es_src_vid = src;
			es.es_dst_vid = (uint64) dsts[k + i];
			es.es_edge_type_id = GPH_EDGE_TYPE_RELATED_TO;
			es.es_flags = 0;
			es.es_xmin = xid;
			GphPageAppendRecord(newpage, &es, sizeof(GphEdgeSlot));
		}
		vr = GphPageGetRecord(vpage, vslot, sizeof(GphVertexRecord));
		vr->vr_adj_head = newblk;
		vr->vr_adj_tail = newblk;
		meta->gm_edge_count += fill;
		GenericXLogFinish(state);

		tailbuf = newbuf;		/* keep pinned+locked as the current append target */
		tailblk = newblk;
		k += fill;
		appended += fill;
	}
	else
	{
		/* Fill any remaining room on the current tail page first (meta+tail = 2). */
		uint32		used,
					room;

		tailbuf = ReadBufferExtended(rel, MAIN_FORKNUM, tailblk, RBM_NORMAL, NULL);
		LockBuffer(tailbuf, BUFFER_LOCK_EXCLUSIVE);
		used = GphPageRecordCount(BufferGetPage(tailbuf), sizeof(GphEdgeSlot));
		room = (slotcap > used) ? slotcap - used : 0;
		if (room > 0)
		{
			uint32		fill = Min((uint32) (nelems - k), room);
			uint32		i;

			state = GenericXLogStart(rel);
			metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
			meta = (GphMeta *) GphPageRecordBase(metapage);
			tailpage = GenericXLogRegisterBuffer(state, tailbuf, 0);
			for (i = 0; i < fill; i++)
			{
				GphEdgeSlot	es;

				memset(&es, 0, sizeof(es));
				es.es_src_vid = src;
				es.es_dst_vid = (uint64) dsts[k + i];
				es.es_edge_type_id = GPH_EDGE_TYPE_RELATED_TO;
				es.es_flags = 0;
				es.es_xmin = xid;
				GphPageAppendRecord(tailpage, &es, sizeof(GphEdgeSlot));
			}
			meta->gm_edge_count += fill;
			GenericXLogFinish(state);
			k += fill;
			appended += fill;
		}
	}

	/*
	 * Chain a new adjacency page for each remaining page-worth of dsts. Each iteration is ONE
	 * GenericXLog record over exactly 4 buffers: meta + vbuf (vr_adj_tail) + old tail (its
	 * gph_next_pageno) + new page (full image). Entering the loop the current tail is always FULL
	 * (the branch above either filled it exactly or left it full), so every chained page is opened
	 * fresh. The previous tail buffer is released only AFTER its next_pageno is durably linked.
	 */
	while (k < nelems)
	{
		Buffer		newbuf = gph_extend_page(rel);
		BlockNumber	newblk = BufferGetBlockNumber(newbuf);
		uint32		fill = Min((uint32) (nelems - k), slotcap);
		uint32		i;

		/* DEV-1354 (Linus review): keep a million-edge hub source interruptible —
		 * without this, Ctrl-C / statement_timeout cannot fire for the whole run (the
		 * scalar path got a CHECK per edge via gph_locate_vertex). Safe here: no
		 * GenericXLog record is open yet, and an ERROR longjmp releases the held
		 * meta/vbuf/tailbuf/newbuf locks via the resource owner. */
		CHECK_FOR_INTERRUPTS();

		state = GenericXLogStart(rel);
		metapage = GenericXLogRegisterBuffer(state, metabuf, 0);
		meta = (GphMeta *) GphPageRecordBase(metapage);
		vpage = GenericXLogRegisterBuffer(state, vbuf, 0);
		tailpage = GenericXLogRegisterBuffer(state, tailbuf, 0);
		newpage = GenericXLogRegisterBuffer(state, newbuf, GENERIC_XLOG_FULL_IMAGE);

		PageInit(newpage, BLCKSZ, GPH_SPECIAL_SIZE);
		GphPageSpecialPtr(newpage)->gph_page_type = GPH_PAGE_ADJ;
		GphPageSpecialPtr(newpage)->gph_unused = 0;
		GphPageSpecialPtr(newpage)->gph_next_pageno = InvalidBlockNumber;
		GphPageSpecialPtr(newpage)->gph_owner_vid = src;
		for (i = 0; i < fill; i++)
		{
			GphEdgeSlot	es;

			memset(&es, 0, sizeof(es));
			es.es_src_vid = src;
			es.es_dst_vid = (uint64) dsts[k + i];
			es.es_edge_type_id = GPH_EDGE_TYPE_RELATED_TO;
			es.es_flags = 0;
			es.es_xmin = xid;
			GphPageAppendRecord(newpage, &es, sizeof(GphEdgeSlot));
		}
		GphPageSpecialPtr(tailpage)->gph_next_pageno = newblk;
		vr = GphPageGetRecord(vpage, vslot, sizeof(GphVertexRecord));
		vr->vr_adj_tail = newblk;
		meta->gm_edge_count += fill;
		GenericXLogFinish(state);

		UnlockReleaseBuffer(tailbuf);	/* link durable => release old tail, advance */
		tailbuf = newbuf;
		tailblk = newblk;
		k += fill;
		appended += fill;
	}

	UnlockReleaseBuffer(tailbuf);
	UnlockReleaseBuffer(vbuf);
	UnlockReleaseBuffer(metabuf);
	relation_close(rel, RowExclusiveLock);
	PG_RETURN_INT64(appended);
}

/* ------------------------------------------------------------------ */
/* Maintenance: gph_freeze() — long-lived-store anti-wraparound gate    */
/* (advisor plan 036 / DEV-1347; docs/graph_store_freeze_design_v0.1.0.md) */
/* ------------------------------------------------------------------ */

/*
 * Freeze ONE stored inserting xid in place if it precedes `horizon`. The store keeps raw xids on
 * its pages (vr_xmin / es_xmin) with no undo, so without a freeze pass every stored xid runs two
 * clocks (clog truncation + 2^31 wraparound). This rewrites a pre-horizon xid to a PERMANENT one
 * WHILE it is still resolvable in clog:
 *   committed -> FrozenTransactionId   (gph_xmin_visible stays TRUE — TransactionIdDidCommit
 *                                       short-circuits Frozen to committed without touching clog)
 *   aborted   -> InvalidTransactionId  (gph_xmin_visible stays FALSE)
 * so visibility is byte-identical before and after: freeze is purely a storage rewrite and the
 * read path (gph_xmin_visible) is untouched. Already-permanent xids (Invalid/Frozen/Bootstrap:
 * !TransactionIdIsNormal) and post-horizon xids are left alone. Returns true iff it rewrote.
 * Caller holds the page registered in an open GenericXLog record.
 */
static bool
gph_freeze_xid(TransactionId *xid, TransactionId horizon)
{
	TransactionId x = *xid;

	if (!TransactionIdIsNormal(x))
		return false;			/* Invalid/Frozen/Bootstrap: already permanent */
	if (!TransactionIdPrecedes(x, horizon))
		return false;			/* at/after horizon: still needs its real clog entry */
	if (TransactionIdDidCommit(x))
		*xid = FrozenTransactionId;
	else
		*xid = InvalidTransactionId;	/* aborted / crashed-uncommitted => invisible */
	return true;
}

/*
 * Freeze ONE stored deleting xid (es_xmax / vr_xmax) in place if its tombstone PRECEDES `horizon`.
 * The field only carries visibility meaning while GPH_FLAG_DELETED is set (gph_deleted_visible); on
 * a live record xmax is InvalidTransactionId from the insert-path memset, so !TransactionIdIsNormal
 * short-circuits it here exactly like gph_freeze_xid — a live record's xmax is left untouched.
 *
 * For a tombstoned record whose xmax precedes horizon:
 *   committed -> FrozenTransactionId, GPH_FLAG_DELETED stays SET    (tombstone visible-deleted
 *                                                                    forever, gph_xmin_visible short-
 *                                                                    circuits Frozen to committed)
 *   aborted   -> InvalidTransactionId, GPH_FLAG_DELETED is CLEARED  (the delete never committed, so
 *                                                                    gph_deleted_visible must now see
 *                                                                    a live record — matching the
 *                                                                    in-flight-abort behavior FR-7
 *                                                                    already gives gph_xmin_visible)
 *
 * There is no "unresolved xmax below horizon" case to guard: gph_freeze()'s caller already checked
 * TransactionIdPrecedes(horizon, oldest-running-xmin) before any freezing starts, so every xid that
 * precedes horizon precedes every in-progress transaction too and is therefore already resolved
 * (committed or aborted) in clog. relfrozenxid can advance to horizon once the walk completes: any
 * xmax >= horizon is left alone here (still needs its real clog entry) and is by construction not
 * older than the horizon vac_update_relstats advances relfrozenxid to.
 *
 * Returns true iff it rewrote xmax (mirrors gph_freeze_xid's counting contract; xmax freezes are
 * folded into the same frozen-slot counter as xmin freezes — see gph_freeze_adj_chain / gph_freeze).
 */
static bool
gph_freeze_xmax(uint32 *flags, TransactionId *xmax, TransactionId horizon)
{
	TransactionId x = *xmax;

	if (!(*flags & GPH_FLAG_DELETED))
		return false;			/* live record: xmax carries no visibility meaning */
	if (!TransactionIdIsNormal(x))
		return false;			/* already permanent (re-run over an already-frozen tombstone) */
	if (!TransactionIdPrecedes(x, horizon))
		return false;			/* at/after horizon: still needs its real clog entry */
	if (TransactionIdDidCommit(x))
		*xmax = FrozenTransactionId;
	else
	{
		*flags &= ~GPH_FLAG_DELETED;	/* delete never committed: record is LIVE again */
		*xmax = InvalidTransactionId;
	}
	return true;
}

/*
 * Freeze every edge slot on ONE adjacency-page chain, one page per GenericXLog record (a chain can
 * exceed GenericXLog's 4-buffer/record cap, so pages are NOT batched). Returns the number of slots
 * frozen (es_xmin freezes + es_xmax freezes, see gph_freeze_xmax). A page with nothing to freeze is
 * GenericXLogAbort'd (no WAL churn), so a re-run over an already-frozen store is cheap (idempotency).
 */
static int64
gph_freeze_adj_chain(Relation rel, BlockNumber head, TransactionId horizon)
{
	BlockNumber	ablk = head;
	int64		frozen = 0;

	while (ablk != InvalidBlockNumber)
	{
		Buffer		abuf;
		Page		apage;
		GenericXLogState *state;
		uint32		count,
					j,
					frozen_here = 0;
		BlockNumber	next;

		CHECK_FOR_INTERRUPTS();
		abuf = ReadBufferExtended(rel, MAIN_FORKNUM, ablk, RBM_NORMAL, NULL);
		LockBuffer(abuf, BUFFER_LOCK_EXCLUSIVE);

		state = GenericXLogStart(rel);
		apage = GenericXLogRegisterBuffer(state, abuf, 0);
		count = GphPageRecordCount(apage, sizeof(GphEdgeSlot));
		for (j = 0; j < count; j++)
		{
			GphEdgeSlot *es = GphPageGetRecord(apage, j, sizeof(GphEdgeSlot));

			if (gph_freeze_xid(&es->es_xmin, horizon))
				frozen_here++;
			if (gph_freeze_xmax(&es->es_flags, &es->es_xmax, horizon))
				frozen_here++;
		}
		next = GphPageSpecialPtr(apage)->gph_next_pageno;

		if (frozen_here > 0)
			GenericXLogFinish(state);
		else
			GenericXLogAbort(state);
		UnlockReleaseBuffer(abuf);

		frozen += frozen_here;
		ablk = next;
	}
	return frozen;
}

PG_FUNCTION_INFO_V1(gph_freeze);

/*
 * gph_freeze(horizon xid) RETURNS bigint — the manual anti-wraparound freeze pass (design:
 * docs/graph_store_freeze_design_v0.1.0.md, advisor 026 + 040). Walks the vertex-page chain and, for
 * every vertex, its adjacency-page chain, rewriting each stored xid that PRECEDES `horizon` to a
 * permanent one (committed -> Frozen, aborted -> Invalid) while it is still resolvable in clog,
 * records gm_frozen_horizon on the metapage, and advances the container's relfrozenxid. This covers
 * both the inserting xid (xmin, gph_freeze_xid) and, for a tombstoned record, the deleting xid
 * (xmax, gph_freeze_xmax) — a tombstone whose xmax committed stays deleted forever, one whose xmax
 * aborted has GPH_FLAG_DELETED cleared and comes back LIVE (plan 040). Returns the number of records
 * frozen.
 *
 * WAL / atomicity: every page is rewritten under GenericXLog in the CALLER's transaction (one WAL,
 * one txn manager — golden rule 2), so a crash mid-pass replays only the completed page diffs and
 * the pass is idempotent (re-run it). The per-record rewrite rolls back with the page on ABORT just
 * like every other graph mutation, so FR-7 visibility is preserved.
 *
 * The relfrozenxid advance uses vac_update_relstats (vacuum's own in-place, only-advance path).
 * That catalog update is NON-transactional, so gph_freeze MUST be run in AUTOCOMMIT (its own
 * transaction), exactly like VACUUM: wrapping it in a BEGIN you then ROLLBACK would leave
 * relfrozenxid advanced past pages the rollback un-froze. (Restated in graph_store_am--0.1.0.sql.)
 *
 * Concurrency: ShareUpdateExclusiveLock (vacuum's level — self-exclusive so two freezes serialize;
 * readers and gph_* writers proceed). Correct under the v1 single-writer bulk-load-then-query
 * contract (graph_am.c header); the concurrent-writer interaction is argued in design §5 and gated
 * on graph_concurrency_probe, not this issue.
 *
 * SCOPE (plan 036 STOP #3 — reported honestly, not shipped as a false "safe"): this is the MANUAL
 * freeze. It disarms the forced anti-wraparound autovacuum only INDIRECTLY — by keeping
 * age(relfrozenxid) low so the forced vacuum never triggers. There is no reliable way to make the
 * forced anti-wraparound autovacuum SKIP a heap-typed relation without the full table-AM handler
 * (deferred, design §3 "Later"); the operator MUST monitor age(relfrozenxid) on gstore and run
 * gph_freeze before autovacuum_freeze_max_age.
 */
Datum
gph_freeze(PG_FUNCTION_ARGS)
{
	TransactionId horizon = DatumGetTransactionId(PG_GETARG_DATUM(0));
	Relation	rel;
	GphMeta		meta;
	TransactionId oldest;
	BlockNumber	vblk;
	int64		frozen = 0;

	if (!TransactionIdIsNormal(horizon))
		ereport(ERROR,
				(errmsg("graph_store: freeze horizon must be a normal transaction id (got %u)",
						horizon)));

	/* Serialize freezes; readers + gph_* writers proceed (design §5). */
	rel = gph_open_store(ShareUpdateExclusiveLock);

	if (RelationGetNumberOfBlocks(rel) == 0)
	{
		relation_close(rel, ShareUpdateExclusiveLock);
		PG_RETURN_INT64(0);		/* store never initialized => nothing to freeze */
	}

	/*
	 * Horizon validation: it MUST precede the cluster's oldest running xmin. Freezing an
	 * in-progress xid into FrozenTransactionId would make an uncommitted (or to-be-aborted) write
	 * permanently, falsely visible — validation makes that unreachable rather than a caller
	 * contract (design §1 "Horizon validation").
	 */
	oldest = GetOldestXmin(rel, PROCARRAY_FLAGS_VACUUM);
	if (!TransactionIdPrecedes(horizon, oldest))
		ereport(ERROR,
				(errmsg("graph_store: freeze horizon %u does not precede the oldest running xmin %u",
						horizon, oldest)));

	/* Monotonicity guard + idempotent early-out: never regress gm_frozen_horizon (design §2). */
	gph_read_meta(rel, &meta);
	if (TransactionIdIsValid(meta.gm_frozen_horizon) &&
		TransactionIdFollowsOrEquals(meta.gm_frozen_horizon, horizon))
	{
		relation_close(rel, ShareUpdateExclusiveLock);
		PG_RETURN_INT64(0);		/* already frozen at/past this horizon */
	}

	/* Walk the vertex-page chain: freeze vertex records, then descend each vertex's adj chain. */
	vblk = meta.gm_first_vertex_blk;
	while (vblk != InvalidBlockNumber)
	{
		Buffer		vbuf;
		Page		vpage;
		GenericXLogState *state;
		uint32		count,
					i,
					frozen_here = 0,
					nheads = 0;
		BlockNumber	vnext;
		BlockNumber *heads;

		CHECK_FOR_INTERRUPTS();
		vbuf = ReadBufferExtended(rel, MAIN_FORKNUM, vblk, RBM_NORMAL, NULL);
		LockBuffer(vbuf, BUFFER_LOCK_EXCLUSIVE);

		state = GenericXLogStart(rel);
		vpage = GenericXLogRegisterBuffer(state, vbuf, 0);
		count = GphPageRecordCount(vpage, sizeof(GphVertexRecord));
		heads = (BlockNumber *) palloc(sizeof(BlockNumber) * Max(count, 1));
		for (i = 0; i < count; i++)
		{
			GphVertexRecord *vr = GphPageGetRecord(vpage, i, sizeof(GphVertexRecord));

			if (gph_freeze_xid(&vr->vr_xmin, horizon))
				frozen_here++;
			if (gph_freeze_xmax(&vr->vr_flags, &vr->vr_xmax, horizon))
				frozen_here++;
			/* Descend EVERY vertex's adjacency chain — including a now-frozen aborted vertex,
			 * whose adj pages carry the SAME old xids and must be frozen too. vr_adj_head is not
			 * touched by the freeze, so reading it from the scratch page copy is exact. */
			if (vr->vr_adj_head != InvalidBlockNumber)
				heads[nheads++] = vr->vr_adj_head;
		}
		vnext = GphPageSpecialPtr(vpage)->gph_next_pageno;

		if (frozen_here > 0)
			GenericXLogFinish(state);
		else
			GenericXLogAbort(state);	/* nothing changed on this page (idempotent re-run) */
		UnlockReleaseBuffer(vbuf);

		frozen += frozen_here;

		/* Adjacency chains are walked AFTER releasing the vertex page: each page needs its own
		 * GenericXLog record and a vertex's chain can exceed the 4-buffer/record cap. */
		for (i = 0; i < nheads; i++)
			frozen += gph_freeze_adj_chain(rel, heads[i], horizon);
		pfree(heads);

		vblk = vnext;
	}

	/* Record the completed horizon in the metapage (repurposed gm_reserved slot, no layout
	 * change), under its own GenericXLog record. */
	{
		Buffer		metabuf;
		Page		metapage;
		GenericXLogState *mstate;
		GphMeta    *m;

		metabuf = ReadBufferExtended(rel, MAIN_FORKNUM, GPH_META_BLKNO, RBM_NORMAL, NULL);
		LockBuffer(metabuf, BUFFER_LOCK_EXCLUSIVE);
		mstate = GenericXLogStart(rel);
		metapage = GenericXLogRegisterBuffer(mstate, metabuf, 0);
		m = (GphMeta *) GphPageRecordBase(metapage);
		m->gm_frozen_horizon = horizon;
		GenericXLogFinish(mstate);
		UnlockReleaseBuffer(metabuf);
	}

	/*
	 * Advance the container's relfrozenxid to the horizon (as VACUUM would) — this is what actually
	 * resets age(relfrozenxid) and keeps the forced anti-wraparound autovacuum from ever triggering
	 * on gstore. vac_update_relstats only-advances (never regresses) and is non-transactional (see
	 * the autocommit note above). num_pages/num_tuples pass the current values through unchanged
	 * (the container is never planned/ANALYZEd — access is gph_* only); hasindex=false.
	 */
	vac_update_relstats(rel,
						RelationGetNumberOfBlocks(rel),
						rel->rd_rel->reltuples,
						0,
						false,
						horizon,
						InvalidMultiXactId,
						false);

	relation_close(rel, ShareUpdateExclusiveLock);
	PG_RETURN_INT64(frozen);
}

/* Mutation: tombstone (soft-delete) edge / vertex (plan 037)          */
/* ------------------------------------------------------------------ */

/*
 * gph_tombstone_adjacency — walk vertex `adj_head`'s adjacency-page chain and set GPH_FLAG_DELETED +
 * es_xmax = `xid` on every LIVE (visibly inserted, not-already-tombstoned) edge slot whose
 * es_dst_vid == `match_dst`, OR on every live slot when `match_all` is true (the vertex out-edge
 * sweep). Adjacency pages are per-vertex (gph_owner_vid == the source), so every slot on the chain
 * already has es_src_vid == the source — no src check needed. Each page that actually has a slot to
 * flip is rewritten ONCE under GenericXLog in the caller's txn (crash-safe, atomic with the host txn,
 * one WAL); pages with nothing to flip are left untouched (no WAL record), so the pass is idempotent.
 * Caller holds RowExclusiveLock on `rel`.
 */
static void
gph_tombstone_adjacency(Relation rel, BlockNumber adj_head, uint64 match_dst, bool match_all,
						TransactionId xid)
{
	BlockNumber	blk = adj_head;

	while (blk != InvalidBlockNumber)
	{
		Buffer		buf;
		Page		page;
		uint32		count,
					i;
		BlockNumber	next;
		bool		any = false;

		CHECK_FOR_INTERRUPTS();
		buf = ReadBufferExtended(rel, MAIN_FORKNUM, blk, RBM_NORMAL, NULL);
		LockBuffer(buf, BUFFER_LOCK_EXCLUSIVE);
		page = BufferGetPage(buf);
		count = GphPageRecordCount(page, sizeof(GphEdgeSlot));
		next = GphPageSpecialPtr(page)->gph_next_pageno;

		/* First pass over the pinned page: is there any live slot to flip here? */
		for (i = 0; i < count; i++)
		{
			GphEdgeSlot *s = GphPageGetRecord(page, i, sizeof(GphEdgeSlot));

			if (!gph_xmin_visible(s->es_xmin))
				continue;			/* aborted/uncommitted insert — already invisible */
			if (gph_deleted_visible(s->es_flags, s->es_xmax))
				continue;			/* already tombstoned — idempotent no-op */
			if (!match_all && s->es_dst_vid != match_dst)
				continue;
			any = true;
			break;
		}

		/* Only pages that change are WAL-logged (idempotent re-tombstone => no WAL). Apply the flag
		 * through the GenericXLog scratch page, matching the same predicate as the probe pass. */
		if (any)
		{
			GenericXLogState *state = GenericXLogStart(rel);
			Page		wpage = GenericXLogRegisterBuffer(state, buf, 0);

			for (i = 0; i < count; i++)
			{
				GphEdgeSlot *s = GphPageGetRecord(wpage, i, sizeof(GphEdgeSlot));

				if (!gph_xmin_visible(s->es_xmin))
					continue;
				if (gph_deleted_visible(s->es_flags, s->es_xmax))
					continue;
				if (!match_all && s->es_dst_vid != match_dst)
					continue;
				s->es_flags |= GPH_FLAG_DELETED;
				s->es_xmax = xid;
			}
			GenericXLogFinish(state);
		}

		UnlockReleaseBuffer(buf);
		blk = next;
	}
}

PG_FUNCTION_INFO_V1(gph_tombstone_edge);

/*
 * gph_tombstone_edge(src bigint, dst bigint) RETURNS void — soft-delete every visible src->dst
 * :related_to edge by setting GPH_FLAG_DELETED + es_xmax under GenericXLog (crash-safe, atomic with
 * the host txn; FR-7). Idempotent: tombstoning an already-deleted or absent edge (or an absent src)
 * is a no-op, not an error. The read path already filters visible tombstones (gph_deleted_visible),
 * so traversal stops emitting the edge immediately; the store-wide gm_edge_count is a raw
 * monotone counter and is deliberately NOT decremented (freeze, plans 036/040, only immortalizes or
 * resurrects the xmax stamp in place — it never reclaims the slot; physical reclamation is plan 055).
 * Owner-guarded (REVOKEd from PUBLIC, plan 026).
 */
Datum
gph_tombstone_edge(PG_FUNCTION_ARGS)
{
	uint64		src = (uint64) PG_GETARG_INT64(0);
	uint64		dst = (uint64) PG_GETARG_INT64(1);
	Relation	rel = gph_open_store(RowExclusiveLock);
	GphVertexRecord	src_rec;
	BlockNumber	vblk;
	uint32		vslot;

	/* vr_adj_head is stable once set (only vr_adj_tail moves on chaining) and the single-writer
	 * contract excludes a concurrent mutator, so the head read here drives a correct chain walk;
	 * each page is re-read under its own exclusive lock inside gph_tombstone_adjacency. */
	if (gph_locate_vertex(rel, src, &vblk, &vslot, &src_rec))
		gph_tombstone_adjacency(rel, src_rec.vr_adj_head, dst, false,
								GetCurrentTransactionId());

	relation_close(rel, RowExclusiveLock);
	PG_RETURN_VOID();
}

PG_FUNCTION_INFO_V1(gph_tombstone_vertex);

/*
 * gph_tombstone_vertex(vid bigint) RETURNS void — soft-delete a vertex: set GPH_FLAG_DELETED +
 * vr_xmax on its vertex record AND tombstone all of its OUT-edges, under GenericXLog (crash-safe,
 * atomic with the host txn; FR-7). After this the vertex is invisible as a traversal source and to
 * gph_vertex_count, and its out-edges vanish from traversal. Idempotent: no-op on an absent or
 * already-tombstoned vertex (gph_locate_vertex filters visible tombstones).
 *
 * IN-EDGES: the v1 store has no reverse (backlink) index — reverse traversal is plan 038 — so
 * in-edges pointing AT this vertex are NOT physically swept here. They remain as edge slots whose
 * dst is now a tombstoned vertex; a traversal that REACHES this vertex still yields the edge, but the
 * target reads as deleted (gph_locate_vertex / gph_vertex_count filter it). The full reverse-sweep is
 * a 038 follow-on (see plan 037 STOP condition). Owner-guarded (REVOKEd from PUBLIC, plan 026).
 */
Datum
gph_tombstone_vertex(PG_FUNCTION_ARGS)
{
	uint64		vid = (uint64) PG_GETARG_INT64(0);
	Relation	rel = gph_open_store(RowExclusiveLock);
	GphVertexRecord	src_rec;
	BlockNumber	vblk;
	uint32		vslot;
	TransactionId	xid;
	Buffer		buf;
	GenericXLogState *state;
	Page		wpage;
	GphVertexRecord *vr;

	if (!gph_locate_vertex(rel, vid, &vblk, &vslot, &src_rec))
	{
		relation_close(rel, RowExclusiveLock);
		PG_RETURN_VOID();		/* absent/already-tombstoned => no-op */
	}

	xid = GetCurrentTransactionId();

	/* 1. Tombstone the vertex record itself. Re-read under the exclusive lock; the slot index is
	 * stable (records never move — the same invariant gph_insert_edge relies on), so vslot from the
	 * released share lock in gph_locate_vertex is still valid. */
	buf = ReadBufferExtended(rel, MAIN_FORKNUM, vblk, RBM_NORMAL, NULL);
	LockBuffer(buf, BUFFER_LOCK_EXCLUSIVE);
	state = GenericXLogStart(rel);
	wpage = GenericXLogRegisterBuffer(state, buf, 0);
	vr = GphPageGetRecord(wpage, vslot, sizeof(GphVertexRecord));
	vr->vr_flags |= GPH_FLAG_DELETED;
	vr->vr_xmax = xid;
	GenericXLogFinish(state);
	UnlockReleaseBuffer(buf);	/* release before the out-edge sweep: never two buffers at once */

	/* 2. Tombstone all of the vertex's out-edges so traversal FROM it yields nothing. */
	gph_tombstone_adjacency(rel, src_rec.vr_adj_head, 0, true, xid);

	relation_close(rel, RowExclusiveLock);
	PG_RETURN_VOID();
}

/* ------------------------------------------------------------------ */
/* Traversal: Open / Next / Close incremental iterator                 */
/* ------------------------------------------------------------------ */

/*
 * GraphScanDescData — the concrete body of the opaque GraphScanDesc declared in graphstore.h.
 * Holds the cursor plus a per-scan in-memory copy of the CURRENT adjacency page's edge slots:
 * each page is read ONCE (ReadBuffer + SHARE-lock + memcpy all slots + UnlockReleaseBuffer in one
 * gs_getnext call) and its neighbors are then served one-per-Next() from page_buf, instead of
 * re-reading the page on every neighbor. It does NOT retain the container Relation or hold a
 * buffer pin across a Next() return (the page is copied to palloc'd memory and the buffer released
 * within the same call), so the iterator stays leak-free under early abandon (LIMIT) and the
 * caller manages the Relation lifetime (passed to gs_getnext).
 */
struct GraphScanDescData
{
	GraphVertexId		src;		/* source vertex of this traversal */
	GraphScanDirection	direction;	/* v1: GRAPH_SCAN_OUTGOING only */
	BlockNumber			cur_blk;	/* NEXT adjacency page to read; Invalid = no more pages */

	/*
	 * Inline filters applied per-slot in gs_getnext (plan 038) — NEVER pre-collected, so TR-1
	 * early termination is preserved (a LIMIT still stops before later chain pages are read).
	 * Defaults reproduce the pre-038 behavior byte-identically: type_filter = RELATED_TO (the old
	 * hardcoded filter), source_scope = GRAPHSTORE_INVALID_ID (unscoped, no source check).
	 */
	uint32				type_filter;	/* match es_edge_type_id; GPH_EDGE_TYPE_ANY = no type filter */
	GraphVertexId		source_scope;	/* match es_src_vid; GRAPHSTORE_INVALID_ID = unscoped */

	/*
	 * Bounded per-page in-memory slot buffer: the current page's edge slots, read once and drained
	 * one per Next(). Sized to slots_per_page (~1022 * 32B ~= 32KB); exactly one page is ever
	 * buffered (streaming). Refilled from cur_blk only when page_i reaches page_n AND another
	 * Next() is called, so a LIMIT that stops mid-page never triggers the next page's read.
	 */
	GphEdgeSlot		   *page_buf;	/* palloc'd once (slots_per_page); reused per page */
	uint32				page_n;		/* # slots currently buffered from the current page */
	uint32				page_i;		/* next buffered slot to serve */
};

/*
 * gs_open — position a traversal scan before src's first out-edge (the Open of Open/Next/Close).
 * `rel` is BORROWED (used only to locate the vertex; not retained). Returns true if `src` exists
 * (scan positioned, possibly over an empty adjacency list); false if `src` is absent. Policy is
 * the caller's: the SQL SRFs treat an absent source as an empty result, while a direct C consumer
 * (e.g. the TJS operator) may raise. v1 supports GRAPH_SCAN_OUTGOING only.
 *
 * plan 038: `type_filter` (GPH_EDGE_TYPE_ANY = any; else a dictionary type id) and `source_scope`
 * (GRAPHSTORE_INVALID_ID = unscoped; else a source vid) are threaded to gs_getnext and applied
 * inline. Callers that want the pre-038 behavior pass (GPH_EDGE_TYPE_RELATED_TO, GRAPHSTORE_INVALID_ID).
 * GRAPH_SCAN_INCOMING / GRAPH_SCAN_BOTH still raise: the adjacency list is out-edges only, so a
 * reverse (dst->src) lookup needs a new index / metapage field — deferred (see docs/decisions/0016).
 */
static bool
gs_open(GraphScanDesc *scan, Relation rel, GraphVertexId start, GraphScanDirection direction,
		uint32 type_filter, GraphVertexId source_scope)
{
	GphVertexRecord src_rec;
	BlockNumber		vblk;
	uint32			vslot;

	if (direction != GRAPH_SCAN_OUTGOING)
		ereport(ERROR,
				(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
				 errmsg("graph_store: only GRAPH_SCAN_OUTGOING is supported (got %d)",
						(int) direction),
				 errdetail("direction=in/both (getBacklinks) needs a reverse adjacency index — "
						   "a follow-on, format-touching plan (docs/decisions/0016).")));

	scan->src = start;
	scan->direction = direction;
	scan->cur_blk = InvalidBlockNumber;
	scan->type_filter = type_filter;
	scan->source_scope = source_scope;
	/* Bounded per-page slot buffer (read each page once; one page buffered = streaming). */
	scan->page_buf = (GphEdgeSlot *) palloc(sizeof(GphEdgeSlot) * GphEdgeSlotsPerPage());
	scan->page_n = 0;
	scan->page_i = 0;

	if (!gph_locate_vertex(rel, start, &vblk, &vslot, &src_rec))
		return false;			/* absent => caller decides (empty scan vs raise) */
	scan->cur_blk = src_rec.vr_adj_head;	/* Invalid if src has no edges => empty scan */
	return true;
}

/*
 * gs_read_page_into_buf — read ONE adjacency page (scan->cur_blk) exactly once into the bounded
 * per-scan page_buf: copies the page's edge slots, then IMMEDIATELY releases the buffer (no pin
 * held across Next()). Advances cur_blk to the chained next page. Increments the page-read counter
 * once (per page, not per neighbor). Returns true if a page was read (page_buf refilled), false if
 * there are no more pages. Visibility/type/delete filtering is applied later in gs_getnext, exactly
 * matching the pre-change per-slot semantics.
 */
static bool
gs_read_page_into_buf(Relation rel, GraphScanDesc *scan)
{
	Buffer		buf;
	Page		page;
	uint32		count,
				j;
	BlockNumber	next_blk;

	if (scan->cur_blk == InvalidBlockNumber)
		return false;

	CHECK_FOR_INTERRUPTS();
	buf = ReadBufferExtended(rel, MAIN_FORKNUM, scan->cur_blk, RBM_NORMAL, NULL);
	gph_page_read_counter++;	/* ONE read per adjacency page (was: one per neighbor) */
	LockBuffer(buf, BUFFER_LOCK_SHARE);
	page = BufferGetPage(buf);
	count = GphPageRecordCount(page, sizeof(GphEdgeSlot));
	next_blk = GphPageSpecialPtr(page)->gph_next_pageno;

	scan->page_n = 0;
	scan->page_i = 0;
	for (j = 0; j < count; j++)
		memcpy(&scan->page_buf[scan->page_n++],
			   GphPageGetRecord(page, j, sizeof(GphEdgeSlot)), sizeof(GphEdgeSlot));

	UnlockReleaseBuffer(buf);	/* no pin held across Next() => leak-free on LIMIT early abandon */
	scan->cur_blk = next_blk;
	return true;
}

/*
 * gs_getnext — advance the scan by ONE visible :related_to edge (the Next of Open/Next/Close).
 * Serves neighbors from the per-scan in-memory copy of the current adjacency page; reads the NEXT
 * page (once) only when the in-memory buffer is exhausted AND another Next() is requested. Holds no
 * buffer pin across calls (each page is copied to palloc'd memory and its buffer released within
 * gs_read_page_into_buf), so a LIMIT above stops before later chain pages are ever read (TR-1
 * storage-level early termination) and abandoning the scan early leaks nothing. Fills *out and
 * returns true; on exhaustion sets out->kind = GRAPH_ELEM_NONE and returns false.
 */
static bool
gs_getnext(Relation rel, GraphScanDesc *scan, GraphElement *out)
{
	out->kind = GRAPH_ELEM_NONE;

	for (;;)
	{
		/* Drain the in-memory copy of the current page. */
		while (scan->page_i < scan->page_n)
		{
			GphEdgeSlot *slot = &scan->page_buf[scan->page_i++];

			if (gph_deleted_visible(slot->es_flags, slot->es_xmax))
				continue;		/* tombstoned by a visible gph_tombstone_* (plan 037) */
			/*
			 * Typed + source-scoped filters (plan 038), applied INLINE per slot — no pre-collected
			 * neighbor set, so a LIMIT above still stops before later chain pages are read (TR-1).
			 * Defaults reproduce the old single line exactly: type_filter=RELATED_TO makes the type
			 * clause `es_edge_type_id != RELATED_TO`, and source_scope=INVALID disables the source
			 * clause. Skipped (filtered-out) slots do NOT bump gph_visit_counter — only EMITTED
			 * edges count as traversal work.
			 */
			if (scan->type_filter != GPH_EDGE_TYPE_ANY &&
				slot->es_edge_type_id != scan->type_filter)
				continue;
			if (scan->source_scope != (GraphVertexId) GRAPHSTORE_INVALID_ID &&
				slot->es_src_vid != scan->source_scope)
				continue;
			if (!gph_xmin_visible(slot->es_xmin))
				continue;		/* edge from an aborted/uncommitted txn */

			out->kind = GRAPH_ELEM_EDGE;
			out->edge_id = GRAPHSTORE_INVALID_ID;	/* v1 edge slots carry no stored id */
			out->edge_src = slot->es_src_vid;
			out->edge_dst = slot->es_dst_vid;
			out->vertex_id = slot->es_dst_vid;		/* convenience: the reached neighbor */
			out->payload = NULL;
			out->payload_len = 0;
			gph_visit_counter++;	/* one unit of traversal work, per edge emitted */
			return true;
		}

		/*
		 * Buffer exhausted. Read the next chain page once (lazily, only on the getnext call AFTER
		 * the buffer empties — so a LIMIT that stopped mid-page never reached here), or finish.
		 */
		if (!gs_read_page_into_buf(rel, scan))
			return false;
	}
}

/* gs_close — release a traversal scan (the Close of Open/Next/Close). Frees the per-page in-memory
 * slot buffer; the scan holds NO buffer pin (read-once-per-page releases each buffer immediately),
 * so early abandon under LIMIT leaks nothing. Idempotent on NULL. */
static void
gs_close(GraphScanDesc *scan)
{
	if (scan == NULL)
		return;
	if (scan->page_buf != NULL)
	{
		pfree(scan->page_buf);
		scan->page_buf = NULL;
	}
	scan->page_n = 0;
	scan->page_i = 0;
	scan->cur_blk = InvalidBlockNumber;
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
		/* Default filters (plan 038): RELATED_TO + unscoped => byte-identical to pre-038. */
		(void) gs_open(scan, rel, src, GRAPH_SCAN_OUTGOING,
					   GPH_EDGE_TYPE_RELATED_TO, (GraphVertexId) GRAPHSTORE_INVALID_ID);	/* absent src => empty (lenient SRF) */
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

/* ------------------------------------------------------------------ */
/* Fused multi-hop BFS operator (the gBrain graph-leg fast path)       */
/* ------------------------------------------------------------------ */
/*
 * gph_traverse_bfs(seed vid, max_depth, type_id) RETURNS SETOF bigint — the distinct vertices
 * reachable from `seed` within `max_depth` out-hops (excluding the seed), computed ENTIRELY in C:
 * frontier expansion + a visited hash, walking the native adjacency directly via gs_open/gs_getnext.
 * ONE call does the whole traversal — no per-node SQL SRF round-trip, no recursive-CTE join per hop.
 * This is the native counterpart to gBrain's relational recursive-CTE `traverseGraph`. `type_id` 0 =
 * any type. TR-1 is respected in spirit (bounded by max_depth; a future early-out-on-k variant is a
 * follow-on). Result is materialized once at Open, then served one vid per Next().
 */
typedef struct GphBfsResult
{
	int64	   *ids;
	int64		n;
	int64		idx;
} GphBfsResult;

PG_FUNCTION_INFO_V1(gph_traverse_bfs);
Datum
gph_traverse_bfs(PG_FUNCTION_ARGS)
{
	FuncCallContext *funcctx;
	GphBfsResult   *res;

	if (SRF_IS_FIRSTCALL())		/* === Open: run the whole BFS === */
	{
		MemoryContext oldctx;
		GraphVertexId seed = (GraphVertexId) PG_GETARG_INT64(0);
		int			max_depth = PG_GETARG_INT32(1);
		int32		arg_type = PG_GETARG_INT32(2);
		uint32		tfilter = (arg_type == 0) ? GPH_EDGE_TYPE_ANY : (uint32) arg_type;
		Relation	rel;
		HASHCTL		hctl;
		HTAB	   *visited;
		int64	   *ids;
		int64		cap = 256,
					n = 0;
		int64	   *frontier,
				   *next;
		int64		fcap = 256,
					ncap = 256,
					fn = 0,
					nn;
		int64		key;
		bool		found;
		int			d;

		funcctx = SRF_FIRSTCALL_INIT();
		oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);

		memset(&hctl, 0, sizeof(hctl));
		hctl.keysize = sizeof(int64);
		hctl.entrysize = sizeof(int64);
		visited = hash_create("gph_bfs_visited", 1024, &hctl, HASH_ELEM | HASH_BLOBS);

		ids = (int64 *) palloc(cap * sizeof(int64));
		frontier = (int64 *) palloc(fcap * sizeof(int64));
		next = (int64 *) palloc(ncap * sizeof(int64));

		key = (int64) seed;
		(void) hash_search(visited, &key, HASH_ENTER, &found);	/* mark seed; never emitted */
		frontier[fn++] = (int64) seed;

		rel = gph_open_store(AccessShareLock);
		for (d = 0; d < max_depth && fn > 0; d++)
		{
			int64		i;

			nn = 0;
			for (i = 0; i < fn; i++)
			{
				GraphScanDesc scan;
				GraphElement  elem;

				memset(&scan, 0, sizeof(scan));
				(void) gs_open(&scan, rel, (GraphVertexId) frontier[i], GRAPH_SCAN_OUTGOING,
							   tfilter, (GraphVertexId) GRAPHSTORE_INVALID_ID);
				while (gs_getnext(rel, &scan, &elem))
				{
					key = (int64) elem.edge_dst;
					(void) hash_search(visited, &key, HASH_ENTER, &found);
					if (!found)			/* first time reached: record + enqueue */
					{
						if (n == cap)
						{
							cap *= 2;
							ids = (int64 *) repalloc(ids, cap * sizeof(int64));
						}
						ids[n++] = key;
						if (nn == ncap)
						{
							ncap *= 2;
							next = (int64 *) repalloc(next, ncap * sizeof(int64));
						}
						next[nn++] = key;
					}
				}
				gs_close(&scan);
			}
			/* next becomes the frontier for the following hop */
			if (nn > fcap)
			{
				fcap = ncap;
				frontier = (int64 *) repalloc(frontier, fcap * sizeof(int64));
			}
			memcpy(frontier, next, nn * sizeof(int64));
			fn = nn;
		}
		relation_close(rel, AccessShareLock);

		res = (GphBfsResult *) palloc(sizeof(GphBfsResult));
		res->ids = ids;
		res->n = n;
		res->idx = 0;
		funcctx->user_fctx = res;
		MemoryContextSwitchTo(oldctx);
	}

	funcctx = SRF_PERCALL_SETUP();	/* === Next === */
	res = (GphBfsResult *) funcctx->user_fctx;
	if (res->idx < res->n)
		SRF_RETURN_NEXT(funcctx, Int64GetDatum(res->ids[res->idx++]));
	SRF_RETURN_DONE(funcctx);		/* === Close === */
}

/* ------------------------------------------------------------------ */
/* Backend-local reverse id cache (plan 034 / DEV-1345, PERF-03)       */
/* ------------------------------------------------------------------ */

/*
 * gph_neighbors_ext (graph_store_am--0.1.0.sql) reverse-translates every emitted neighbor vid back
 * to its external id with a correlated per-row subquery over gph_vid_map (btree + SPI, ~1us/neighbor
 * => ~2ms at fanout 2000). This backend-local hash does the SAME reverse map in ~50ns/neighbor: on
 * first probe it loads the WHOLE gph_vid_map (vid -> ext_id) once into a session-lifetime hash, then
 * every translation is an O(1) lookup. gph_neighbors_ext_cached() below is the drop-in the TJS
 * operator's reachable-set resolution (graphReachableT) probes instead of the correlated shim.
 *
 * Correctness contract (documented deliberately — plan 034 Step 2):
 *   - Freshness is guaranteed by the v1 SINGLE-WRITER, bulk-load-THEN-query contract (graph_am.c
 *     header): the map is fully populated before the first query builds the cache and is not mutated
 *     mid-query, so the cache can never serve a stale id today.
 *   - gph_vid_cache_invalidate (registered via CacheRegisterRelcacheCallback, mirroring ADR-0014's
 *     HNSW index-map eviction) flushes the whole hash whenever gph_vid_map receives a relcache
 *     invalidation (TRUNCATE / DROP / rewrite / an explicit CacheInvalidateRelcacheByRelid). This is
 *     a NO-OP under today's contract, but it is what makes the cache safe by construction once
 *     DIRECTION-04 incremental ingest lands: that writer must emit an explicit relcache invalidation
 *     on gph_vid_map after inserting a mapping (a plain heap INSERT alone does NOT invalidate the
 *     relcache), and the reader cache then rebuilds on the next probe.
 *   - Memory: one (vid, ext_id) pair per mapped vertex per backend (~16B + hash overhead). At very
 *     large V this is a real per-session cost (plan 034 STOP #3) — bound it or prefer the PERF-02
 *     identity path there; unbounded is acceptable at the benchmark's V and disclosed here.
 */
typedef struct GphVidCacheEntry
{
	int64		vid;			/* hash key: the dense v1 vid */
	int64		ext_id;			/* the mapped external id */
} GphVidCacheEntry;

static HTAB *gph_vid_cache = NULL;			/* vid -> ext_id, in CacheMemoryContext; NULL = unbuilt */
static Oid	gph_vid_map_oid = InvalidOid;	/* resolved when the cache is built */
static bool gph_vid_cache_cb_done = false;	/* relcache callback registered once per backend */

/*
 * Flush the whole reverse cache when gph_vid_map is invalidated. Registered process-lifetime, so it
 * must be cheap and must NOT ereport (it runs in invalidation-processing context). relid ==
 * InvalidOid is a global reset; otherwise flush only on our map's relid.
 */
static void
gph_vid_cache_invalidate(Datum arg, Oid relid)
{
	if (gph_vid_cache == NULL)
		return;
	if (relid != InvalidOid && relid != gph_vid_map_oid)
		return;
	hash_destroy(gph_vid_cache);	/* deletes the dynahash child context under CacheMemoryContext */
	gph_vid_cache = NULL;			/* next probe rebuilds from the live map */
}

/*
 * Build the reverse cache from a single sequential pass over gph_vid_map, into CacheMemoryContext
 * (session lifetime). No-op if already built. Uses SPI, so it must run inside a transaction — the
 * SRF's first call is. Registers the invalidation callback exactly once per backend.
 */
static void
gph_vid_cache_ensure(void)
{
	HASHCTL		ctl;
	HTAB	   *h;
	uint64		i;

	if (gph_vid_cache != NULL)
		return;

	gph_vid_map_oid = RangeVarGetRelid(makeRangeVar(GPH_SCHEMA, "gph_vid_map", -1),
									   NoLock, false);

	memset(&ctl, 0, sizeof(ctl));
	ctl.keysize = sizeof(int64);
	ctl.entrysize = sizeof(GphVidCacheEntry);
	ctl.hcxt = CacheMemoryContext;
	/*
	 * Build into a LOCAL handle and publish to gph_vid_cache only once fully populated: an
	 * invalidation that fires mid-build (SPI acquires a lock, which processes pending inval
	 * messages) sees gph_vid_cache still NULL and no-ops, so it can never hash_destroy the table
	 * out from under this loop.
	 */
	h = hash_create("graph_store vid reverse cache", 4096, &ctl,
					HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);

	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "graph_store vid cache: SPI_connect failed");
	if (SPI_execute("SELECT vid, ext_id FROM graph_store.gph_vid_map", true, 0) != SPI_OK_SELECT)
		elog(ERROR, "graph_store vid cache: gph_vid_map scan failed");

	for (i = 0; i < SPI_processed; i++)
	{
		HeapTuple	tup = SPI_tuptable->vals[i];
		TupleDesc	desc = SPI_tuptable->tupdesc;
		bool		vnull;
		bool		enull;
		int64		vid = DatumGetInt64(SPI_getbinval(tup, desc, 1, &vnull));
		int64		ext = DatumGetInt64(SPI_getbinval(tup, desc, 2, &enull));
		GphVidCacheEntry *e;
		bool		found;

		if (vnull || enull)
			continue;			/* NOT NULL columns; defensive */
		e = (GphVidCacheEntry *) hash_search(h, &vid, HASH_ENTER, &found);
		e->ext_id = ext;
	}
	SPI_finish();

	if (!gph_vid_cache_cb_done)
	{
		CacheRegisterRelcacheCallback(gph_vid_cache_invalidate, (Datum) 0);
		gph_vid_cache_cb_done = true;
	}
	gph_vid_cache = h;			/* publish the fully-built table */
}

PG_FUNCTION_INFO_V1(gph_neighbors_ext_cached);

/*
 * gph_neighbors_ext_cached(src bigint) RETURNS SETOF bigint — the cached-translation twin of the
 * SQL gph_neighbors_ext: same external-id traversal (translate src -> vid, walk the native adjacency
 * chain, translate each neighbor vid -> ext_id), same storage emission order, same lenient contract
 * (absent src => empty set; an unmapped neighbor vid => a NULL row, matching the shim's scalar
 * subquery) — but the per-neighbor reverse translation hits the backend-local hash instead of a
 * correlated btree + SPI subquery. Byte-identical to gph_neighbors_ext (parity oracle).
 */
Datum
gph_neighbors_ext_cached(PG_FUNCTION_ARGS)
{
	FuncCallContext *funcctx;
	GraphScanDesc  *scan;
	Relation		rel;
	GraphElement	elem;

	if (SRF_IS_FIRSTCALL())		/* === Open === */
	{
		MemoryContext	oldctx;
		int64			ext_src = PG_GETARG_INT64(0);
		int64			vid = 0;
		bool			have_vid = false;

		funcctx = SRF_FIRSTCALL_INIT();
		oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);

		scan = (GraphScanDesc *) palloc0(sizeof(GraphScanDesc));

		/* Warm the reverse cache (also registers the invalidation hook). */
		gph_vid_cache_ensure();

		/*
		 * Forward ext_id -> vid: ONE scalar probe per call (the reverse per-neighbor direction is
		 * what the cache accelerates, not this). ext_src is a bigint from PG_GETARG, so the %lld
		 * interpolation carries no injection risk (same pattern the TJS operator's SPI probe uses).
		 */
		if (SPI_connect() != SPI_OK_CONNECT)
			elog(ERROR, "graph_store vid cache: SPI_connect failed");
		{
			char	cmd[128];

			snprintf(cmd, sizeof(cmd),
					 "SELECT vid FROM graph_store.gph_vid_map WHERE ext_id = %lld",
					 (long long) ext_src);
			if (SPI_execute(cmd, true, 1) == SPI_OK_SELECT && SPI_processed == 1)
			{
				bool	isnull;
				int64	v = DatumGetInt64(SPI_getbinval(SPI_tuptable->vals[0],
													   SPI_tuptable->tupdesc, 1, &isnull));

				if (!isnull)
				{
					vid = v;
					have_vid = true;
				}
			}
		}
		SPI_finish();

		if (have_vid)
		{
			rel = gph_open_store(AccessShareLock);
			(void) gs_open(scan, rel, (GraphVertexId) vid, GRAPH_SCAN_OUTGOING,
						   GPH_EDGE_TYPE_RELATED_TO, (GraphVertexId) GRAPHSTORE_INVALID_ID);
			relation_close(rel, AccessShareLock);
		}
		else
		{
			/*
			 * Unknown ext_id => empty set. Leave the scan in the exhausted state gs_open sets for an
			 * absent vertex (page_buf allocated, cur_blk Invalid) so gs_getnext returns false — do
			 * NOT rely on palloc0's zeroed cur_blk (block 0 is the metapage, not "no pages").
			 */
			scan->page_buf = (GphEdgeSlot *) palloc(sizeof(GphEdgeSlot) * GphEdgeSlotsPerPage());
			scan->cur_blk = InvalidBlockNumber;
			scan->type_filter = GPH_EDGE_TYPE_RELATED_TO;
			scan->source_scope = (GraphVertexId) GRAPHSTORE_INVALID_ID;
			scan->page_n = 0;
			scan->page_i = 0;
		}

		funcctx->user_fctx = scan;
		MemoryContextSwitchTo(oldctx);
	}

	funcctx = SRF_PERCALL_SETUP();	/* === Next === */
	scan = (GraphScanDesc *) funcctx->user_fctx;

	rel = gph_open_store(AccessShareLock);
	if (gs_getnext(rel, scan, &elem))
	{
		int64		nvid = (int64) elem.edge_dst;
		GphVidCacheEntry *e;
		bool		found;

		relation_close(rel, AccessShareLock);
		gph_vid_cache_ensure();		/* re-warm if an invalidation flushed it between Next() calls */
		e = (GphVidCacheEntry *) hash_search(gph_vid_cache, &nvid, HASH_FIND, &found);
		if (found)
			SRF_RETURN_NEXT(funcctx, Int64GetDatum(e->ext_id));
		SRF_RETURN_NEXT_NULL(funcctx);	/* unmapped vid => NULL (shim parity) */
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
		/* Default filters (plan 038): RELATED_TO + unscoped => byte-identical to pre-038. */
		(void) gs_open(scan, rel, src, GRAPH_SCAN_OUTGOING,
					   GPH_EDGE_TYPE_RELATED_TO, (GraphVertexId) GRAPHSTORE_INVALID_ID);	/* absent src => empty (lenient SRF) */
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

PG_FUNCTION_INFO_V1(gph_traverse_typed);

/*
 * gph_traverse_typed(src bigint, type_id integer, direction integer, source_id bigint)
 *   RETURNS TABLE(src bigint, dst bigint) — the typed + directional + source-scoped twin of
 * gph_traverse (plan 038 / gBrain traversePaths). Same shared gs_* engine, same one-edge-per-Next()
 * TR-1 early termination; only the inline gs_getnext filters differ:
 *   - type_id     : dictionary edge type id, or 0 (GPH_EDGE_TYPE_ANY) to match any type.
 *   - direction   : 0 = out (v1); in/both raise (reverse adjacency deferred — docs/decisions/0016).
 *   - source_id   : source vid to scope to, or -1 for unscoped (-1 => (uint64) UINT64_MAX =
 *                   GRAPHSTORE_INVALID_ID). Since adjacency chains are per-vertex, es_src_vid is
 *                   uniform per scan, so this is a defensive scope assertion at the single-vertex
 *                   level; tenant-grouped (multi-vertex) scoping needs the relational vertex->source
 *                   side-table (B3), not the native slot.
 * gph_traverse_typed(src, GPH_EDGE_TYPE_RELATED_TO, 0, -1) is byte-identical to gph_traverse(src)
 * (parity oracle). Must be used in a target-list / ProjectSet position, like gph_traverse.
 */
Datum
gph_traverse_typed(PG_FUNCTION_ARGS)
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
		uint32		type_filter = (uint32) PG_GETARG_INT32(1);
		int			dir = PG_GETARG_INT32(2);
		GraphVertexId source_scope = (GraphVertexId) PG_GETARG_INT64(3);

		funcctx = SRF_FIRSTCALL_INIT();
		oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);

		if (get_call_result_type(fcinfo, NULL, &tupdesc) != TYPEFUNC_COMPOSITE)
			ereport(ERROR,
					(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					 errmsg("gph_traverse_typed must be called in a context that expects a record")));
		funcctx->tuple_desc = BlessTupleDesc(tupdesc);

		scan = (GraphScanDesc *) palloc0(sizeof(GraphScanDesc));
		rel = gph_open_store(AccessShareLock);
		/* gs_open raises on direction != OUTGOING (in/both deferred, docs/decisions/0016). */
		(void) gs_open(scan, rel, src, (GraphScanDirection) dir,
					   type_filter, source_scope);	/* absent src => empty (lenient SRF) */
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

PG_FUNCTION_INFO_V1(gph_page_reads);

/*
 * gph_page_reads() RETURNS bigint — per-backend adjacency-page-read counter (one increment per
 * adjacency page a traversal scan reads, NOT per neighbor). Backend-local + monotonic; read DELTAS
 * (v1 - v0). With the read-once scan a degree-D hub over P chained pages costs ~P page reads, not
 * ~D — this probe lets the harness measure that reduction without touching pg_statio.
 */
Datum
gph_page_reads(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT64(gph_page_read_counter);
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

			if (gph_xmin_visible(vr->vr_xmin) &&
				!gph_deleted_visible(vr->vr_flags, vr->vr_xmax))
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

/*
 * tjs_pg.c — the fused tri-modal operator re-homed on STOCK PostgreSQL 16/17 (ADR-0019,
 * roadmap D2 phase 2.5).
 *
 * tjs_open(table regclass, k int, term_cond int, m_seeds int, hops int,
 *          id_col text, filter text, query vector, src bigint, edge_type int)
 *   RETURNS SETOF bigint
 *
 * TWO PHYSICAL PATHS, selected by `src`:
 *
 *   FILTER-FIRST (src IS NOT NULL): the Gate-B winning plan behind the operator surface —
 *   one SPI statement: native typed BFS reach (graph_store.gph_traverse_bfs) joined to the
 *   table, relational filter, exact vector rank, LIMIT k. The graph + filter legs are the
 *   selective seed; the vector leg is an exact rank over the small survivor set.
 *
 *   VECTOR-FIRST / SEEDLESS (src IS NULL): the operator OWNS the index scan loop over
 *   pgvector's HNSW (hnsw.iterative_scan = relaxed_order required, pgvector >= 0.8):
 *   index_beginscan + an ORDER-BY ScanKey + index_getnext_tid stream candidates in relaxed
 *   nearest-first order; the executor's "index returned tuples in wrong order" check never
 *   applies because no executor node drives this scan (the ADR-0019 load-bearing point —
 *   this is what makes the MSVBASE executor fork unnecessary here). Per candidate: fetch the
 *   heap tuple (visibility-checked), RECOMPUTE the distance from the heap value via the
 *   pgvector distance function resolved by name (pgvector does not populate xs_orderbyvals —
 *   ADR-0015 E3 gap 1, closed), test the relational filter and (optionally, m_seeds > 0) the
 *   graph reachability predicate via cached SPI plans, and feed a bounded top-k. TR-1 early
 *   termination = ADR-0007 consecutive-drops term_cond over the recomputed distances; when it
 *   fires the scan is closed mid-stream. If the stream ends first, pgvector does NOT
 *   disclose whether its scan budget (hnsw.max_scan_tuples) or natural index exhaustion
 *   ended it — the ending is RIGHT-CENSORED and reported as such (plan 074):
 *   tjs_open_termination_reason() = 'stream_end_unknown', tjs_open_budget_capped() = SQL
 *   NULL. The boolean is never true without an observable upstream budget signal, which
 *   pgvector's iterator API does not expose today (the honest E3.3 consequence, disclosed
 *   not manufactured).
 *
 * SEEDLESS GRAPH BRIDGES (m_seeds > 0) — fork-parity semantics (plan 087): the operator
 * buffers the first seed_window = max(m_seeds*8, m_seeds+32) FILTER-PASSING stream
 * candidates and seeds from the m_seeds NEAREST within that window (the relaxed-order
 * stream is only approximately nearest-first — first-emitted is not nearest); the
 * reachable set is the union of the seeds' `hops`-bounded typed out-reach
 * (graph_store.gph_traverse_bfs via SPI, one probe per seed, cached in a per-call hash).
 * Every streamed candidate competes for the vector top-k and the TR-1 drop counter sees
 * the uniform improve-or-drop outcome (the window itself is exempt, fork phase 1/3a); a
 * reach member is ADDITIONALLY offered to a guaranteed bridge budget, and reach members
 * the stream never emitted are fetched directly by id (filter respected). At finalize the
 * reserved bridge share is capped at k/2 (min 1 when any bridge exists) — the fork's
 * rule; bridges-take-all would silently delete the vector modality on dense graphs.
 *
 * Counters (per-backend, read via SQL): tjs_open_candidates_examined() — vector-first: heap
 * tuples examined by the last call; filter-first: qualifying rows examined (post-filter,
 * BEFORE the top-k LIMIT — a count capped at k carries no information, plan 074).
 * tjs_open_termination_reason() — 'filter_first' | 'term_cond' | 'stream_end_unknown'.
 * tjs_open_budget_capped() — compat shim over the reason: false for known non-budget
 * endings, SQL NULL for 'stream_end_unknown'; never true (no observable budget signal).
 */
#include "postgres.h"

#include "access/genam.h"
#include "access/relscan.h"
#include "access/skey.h"
#include "access/stratnum.h"
#include "access/table.h"
#include "access/tableam.h"
#include "catalog/index.h"
#include "catalog/namespace.h"
#include "catalog/pg_am.h"
#include "catalog/pg_opclass.h"
#include "catalog/pg_operator.h"
#include "commands/defrem.h"
#include "catalog/pg_type.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "funcapi.h"
#include "miscadmin.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"
#include "utils/rel.h"
#include "utils/regproc.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"
#include "utils/hsearch.h"

PG_MODULE_MAGIC;

/*
 * How the last call ended (plan 074). TJS_TERM_STREAM_END_UNKNOWN is right-censored:
 * pgvector's iterator API does not say whether hnsw.max_scan_tuples or natural index
 * exhaustion ended the stream, so the operator refuses to guess.
 */
typedef enum TjsTermReason
{
	TJS_TERM_FILTER_FIRST,		/* single fused statement; no candidate stream */
	TJS_TERM_TERM_COND,			/* TR-1 consecutive-drops fired mid-stream */
	TJS_TERM_STREAM_END_UNKNOWN	/* stream ended: budget OR exhaustion, unobservable */
} TjsTermReason;

/* per-backend counters (mirror the fork's SM-3 probes) */
static int64 tjs_examined = 0;
static int64 tjs_bridges_injected = 0;
/* pre-first-call state is also unknown — the censored default, not a claim */
static TjsTermReason tjs_term_reason = TJS_TERM_STREAM_END_UNKNOWN;

/* ---------------------------------------------------------------------------------- */

typedef struct TopkItem
{
	int64		id;
	float8		dist;
} TopkItem;

/* bounded max-heap on dist: root = worst of the current top-k */
typedef struct Topk
{
	int			cap;
	int			n;
	TopkItem   *items;
} Topk;

static void
topk_init(Topk *t, int cap)
{
	t->cap = cap;
	t->n = 0;
	t->items = palloc(sizeof(TopkItem) * cap);
}

static void
topk_sift_down(Topk *t, int i)
{
	for (;;)
	{
		int			l = 2 * i + 1,
					r = 2 * i + 2,
					m = i;

		if (l < t->n && t->items[l].dist > t->items[m].dist)
			m = l;
		if (r < t->n && t->items[r].dist > t->items[m].dist)
			m = r;
		if (m == i)
			break;
		{
			TopkItem	tmp = t->items[i];

			t->items[i] = t->items[m];
			t->items[m] = tmp;
		}
		i = m;
	}
}

/* returns true iff the item entered the top-k (i.e. improved the held set) */
static bool
topk_offer(Topk *t, int64 id, float8 dist)
{
	if (t->n < t->cap)
	{
		int			i = t->n++;

		t->items[i].id = id;
		t->items[i].dist = dist;
		while (i > 0)
		{
			int			p = (i - 1) / 2;

			if (t->items[p].dist >= t->items[i].dist)
				break;
			{
				TopkItem	tmp = t->items[i];

				t->items[i] = t->items[p];
				t->items[p] = tmp;
			}
			i = p;
		}
		return true;
	}
	if (dist >= t->items[0].dist)
		return false;
	t->items[0].id = id;
	t->items[0].dist = dist;
	topk_sift_down(t, 0);
	return true;
}

static int
topk_cmp_dist(const void *a, const void *b)
{
	float8		da = ((const TopkItem *) a)->dist;
	float8		db = ((const TopkItem *) b)->dist;

	if (da < db)
		return -1;
	if (da > db)
		return 1;
	return 0;
}

/* ---------------------------------------------------------------------------------- */

/*
 * Locate `table`'s hnsw index on a vector column: returns the index oid, sets *vec_attno to
 * the INDEXED heap column and *distproc to the opclass ORDER BY distance function (resolved
 * from the index's opfamily, amopstrategy 1 == pgvector's distance operator slot). Errors if
 * no hnsw index exists.
 */
static Oid
find_hnsw_index(Relation heap, AttrNumber *vec_attno, Oid *distproc, Oid *distop_out)
{
	List	   *indexes = RelationGetIndexList(heap);
	ListCell   *lc;
	Oid			hnsw_am = get_am_oid("hnsw", true);

	if (!OidIsValid(hnsw_am))
		ereport(ERROR, (errmsg("tjs_open: access method \"hnsw\" not found — is pgvector installed?")));

	foreach(lc, indexes)
	{
		Oid			indexoid = lfirst_oid(lc);
		Relation	ind = index_open(indexoid, AccessShareLock);

		if (ind->rd_rel->relam == hnsw_am && ind->rd_index->indnatts >= 1)
		{
			Oid			opfamily = ind->rd_opfamily[0];
			Oid			opcintype = ind->rd_opcintype[0];
			Oid			distop;

			*vec_attno = ind->rd_index->indkey.values[0];
			/* strategy 1 = the distance operator the index was built for (<-> for l2_ops) */
			distop = get_opfamily_member(opfamily, opcintype, opcintype, 1);
			if (!OidIsValid(distop))
				ereport(ERROR, (errmsg("tjs_open: no strategy-1 distance operator in the index opfamily")));
			*distproc = get_opcode(distop);
			*distop_out = distop;	/* the operator OID, for rendering into SQL */
			index_close(ind, AccessShareLock);
			list_free(indexes);
			return indexoid;
		}
		index_close(ind, AccessShareLock);
	}
	list_free(indexes);
	ereport(ERROR, (errmsg("tjs_open: relation \"%s\" has no hnsw index (vector-first path needs one)",
						   RelationGetRelationName(heap))));
	return InvalidOid;			/* unreachable */
}

/* quote an identifier safely into a StringInfo-owned string */
static const char *
qident(const char *raw)
{
	return quote_identifier(raw);
}

/*
 * Reachability set for the seedless graph predicate: union of gph_traverse_bfs(seed, hops,
 * edge_type) over the seed vids, collected into a per-call int64 hash. SPI must be connected.
 */
static HTAB *
reach_create(void)
{
	HASHCTL		ctl;

	memset(&ctl, 0, sizeof(ctl));
	ctl.keysize = sizeof(int64);
	ctl.entrysize = sizeof(int64);
	ctl.hcxt = CurrentMemoryContext;
	return hash_create("tjs_open reach", 4096, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
}

static void
reach_add_from_seed(HTAB *reach, int64 seed, int hops, int edge_type)
{
	char		sql[256];
	int			rc;
	uint64		i;

	/* the fork's bridge set INCLUDES the seeds themselves (gph_traverse_bfs excludes its seed) */
	(void) hash_search(reach, &seed, HASH_ENTER, NULL);

	snprintf(sql, sizeof(sql),
			 "SELECT graph_store.gph_traverse_bfs(" INT64_FORMAT ", %d, %d)",
			 seed, hops, edge_type);
	rc = SPI_execute(sql, true, 0);
	if (rc != SPI_OK_SELECT)
		ereport(ERROR, (errmsg("tjs_open: graph reach probe failed (%d)", rc)));
	for (i = 0; i < SPI_processed; i++)
	{
		bool		isnull;
		int64		v = DatumGetInt64(SPI_getbinval(SPI_tuptable->vals[i],
													SPI_tuptable->tupdesc, 1, &isnull));

		if (!isnull)
			(void) hash_search(reach, &v, HASH_ENTER, NULL);
	}
}

/*
 * Seedless phases 2/3a (fork parity, plan 087): runs ONCE, when the seed window fills
 * or the stream ends first. Selects the m_seeds NEAREST buffered candidates as seeds
 * (the fork buffers a seed_window = m_seeds*8, floor m_seeds+32, prefix and seeds from
 * the nearest within it), expands the reach from them, then admits every buffered
 * candidate to the vector top-k exactly once (fork phase 3a: a win resets the drop run;
 * a loss does NOT count toward term_cond — the window is exempt). A buffered candidate
 * in reach is ADDITIONALLY offered to the guaranteed bridge budget and counted (the
 * fork's exhaustive by-id bridge fetch covers the same rows; here the phase-3b direct
 * fetch skips in-stream ids, so the offer happens stream-side).
 */
static void
seedless_seed_and_admit(TopkItem *seed_buf, int n_buf, int m_seeds, int hops,
						int edge_type, HTAB *reach, Topk *topk, Topk *bridge_topk,
						int *drops)
{
	int			i;
	int			n_seeds;

	/* nearest-first: seed selection and admission both use distance order */
	qsort(seed_buf, n_buf, sizeof(TopkItem), topk_cmp_dist);
	n_seeds = Min(m_seeds, n_buf);
	for (i = 0; i < n_seeds; i++)
		reach_add_from_seed(reach, seed_buf[i].id, hops, edge_type);

	for (i = 0; i < n_buf; i++)
	{
		bool		found;

		(void) hash_search(reach, &seed_buf[i].id, HASH_FIND, &found);
		if (found)
		{
			(void) topk_offer(bridge_topk, seed_buf[i].id, seed_buf[i].dist);
			tjs_bridges_injected++;
		}
		if (topk_offer(topk, seed_buf[i].id, seed_buf[i].dist))
			*drops = 0;
	}
}

/* ---------------------------------------------------------------------------------- */

PG_FUNCTION_INFO_V1(tjs_open_pg);

Datum
tjs_open_pg(PG_FUNCTION_ARGS)
{
	Oid			reloid = PG_GETARG_OID(0);
	int32		k = PG_GETARG_INT32(1);
	int32		term_cond = PG_GETARG_INT32(2);
	int32		m_seeds = PG_GETARG_INT32(3);
	int32		hops = PG_GETARG_INT32(4);
	text	   *id_col_t;
	text	   *filter_t;
	Datum		query_vec;
	bool		have_src = !PG_ARGISNULL(8);
	int64		src = have_src ? PG_GETARG_INT64(8) : 0;
	int32		edge_type = PG_ARGISNULL(9) ? 0 : PG_GETARG_INT32(9);

	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	Tuplestorestate *tupstore;
	TupleDesc	tupdesc;
	MemoryContext per_query_ctx,
				oldctx;
	char	   *id_col;
	char	   *filter;

	if (PG_ARGISNULL(0) || PG_ARGISNULL(1) || PG_ARGISNULL(2) || PG_ARGISNULL(3) ||
		PG_ARGISNULL(4) || PG_ARGISNULL(5) || PG_ARGISNULL(6) || PG_ARGISNULL(7))
		ereport(ERROR,
				(errmsg("tjs_open: args tbl, k, term_cond, m_seeds, hops, id_col, filter, "
						"query must all be non-NULL (src and edge_type may be NULL)")));

	id_col_t = PG_GETARG_TEXT_PP(5);
	filter_t = PG_GETARG_TEXT_PP(6);
	query_vec = PG_GETARG_DATUM(7);
	id_col = text_to_cstring(id_col_t);
	filter = text_to_cstring(filter_t);

	if (k <= 0 || k > 10000)
		ereport(ERROR, (errmsg("tjs_open: k must be in 1..10000 (got %d)", k)));
	if (hops < 0 || hops > 8)
		ereport(ERROR, (errmsg("tjs_open: hops must be in 0..8 (got %d)", hops)));
	if (term_cond < 0)
		ereport(ERROR, (errmsg("tjs_open: term_cond must be >= 0")));
	if (m_seeds < 0 || m_seeds > 10000)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("tjs_open: m_seeds must be in 0..10000 (got %d)", m_seeds)));

	/* materialize-mode SRF plumbing */
	if (rsinfo == NULL || !IsA(rsinfo, ReturnSetInfo))
		ereport(ERROR, (errmsg("tjs_open: set-valued function called in non-set context")));
	per_query_ctx = rsinfo->econtext->ecxt_per_query_memory;
	oldctx = MemoryContextSwitchTo(per_query_ctx);
	tupdesc = CreateTemplateTupleDesc(1);
	TupleDescInitEntry(tupdesc, (AttrNumber) 1, "id", INT8OID, -1, 0);
	tupstore = tuplestore_begin_heap(false, false, work_mem);
	rsinfo->returnMode = SFRM_Materialize;
	rsinfo->setResult = tupstore;
	rsinfo->setDesc = tupdesc;
	MemoryContextSwitchTo(oldctx);

	/* per-call metric reset: one lifecycle point, no leak into the next call */
	tjs_examined = 0;
	tjs_bridges_injected = 0;
	tjs_term_reason = TJS_TERM_STREAM_END_UNKNOWN;

	if (SPI_connect() != SPI_OK_CONNECT)
		ereport(ERROR, (errmsg("tjs_open: SPI_connect failed")));

	if (have_src)
	{
		/*
		 * FILTER-FIRST: one fused SPI statement — the Gate B plan behind the operator
		 * surface. The graph reach is the selective seed; exact rank orders survivors.
		 */
		StringInfoData q;
		int			rc;
		uint64		i;
		Oid			argtypes[1];
		Datum		values[1];
		char	   *relname = get_rel_name(reloid);
		char	   *nspname = get_namespace_name(get_rel_namespace(reloid));
		char	   *vec_col;
		Oid			distop = InvalidOid;

		/*
		 * The vector column AND its distance operator come from the table's
		 * hnsw index, same as vector-first — rank by whatever metric the index
		 * was built for (l2 <->, cosine <=>, ip <#>), never a hardcoded L2.
		 */
		{
			Relation	heap = table_open(reloid, AccessShareLock);
			AttrNumber	vattno = InvalidAttrNumber;
			Oid			dp = InvalidOid;

			(void) find_hnsw_index(heap, &vattno, &dp, &distop);
			vec_col = get_attname(reloid, vattno, false);
			table_close(heap, AccessShareLock);
		}

		/*
		 * count(*) OVER () evaluates after WHERE but before ORDER BY/LIMIT: every
		 * emitted row carries the FULL qualifying count, so tjs_examined reports the
		 * work the filter legs actually did, not min(work, k) — no second C-side
		 * materialization, ordering untouched (plan 074).
		 */
		initStringInfo(&q);
		appendStringInfo(&q,
						 "SELECT e.%s, count(*) OVER () FROM graph_store.gph_traverse_bfs(" INT64_FORMAT ", %d, %d) AS t(dst) "
						 "JOIN %s.%s e ON e.%s = t.dst WHERE e.%s <> " INT64_FORMAT,
						 qident(id_col), src, hops, edge_type,
						 qident(nspname), qident(relname), qident(id_col),
						 qident(id_col), src);
		if (filter[0] != '\0')
			appendStringInfo(&q, " AND (%s)", filter);
		/*
		 * Rank by the index's own distance operator (l2 <->, cosine <=>, ip
		 * <#>), schema-qualified via OPERATOR(nsp.op) so it resolves regardless
		 * of search_path. (format_operator() is unusable here — it emits the
		 * regoperator form `<->(vector,vector)`, which is invalid infix.)
		 */
		{
			HeapTuple	optup = SearchSysCache1(OPEROID, ObjectIdGetDatum(distop));
			Form_pg_operator opform;
			char	   *opnsp;
			char	   *opname;

			if (!HeapTupleIsValid(optup))
				ereport(ERROR, (errmsg("tjs_open: cache lookup failed for operator %u", distop)));
			opform = (Form_pg_operator) GETSTRUCT(optup);
			opnsp = get_namespace_name(opform->oprnamespace);
			opname = pstrdup(NameStr(opform->oprname));
			ReleaseSysCache(optup);
			appendStringInfo(&q, " ORDER BY e.%s OPERATOR(%s.%s) $1 LIMIT %d",
							 qident(vec_col), quote_identifier(opnsp), opname, k);
		}

		argtypes[0] = get_fn_expr_argtype(fcinfo->flinfo, 7);
		values[0] = query_vec;
		rc = SPI_execute_with_args(q.data, 1, argtypes, values, NULL, true, 0);
		if (rc != SPI_OK_SELECT)
			ereport(ERROR, (errmsg("tjs_open: filter-first statement failed (%d)", rc)));
		tjs_term_reason = TJS_TERM_FILTER_FIRST;
		if (SPI_processed > 0)
		{
			/* qualifying count rides column 2 of any returned row */
			bool		qnull;
			Datum		qd = SPI_getbinval(SPI_tuptable->vals[0],
										   SPI_tuptable->tupdesc, 2, &qnull);

			tjs_examined = qnull ? (int64) SPI_processed : DatumGetInt64(qd);
		}
		else
			tjs_examined = 0;	/* zero rows -> no window row carries the count */
		for (i = 0; i < SPI_processed; i++)
		{
			bool		isnull;
			Datum		d = SPI_getbinval(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1, &isnull);

			if (!isnull)
			{
				Datum		row[1] = {d};
				bool		nulls[1] = {false};

				tuplestore_putvalues(tupstore, tupdesc, row, nulls);
			}
		}
		SPI_finish();
		pfree(id_col);
		pfree(filter);
		return (Datum) 0;
	}

	/*
	 * VECTOR-FIRST / SEEDLESS: own the relaxed-order HNSW scan (ADR-0019 mechanisms 1-3).
	 */
	{
		const char *iter = GetConfigOption("hnsw.iterative_scan", true, false);
		Relation	heap;
		Oid			indexoid;
		AttrNumber	vec_attno = InvalidAttrNumber;
		Oid			distproc = InvalidOid;
		Oid			ignore_op = InvalidOid;		/* vector-first ranks via distfn, not a rendered operator */
		Relation	index;
		IndexScanDesc scan;
		ScanKeyData orderby;
		FmgrInfo	distfn;
		TupleTableSlot *slot;
		AttrNumber	id_attno;
		Topk		topk;
		int			drops = 0;
		bool		terminated = false;
		SPIPlanPtr	filter_plan = NULL;
		HTAB	   *reach = NULL;
		HTAB	   *seen = NULL;
		Topk		bridge_topk;
		int			seed_window = 0;
		TopkItem   *seed_buf = NULL;
		int			n_buf = 0;
		bool		seeded = false;
		ItemPointer tid;

		if (iter == NULL || strcmp(iter, "relaxed_order") != 0)
			ereport(ERROR,
					(errmsg("tjs_open: vector-first path requires hnsw.iterative_scan = relaxed_order"),
					 errhint("SET hnsw.iterative_scan = relaxed_order; (pgvector >= 0.8). "
							 "The scan budget hnsw.max_scan_tuples bounds the stream — a possibly-capped ending is disclosed "
							 "via tjs_open_termination_reason() = 'stream_end_unknown' (tjs_open_budget_capped() = NULL).")));

		heap = table_open(reloid, AccessShareLock);
		indexoid = find_hnsw_index(heap, &vec_attno, &distproc, &ignore_op);
		index = index_open(indexoid, AccessShareLock);
		fmgr_info(distproc, &distfn);

		id_attno = get_attnum(reloid, id_col);
		if (id_attno == InvalidAttrNumber)
			ereport(ERROR, (errmsg("tjs_open: id column \"%s\" not found", id_col)));

		/* relational filter as a cached parameterized plan: id = $1 AND (filter) */
		if (filter[0] != '\0')
		{
			StringInfoData fq;
			Oid			fargs[1] = {INT8OID};

			initStringInfo(&fq);
			appendStringInfo(&fq, "SELECT 1 FROM %s.%s WHERE %s = $1 AND (%s)",
							 qident(get_namespace_name(get_rel_namespace(reloid))),
							 qident(get_rel_name(reloid)), qident(id_col), filter);
			filter_plan = SPI_prepare(fq.data, 1, fargs);
			if (filter_plan == NULL)
				ereport(ERROR, (errmsg("tjs_open: filter plan preparation failed: %s", fq.data)));
		}
		if (m_seeds > 0)
		{
			reach = reach_create();
			seen = reach_create();	/* same shape: an int64 membership hash */
			topk_init(&bridge_topk, k);
			/* fork seed window: m_seeds * 8, floor m_seeds + 32 (bounded by the
			 * m_seeds <= 10000 argument guard — no second knob) */
			seed_window = m_seeds * 8;
			if (seed_window < m_seeds + 32)
				seed_window = m_seeds + 32;
			seed_buf = palloc(sizeof(TopkItem) * seed_window);
		}

		topk_init(&topk, k);

		/* ORDER BY scankey: strategy 1 distance operator vs the query vector */
		ScanKeyEntryInitialize(&orderby, SK_ORDER_BY, 1, 1, InvalidOid,
							   InvalidOid, distproc, query_vec);
		scan = index_beginscan(heap, index, GetActiveSnapshot(), 0, 1);
		index_rescan(scan, NULL, 0, &orderby, 1);
		slot = table_slot_create(heap, NULL);

		while ((tid = index_getnext_tid(scan, ForwardScanDirection)) != NULL)
		{
			Datum		idd,
					vecd;
			bool		idnull,
					vecnull;
			int64		cand;
			float8		dist;
			bool		passes = true;

			CHECK_FOR_INTERRUPTS();
			if (!index_fetch_heap(scan, slot))
				continue;		/* not visible */

			tjs_examined++;

			idd = slot_getattr(slot, id_attno, &idnull);
			vecd = slot_getattr(slot, vec_attno, &vecnull);
			if (idnull || vecnull)
				continue;
			cand = DatumGetInt64(idd);

			/* relational filter */
			if (filter_plan != NULL)
			{
				Datum		fv[1] = {Int64GetDatum(cand)};
				int			rc = SPI_execute_plan(filter_plan, fv, NULL, true, 1);

				if (rc != SPI_OK_SELECT)
					ereport(ERROR, (errmsg("tjs_open: filter probe failed (%d)", rc)));
				passes = (SPI_processed == 1);
			}
			if (!passes)
				continue;

			/* recomputed rank authority (E3 gap 1) */
			dist = DatumGetFloat8(FunctionCall2Coll(&distfn, InvalidOid, vecd, query_vec));

			/*
			 * Seedless bridge semantics (fork parity, ADR-0012 recipe B / plan 087).
			 * PHASE 1: buffer the first seed_window filter-passing candidates; seeds are
			 * the m_seeds NEAREST within the window, not the first m_seeds emitted
			 * (relaxed-order streams are only approximately nearest-first). The buffered
			 * prefix is exempt from drop accounting (fork phase 1/3a) and is admitted to
			 * the vector top-k exactly once when the window closes.
			 * PHASE 3b: a reach member is ADDITIONALLY offered to the guaranteed bridge
			 * budget, but it still competes for the vector top-k below and the drop
			 * counter sees the uniform improve-or-drop outcome — the fork admits every
			 * streamed candidate to the vector queue and never exempts bridges from
			 * termination progress.
			 */
			if (reach != NULL)
			{
				bool		dummy;
				bool		found;

				(void) hash_search(seen, &cand, HASH_ENTER, &dummy);
				if (!seeded)
				{
					seed_buf[n_buf].id = cand;
					seed_buf[n_buf].dist = dist;
					n_buf++;
					if (n_buf < seed_window)
						continue;
					seedless_seed_and_admit(seed_buf, n_buf, m_seeds, hops, edge_type,
											reach, &topk, &bridge_topk, &drops);
					seeded = true;
					continue;
				}

				(void) hash_search(reach, &cand, HASH_FIND, &found);
				if (found)
				{
					(void) topk_offer(&bridge_topk, cand, dist);
					tjs_bridges_injected++;
				}
			}

			if (topk_offer(&topk, cand, dist))
				drops = 0;
			else if (topk.n == k && term_cond > 0)
			{
				/* ADR-0007 consecutive drops: candidate did not improve a FULL top-k */
				if (++drops >= term_cond)
				{
					terminated = true;
					break;		/* TR-1 early termination: we own the loop, just stop */
				}
			}
		}

		/* stream ended inside the seed window: seed from the partial buffer (fork
		 * parity — phase 1 breaks out and proceeds with what it has) */
		if (reach != NULL && !seeded)
		{
			seedless_seed_and_admit(seed_buf, n_buf, m_seeds, hops, edge_type,
									reach, &topk, &bridge_topk, &drops);
			seeded = true;
		}

		/*
		 * Termination bookkeeping (plan 074). When term_cond fired, the ending is KNOWN
		 * and not budget-shaped. When the stream ended first, pgvector's iterator API
		 * does not say whether hnsw.max_scan_tuples or natural index exhaustion did it:
		 * the ending stays TJS_TERM_STREAM_END_UNKNOWN (the reset default) — a
		 * right-censored measurement the harness must treat as possibly budget-shaped
		 * (ADR-0015 E3.3), never a manufactured boolean.
		 */
		if (terminated)
			tjs_term_reason = TJS_TERM_TERM_COND;

		ExecDropSingleTupleTableSlot(slot);
		index_endscan(scan);
		index_close(index, AccessShareLock);
		table_close(heap, AccessShareLock);

		/*
		 * PHASE 3b (fork parity): bridges the ANN stream never emitted before termination are
		 * fetched DIRECTLY by id (filter respected) and offered to the guaranteed bridge
		 * budget — "a graph-reachable candidate is admitted even when its vector rank is past
		 * the frontier". Each direct fetch counts as examined work (the fork's work-bound
		 * counts the drain the same way).
		 */
		if (reach != NULL)
		{
			StringInfoData bq;
			SPIPlanPtr	fetch_plan;
			Oid			bargs[1] = {INT8OID};
			HASH_SEQ_STATUS hs;
			int64	   *bid;

			initStringInfo(&bq);
			appendStringInfo(&bq, "SELECT %s FROM %s.%s WHERE %s = $1",
							 qident(get_attname(reloid, vec_attno, false)),
							 qident(get_namespace_name(get_rel_namespace(reloid))),
							 qident(get_rel_name(reloid)), qident(id_col));
			if (filter[0] != '\0')
				appendStringInfo(&bq, " AND (%s)", filter);
			fetch_plan = SPI_prepare(bq.data, 1, bargs);
			if (fetch_plan == NULL)
				ereport(ERROR, (errmsg("tjs_open: bridge fetch plan failed: %s", bq.data)));

			hash_seq_init(&hs, reach);
			while ((bid = (int64 *) hash_seq_search(&hs)) != NULL)
			{
				bool		in_stream;
				Datum		fv[1];
				int			rc;

				CHECK_FOR_INTERRUPTS();
				(void) hash_search(seen, bid, HASH_FIND, &in_stream);
				if (in_stream)
					continue;	/* already offered from the stream */
				fv[0] = Int64GetDatum(*bid);
				rc = SPI_execute_plan(fetch_plan, fv, NULL, true, 1);
				if (rc != SPI_OK_SELECT)
					ereport(ERROR, (errmsg("tjs_open: bridge fetch failed (%d)", rc)));
				tjs_examined++;
				if (SPI_processed == 1)
				{
					bool		vnull;
					Datum		vd = SPI_getbinval(SPI_tuptable->vals[0],
												   SPI_tuptable->tupdesc, 1, &vnull);

					if (!vnull)
					{
						float8		bdist = DatumGetFloat8(
									FunctionCall2Coll(&distfn, InvalidOid, vd, query_vec));

						/* counted per offer, landed or not — the fork counts every
						 * materialized bridge row the same way (plan 087) */
						(void) topk_offer(&bridge_topk, *bid, bdist);
						tjs_bridges_injected++;
					}
				}
			}
		}

		/*
		 * FINALIZE (fork parity, plan 087): bridges are GUARANTEED into the final budget
		 * but their reserved share is CAPPED at k/2 (min 1 when any bridge exists) —
		 * bridges-take-all would silently delete the vector modality on dense graphs.
		 * Nearest bridges first up to the cap, vector winners fill the rest (dedup'd by
		 * id), and only if the vector winners run out do remaining bridges backfill.
		 * The merged set is emitted ascending by distance.
		 */
		{
			TopkItem   *final_items = palloc(sizeof(TopkItem) * k);
			int			n_final = 0;
			int			i,
					j;
			int			bridge_cap = k / 2;

			if (reach != NULL)
			{
				if (bridge_cap == 0 && bridge_topk.n > 0)
					bridge_cap = 1; /* min 1 when any bridge exists (fork rule) */
				qsort(bridge_topk.items, bridge_topk.n, sizeof(TopkItem), topk_cmp_dist);
				for (i = 0; i < bridge_topk.n && n_final < bridge_cap; i++)
				{
					bool		dup = false;

					for (j = 0; j < n_final; j++)
						if (final_items[j].id == bridge_topk.items[i].id)
						{
							dup = true;
							break;
						}
					if (!dup)
						final_items[n_final++] = bridge_topk.items[i];
				}
			}
			qsort(topk.items, topk.n, sizeof(TopkItem), topk_cmp_dist);
			for (i = 0; i < topk.n && n_final < k; i++)
			{
				bool		dup = false;

				for (j = 0; j < n_final; j++)
					if (final_items[j].id == topk.items[i].id)
					{
						dup = true;
						break;
					}
				if (!dup)
					final_items[n_final++] = topk.items[i];
			}
			/* backfill: only if the vector winners ran out (fork finalize step 3) */
			if (reach != NULL)
				for (i = 0; i < bridge_topk.n && n_final < k; i++)
				{
					bool		dup = false;

					for (j = 0; j < n_final; j++)
						if (final_items[j].id == bridge_topk.items[i].id)
						{
							dup = true;
							break;
						}
					if (!dup)
						final_items[n_final++] = bridge_topk.items[i];
				}
			qsort(final_items, n_final, sizeof(TopkItem), topk_cmp_dist);
			for (i = 0; i < n_final; i++)
			{
				Datum		row[1] = {Int64GetDatum(final_items[i].id)};
				bool		nulls[1] = {false};

				tuplestore_putvalues(tupstore, tupdesc, row, nulls);
			}
		}
	}

	SPI_finish();
	pfree(id_col);
	pfree(filter);
	return (Datum) 0;
}

PG_FUNCTION_INFO_V1(tjs_open_candidates_examined_pg);
Datum
tjs_open_candidates_examined_pg(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT64(tjs_examined);
}

PG_FUNCTION_INFO_V1(tjs_open_bridges_injected_pg);
Datum
tjs_open_bridges_injected_pg(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT64(tjs_bridges_injected);
}

PG_FUNCTION_INFO_V1(tjs_open_termination_reason_pg);
Datum
tjs_open_termination_reason_pg(PG_FUNCTION_ARGS)
{
	const char *reason;

	switch (tjs_term_reason)
	{
		case TJS_TERM_FILTER_FIRST:
			reason = "filter_first";
			break;
		case TJS_TERM_TERM_COND:
			reason = "term_cond";
			break;
		case TJS_TERM_STREAM_END_UNKNOWN:
		default:
			reason = "stream_end_unknown";
			break;
	}
	PG_RETURN_TEXT_P(cstring_to_text(reason));
}

/*
 * Compat shim over the termination reason: false for known non-budget endings, SQL NULL
 * when the stream ended for an unobservable reason (budget OR exhaustion — pgvector does
 * not disclose which). Never true today: there is no upstream budget signal to observe.
 */
PG_FUNCTION_INFO_V1(tjs_open_budget_capped_pg);
Datum
tjs_open_budget_capped_pg(PG_FUNCTION_ARGS)
{
	if (tjs_term_reason == TJS_TERM_STREAM_END_UNKNOWN)
		PG_RETURN_NULL();
	PG_RETURN_BOOL(false);
}

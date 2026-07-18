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
 *   FILTER-FIRST (src IS NOT NULL): the Gate-B winning plan behind the operator surface,
 *   re-cut on the bounded pull iterator (plan 077 / ADR-0020) — a budget-bounded
 *   graph_store.gph_traverse_bounded traversal (pulled via an SPI cursor, one vertex at a
 *   time), per-vertex relational filter probe, distance recompute, bounded top-k of k.
 *   term_cond = 0 (every existing caller's default) disables early termination, so an
 *   uncensored call examines the WHOLE bounded reach and is byte-identical to the pre-077
 *   fused-statement contract; term_cond > 0 additionally applies the same ADR-0007
 *   consecutive-drops rule vector-first uses below. The graph + filter legs are the
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
 * reachable set is the union of the seeds' `hops`-bounded typed out-reach, now acquired via
 * the bounded pull iterator (graph_store.gph_traverse_bounded via an SPI cursor, one probe
 * per seed, seeds visited nearest-first, sharing ONE tjs.graph_work_budget edge-step pool
 * across all seeds — plan 077 / ADR-0020 decision 2 — collected into a per-call hash).
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
 * tjs_open_graph_examined() / tjs_open_graph_censored() (plan 077 / ADR-0020) — edge-steps
 * the graph leg consumed, and whether tjs.graph_work_budget was hit before the bounded
 * reach exhausted naturally; orthogonal to the stream-termination metrics above.
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
#include "utils/hsearch.h"

PG_MODULE_MAGIC;

/*
 * tjs.graph_work_budget (plan 077 / ADR-0020): per-backend GUC, edge-steps -- the same unit
 * graph_store.gph_visits() counts. Bounds the TOTAL graph-leg work of ONE tjs_open() call
 * (shared across all seeds in seedless mode, nearest-seed-first — ADR-0020 decision 2), and by
 * construction bounds graph-leg memory too (visited/frontier entries grow only on first-visit,
 * and first-visits <= edge-steps <= budget). tjs_pg owns this knob (graph_store does not know
 * about it — gph_traverse_bounded takes budget as an explicit argument, never a shared GUC
 * read across the extension boundary). Default 65536, range 128..2^30 (ADR-0020 decision 1).
 */
static int	tjs_graph_work_budget = 65536;

/*
 * tjs.graph_scoring (ADR-0021 D1, was plan 095's opt-in spike): graph scoring on the SEEDLESS
 * path only. 'ppr' (default) runs bounded forward-push Personalized PageRank (ADR-0012 addendum,
 * Andersen-Chung-Lang FOCS'06) over the SAME gs_open/gs_getnext engine the bounded traversal is
 * built on, fusing vector-similarity with a PPR-reserve score — see graph_reach_ppr_push() below.
 * Measured to dominate membership on two independent-gold corpora (ADR-0012's 2026-07-17 and
 * 2026-07-18 addenda; see ADR-0021 for the full evidence and rationale). 'membership' is the
 * ADR-0020-ratified reachability-membership scoring — BYTE-INERT with pre-095 behavior (every
 * branch gated on graph_scoring == TJS_SCORING_PPR is skipped), the fork-parity mode (ADR-0021
 * D4) and the mode the 071 filter-first parity harness relies on. NOT a query-language parameter
 * (the ADR-0008 pinned tjs_open surface is unchanged); a documented operator setting, like
 * tjs.graph_work_budget.
 */
typedef enum TjsGraphScoring
{
	TJS_SCORING_MEMBERSHIP = 0,
	TJS_SCORING_PPR = 1
} TjsGraphScoring;

static const struct config_enum_entry tjs_graph_scoring_options[] = {
	{"membership", TJS_SCORING_MEMBERSHIP, false},
	{"ppr", TJS_SCORING_PPR, false},
	{NULL, 0, false}
};

static int	tjs_graph_scoring = TJS_SCORING_PPR;

/*
 * tjs.ppr_alpha / tjs.ppr_rmax (ADR-0021 D3): the forward-push PPR teleport probability and
 * residue-drain threshold, formerly fixed C constants (TJS_PPR_ALPHA/TJS_PPR_RMAX). Exposed as
 * PGC_USERSET GUCs so the alpha/r_max sweep ADR-0021 calls for has something to sweep. Defaults
 * (0.15, 1e-3) reproduce the host reference (bench/tjs_open_ref.py) and are the ONLY values
 * ADR-0012's two measured recall-gate addenda exercised — UNSWEPT research knobs; changing them
 * moves outside the measured evidence.
 */
static double tjs_ppr_alpha = 0.15;
static double tjs_ppr_rmax = 1e-3;

void		_PG_init(void);
void
_PG_init(void)
{
	DefineCustomIntVariable("tjs.graph_work_budget",
							"Edge-step budget for one tjs_open() call's graph leg (plan 077 / ADR-0020).",
							"Shared across all seeds in seedless mode, consumed nearest-seed-first. "
							"A call that exhausts its bounded reach within budget is exact "
							"(byte-identical to the pre-077 contract); one that hits the budget "
							"first returns a deterministic-prefix result with "
							"tjs_open_graph_censored() = true (never silently exact).",
							&tjs_graph_work_budget,
							65536,
							128,
							1073741824,	/* 2^30 */
							PGC_USERSET,
							0,
							NULL, NULL, NULL);

	DefineCustomEnumVariable("tjs.graph_scoring",
							 "Seedless graph-leg scoring: 'ppr' (default, ADR-0021) or "
							 "'membership' (ADR-0020, fork-parity mode).",
							 "'ppr' replaces the bridge-guarantee ranking input with a fused "
							 "vector-similarity + PPR-reserve score; measured to dominate "
							 "membership on two independent-gold recall gates (ADR-0021). "
							 "'membership' is byte-inert with pre-095 behavior and is the mode "
							 "the 071 filter-first parity harness and fork-parity posture rely "
							 "on (ADR-0021 D4). Filter-first scoring is unaffected by either "
							 "setting.",
							 &tjs_graph_scoring,
							 TJS_SCORING_PPR,
							 tjs_graph_scoring_options,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomRealVariable("tjs.ppr_alpha",
							 "PPR forward-push teleport probability (ADR-0021 D3, unswept research knob).",
							 "Fraction of residue banked to reserve at each pop; the rest pushes to "
							 "out-neighbors. Default 0.15 reproduces the host reference and is the "
							 "only value ADR-0012's measured recall-gate addenda exercised.",
							 &tjs_ppr_alpha,
							 0.15,
							 0.0001,
							 0.9999,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	DefineCustomRealVariable("tjs.ppr_rmax",
							 "PPR forward-push residue-drain threshold (ADR-0021 D3, unswept research knob).",
							 "A node's residue must reach this floor before it is (re-)enqueued for "
							 "a push. Default 1e-3 reproduces the host reference and is the only "
							 "value ADR-0012's measured recall-gate addenda exercised.",
							 &tjs_ppr_rmax,
							 1e-3,
							 1e-12,
							 1.0,
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);
}

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

/*
 * Graph-leg honesty counters (plan 077 / ADR-0020), orthogonal to tjs_term_reason above: the
 * graph cap is a property of the reach acquisition, not of how the candidate stream ended.
 * tjs_graph_examined: edge-steps consumed by the last call's graph leg (0 for a pure vector-
 * first call with m_seeds = 0, which never touches the graph). tjs_graph_censored: true iff
 * ANY seed's (or filter-first's single) bounded traversal hit tjs.graph_work_budget before its
 * reach exhausted -- a real boolean, never NULL, reset every call.
 */
static int64 tjs_graph_examined = 0;
static bool tjs_graph_censored = false;

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

/*
 * Ascending distance, ties broken by ascending id (plan 077 / ADR-0020 §5 pins the tie-break —
 * this qsort was tie-unstable before). Used for both the vector-first/bridge heaps and
 * filter-first's final ranking.
 */
static int
topk_cmp_dist(const void *a, const void *b)
{
	const TopkItem *ia = (const TopkItem *) a;
	const TopkItem *ib = (const TopkItem *) b;

	if (ia->dist < ib->dist)
		return -1;
	if (ia->dist > ib->dist)
		return 1;
	if (ia->id < ib->id)
		return -1;
	if (ia->id > ib->id)
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
 * ReachEntry (plan 095): the seedless graph predicate's reach set is a vid -> reserve hash.
 * `reserve` is the PPR forward-push reserve (plan 095, graph_reach_ppr_push below); it stays
 * 0.0 and unread for the whole call when tjs.graph_scoring = membership (the default), which is
 * how the default stays byte-inert -- the field's presence changes memory layout only, never a
 * membership-mode value or control-flow decision. `seen` (a separate hash, stream-membership
 * only) keeps using the same entrysize via reach_create() too; its extra bytes are unused.
 */
typedef struct ReachEntry
{
	int64		vid;			/* HASH_BLOBS key: first sizeof(int64) bytes */
	float8		reserve;
} ReachEntry;

/*
 * PprCand (plan 095): one candidate in the bounded finalize pool for the ppr-mode unified
 * ranking pass -- (id, vector distance, PPR reserve) -- so vec-sim and reserve can be
 * min-max normalized together over the WHOLE pool before any fused score is computed (the
 * FR-fusion composition bench/tjs_open_ref.py measured as the winner: score = normalized
 * vec-sim + normalized reserve). Bounded to <= topk.n + |reach| candidates, never the full
 * corpus -- see tjs_open_pg's ppr finalize branch.
 */
typedef struct PprCand
{
	int64		id;
	float8		dist;
	float8		reserve;
	bool		graph_sourced;	/* present in `reach` (vs. vector-only, reserve == 0 by construction) */
} PprCand;

/*
 * Ascending "goodness" key (score_as_dist = -fused_score), ties by ascending id -- the plan
 * 095 ppr finalize pool's comparator. PprCand's leading (id, dist) layout matches TopkItem's,
 * but a dedicated comparator over the real PprCand type avoids relying on that coincidence.
 */
static int
topk_cmp_dist_ppr_pool(const void *a, const void *b)
{
	const PprCand *ia = (const PprCand *) a;
	const PprCand *ib = (const PprCand *) b;

	if (ia->dist < ib->dist)
		return -1;
	if (ia->dist > ib->dist)
		return 1;
	if (ia->id < ib->id)
		return -1;
	if (ia->id > ib->id)
		return 1;
	return 0;
}

/*
 * Reachability set for the seedless graph predicate: union of the bounded traversal's reach
 * over the seed vids, collected into a per-call vid->reserve hash. SPI must be connected.
 */
static HTAB *
reach_create(void)
{
	HASHCTL		ctl;

	memset(&ctl, 0, sizeof(ctl));
	ctl.keysize = sizeof(int64);
	ctl.entrysize = sizeof(ReachEntry);
	ctl.hcxt = CurrentMemoryContext;
	return hash_create("tjs_open reach", 4096, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
}

/*
 * tjs_read_graph_visits / tjs_read_graph_censored -- single-row SPI probes reading the graph
 * store's per-backend counters. Small, uncached SPI_execute calls (no cursor needed: exactly
 * one row). SPI must already be connected.
 */
static int64
tjs_read_graph_visits(void)
{
	bool		isnull;
	int64		v;

	if (SPI_execute("SELECT graph_store.gph_visits()", true, 0) != SPI_OK_SELECT ||
		SPI_processed != 1)
		ereport(ERROR, (errmsg("tjs_open: graph_store.gph_visits() probe failed")));
	v = DatumGetInt64(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull));
	return isnull ? 0 : v;
}

static bool
tjs_read_graph_censored(void)
{
	bool		isnull;
	bool		v;

	if (SPI_execute("SELECT graph_store.gph_traverse_bounded_censored()", true, 0) != SPI_OK_SELECT ||
		SPI_processed != 1)
		ereport(ERROR, (errmsg("tjs_open: graph_store.gph_traverse_bounded_censored() probe failed")));
	v = DatumGetBool(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull));
	return isnull ? false : v;
}

/*
 * graph_reach_pull -- pull ONE seed's bounded reach (ADR-0020 §1) into `reach`, via an SPI
 * cursor over a bare "SELECT graph_store.gph_traverse_bounded(...)" target-list call (ADR-0005
 * cross-extension composition: SPI, never a static link across the extension boundary, and
 * never a FROM-clause FunctionScan -- fetched ONE row at a time, so the underlying pull
 * iterator's Open/Next/Close bound is preserved end to end). Replaces the old whole-BFS SPI
 * helper call (the pre-077 graph store's materializing multi-hop function, banned from this
 * operator path by the Step 5 static guard -- whole reach materialized before this function
 * ever saw a row).
 *
 * `*budget_remaining` is the shared pool (ADR-0020 decision 2: one pool, consumed
 * nearest-seed-first -- the caller already visits seeds in nearest-first order); this call
 * consumes its share and decrements the pool by the edge-steps actually spent. The seed itself
 * is ALWAYS added to `reach` (the fork's bridge set includes the seeds themselves; the bounded
 * pull traversal excludes its own seed from its output, matching the pre-077 helper), even if
 * the pool is already empty -- in that case this seed's OWN reach beyond itself contributes
 * nothing and *graph_censored is set: a deterministic prefix, disclosed, never silently exact.
 */
static void
graph_reach_pull(HTAB *reach, int64 seed, int hops, int edge_type,
				  int64 *budget_remaining, int64 *graph_examined, bool *graph_censored)
{
	Oid			argtypes[4] = {INT8OID, INT4OID, INT4OID, INT8OID};
	Datum		values[4];
	Portal		portal;
	int64		v0,
				v1;
	ReachEntry *re;
	bool		found;

	re = (ReachEntry *) hash_search(reach, &seed, HASH_ENTER, &found);
	if (!found)
		re->reserve = 0.0;

	if (*budget_remaining <= 0)
	{
		*graph_censored = true;
		return;
	}

	values[0] = Int64GetDatum(seed);
	values[1] = Int32GetDatum(hops);
	values[2] = Int32GetDatum(edge_type);
	values[3] = Int64GetDatum(*budget_remaining);

	v0 = tjs_read_graph_visits();
	portal = SPI_cursor_open_with_args(NULL,
									   "SELECT graph_store.gph_traverse_bounded($1, $2, $3, $4)",
									   4, argtypes, values, NULL, true, 0);
	for (;;)
	{
		bool		isnull;
		Datum		d;

		CHECK_FOR_INTERRUPTS();
		SPI_cursor_fetch(portal, true, 1);
		if (SPI_processed == 0)
			break;
		d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
		if (!isnull)
		{
			int64		v = DatumGetInt64(d);
			bool		vfound;
			ReachEntry *ve = (ReachEntry *) hash_search(reach, &v, HASH_ENTER, &vfound);

			if (!vfound)
				ve->reserve = 0.0;
		}
	}
	SPI_cursor_close(portal);
	v1 = tjs_read_graph_visits();

	*graph_examined += (v1 - v0);
	*budget_remaining -= (v1 - v0);
	if (*budget_remaining < 0)
		*budget_remaining = 0;
	if (tjs_read_graph_censored())
		*graph_censored = true;
}

/*
 * PPR forward-push alpha/r_max (ADR-0021 D3) now live in the tjs.ppr_alpha/tjs.ppr_rmax GUCs
 * defined in _PG_init above (formerly fixed TJS_PPR_ALPHA/TJS_PPR_RMAX constants). Their
 * defaults still mirror the host reference (bench/tjs_open_ref.py: alpha=0.15, r_max=1e-3)
 * byte-for-byte. tjs.graph_work_budget (ADR-0020) remains the one shared work bound for both
 * the membership BFS and this push.
 */

typedef struct PprQItem
{
	int64		vid;
	int32		depth;
} PprQItem;

/* residue scratch state: vid -> not-yet-pushed probability mass, drained to `reserve` (in the
 * caller's ReachEntry hash) alpha at a time as each active node is popped. */
typedef struct ResidueEntry
{
	int64		vid;
	float8		residue;
} ResidueEntry;

static void
ppr_enqueue(PprQItem **queue, int *qcap, int *qn, int64 vid, int32 depth)
{
	if (*qn == *qcap)
	{
		*qcap *= 2;
		*queue = (PprQItem *) repalloc(*queue, (*qcap) * sizeof(PprQItem));
	}
	(*queue)[*qn].vid = vid;
	(*queue)[*qn].depth = depth;
	(*qn)++;
}

/*
 * graph_reach_ppr_push -- bounded forward-push Personalized PageRank (Andersen-Chung-Lang,
 * FOCS'06; ADR-0012 addendum "Ranking") over ALL seedless seeds together, replacing
 * graph_reach_pull's per-seed membership BFS when tjs.graph_scoring = ppr. Personalization
 * mass per seed is proportional to its vector proximity (sim = 1/(1+dist)), normalized to sum
 * 1 across the n_seeds, so residue naturally merges across seeds sharing a frontier -- the
 * "seeds weighted by vector proximity" contract (plan 095), preserving 087's nearest-in-window
 * seed selection (the caller already qsorts seed_buf and passes the nearest n_seeds).
 *
 * DEVIATION (documented): queue-based FIFO local push, not the strict max-residue-first
 * priority pop the ADR-0012 addendum describes. Forward push's fixed point -- the reserve
 * split once every active node's residue has dropped below r_max -- is order-independent
 * (Andersen-Chung-Lang's local push is confluent: any sequence that keeps processing
 * above-threshold nodes converges to the same reserve/residue split). FIFO order changes only
 * the NUMBER of push operations en route to that fixed point, never its correctness, and avoids
 * a decrease-key priority queue for this spike's bounded C operator.
 *
 * `hops` bounds EXPANSION exactly like the membership path's bounded traversal: a node at
 * depth == hops still receives residue/reserve credit (it is "reached") but its own out-edges
 * are never walked (mirrors gph_traverse_bounded's `item.depth >= ctx->max_depth => never
 * expanded further`). This is depth-bounded PPR, not the unbounded original.
 *
 * Each out-edge enumerated -- via `SELECT (graph_store.gph_traverse_typed($1,$2,0,-1)).dst`, a
 * target-list (not FROM-clause) SRF call over the SAME gs_open/gs_getnext single-hop engine
 * gph_traverse_bounded is built on, pulled one row per SPI cursor fetch -- is one edge-step
 * charged against the shared *budget_remaining pool (ADR-0020 decision 2 accounting, reused
 * verbatim from graph_reach_pull). Hitting the budget mid-push stops the push (the reserve
 * state already banked is kept, never discarded) and sets *graph_censored: a disclosed partial
 * PPR, never silently exact -- same honesty contract ADR-0020 §3 pins for the membership path.
 *
 * `reach` (ReachEntry: vid, reserve) accumulates every touched vertex's alpha-reserve -- the
 * SAME HTAB shape graph_reach_pull populates for membership mode (reserve stays 0 there). State
 * is bounded by budget exactly like the membership path: residue/queue entries are created only
 * on first touch, and touches <= edge-steps <= budget (ADR-0020 §2's O(min(budget,|V|)) bound,
 * unchanged).
 */
static void
graph_reach_ppr_push(HTAB *reach, TopkItem *seed_buf, int n_seeds, int hops, int edge_type,
					  int64 *budget_remaining, int64 *graph_examined, bool *graph_censored)
{
	HASHCTL		rctl;
	HTAB	   *residue;
	PprQItem   *queue;
	int			qcap,
				qn,
				qhead;
	float8	   *simw = palloc(sizeof(float8) * Max(n_seeds, 1));
	float8		simsum = 0.0;
	int			i;

	if (n_seeds <= 0)
		return;					/* nothing to personalize on; leave reach untouched */

	memset(&rctl, 0, sizeof(rctl));
	rctl.keysize = sizeof(int64);
	rctl.entrysize = sizeof(ResidueEntry);
	rctl.hcxt = CurrentMemoryContext;
	residue = hash_create("tjs_open ppr residue", 4096, &rctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);

	qcap = 256;
	qn = 0;
	qhead = 0;
	queue = (PprQItem *) palloc(qcap * sizeof(PprQItem));

	for (i = 0; i < n_seeds; i++)
	{
		simw[i] = 1.0 / (1.0 + seed_buf[i].dist);
		simsum += simw[i];
	}
	if (simsum <= 0.0)
		simsum = 1.0;			/* defensive; dist >= 0 so simw > 0 and simsum > 0 whenever n_seeds > 0 */

	for (i = 0; i < n_seeds; i++)
	{
		int64		seed = seed_buf[i].id;
		float8		p = simw[i] / simsum;
		bool		found;
		ReachEntry *re = (ReachEntry *) hash_search(reach, &seed, HASH_ENTER, &found);
		ResidueEntry *rse;

		if (!found)
			re->reserve = 0.0;
		rse = (ResidueEntry *) hash_search(residue, &seed, HASH_ENTER, &found);
		if (!found)
			rse->residue = 0.0;
		rse->residue += p;
		if (rse->residue >= tjs_ppr_rmax)
			ppr_enqueue(&queue, &qcap, &qn, seed, 0);
	}

	while (qhead < qn)
	{
		PprQItem	item = queue[qhead++];
		bool		found;
		ResidueEntry *rse;
		ReachEntry *re;
		float8		cur_residue;
		float8		push_mass;
		int64	   *nbrs;
		int			ncap,
					ndeg;

		CHECK_FOR_INTERRUPTS();

		rse = (ResidueEntry *) hash_search(residue, &item.vid, HASH_FIND, &found);
		cur_residue = found ? rse->residue : 0.0;
		if (cur_residue < tjs_ppr_rmax)
			continue;			/* stale queue entry: already drained since it was enqueued */

		re = (ReachEntry *) hash_search(reach, &item.vid, HASH_ENTER, &found);
		if (!found)
			re->reserve = 0.0;
		re->reserve += tjs_ppr_alpha * cur_residue;
		push_mass = (1.0 - tjs_ppr_alpha) * cur_residue;
		rse->residue = 0.0;		/* drained */

		if (item.depth >= hops)
			continue;			/* reached, never expanded further (mirrors the bounded
								 * traversal's hop bound) */

		if (*budget_remaining <= 0)
		{
			*graph_censored = true;
			continue;			/* reserve already banked above; no budget to enumerate edges */
		}

		/* enumerate item.vid's out-edges: one SPI cursor fetch per edge = one edge-step. */
		ncap = 16;
		ndeg = 0;
		nbrs = palloc(sizeof(int64) * ncap);
		{
			Oid			argtypes[4] = {INT8OID, INT4OID, INT4OID, INT8OID};
			Datum		values[4];
			Portal		portal;

			values[0] = Int64GetDatum(item.vid);
			values[1] = Int32GetDatum(edge_type);
			values[2] = Int32GetDatum(0);	/* direction 0 = out */
			values[3] = Int64GetDatum((int64) -1);	/* source_id -1 = unscoped */
			portal = SPI_cursor_open_with_args(NULL,
											   "SELECT (graph_store.gph_traverse_typed($1, $2, $3, $4)).dst",
											   4, argtypes, values, NULL, true, 0);
			for (;;)
			{
				bool		isnull;
				Datum		d;

				CHECK_FOR_INTERRUPTS();
				if (*budget_remaining <= 0)
				{
					*graph_censored = true;
					break;
				}
				SPI_cursor_fetch(portal, true, 1);
				if (SPI_processed == 0)
					break;
				(*graph_examined)++;
				(*budget_remaining)--;
				d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
				if (!isnull)
				{
					if (ndeg == ncap)
					{
						ncap *= 2;
						nbrs = (int64 *) repalloc(nbrs, sizeof(int64) * ncap);
					}
					nbrs[ndeg++] = DatumGetInt64(d);
				}
			}
			SPI_cursor_close(portal);
		}

		if (ndeg > 0)
		{
			float8		share = push_mass / ndeg;
			int			j;

			for (j = 0; j < ndeg; j++)
			{
				int64		dst = nbrs[j];
				ReachEntry *dre2 = (ReachEntry *) hash_search(reach, &dst, HASH_ENTER, &found);
				ResidueEntry *dre;

				if (!found)
					dre2->reserve = 0.0;
				dre = (ResidueEntry *) hash_search(residue, &dst, HASH_ENTER, &found);
				if (!found)
					dre->residue = 0.0;
				dre->residue += share;
				if (dre->residue >= tjs_ppr_rmax)
					ppr_enqueue(&queue, &qcap, &qn, dst, item.depth + 1);
			}
		}
		/* dangling node (ndeg == 0): push_mass is discarded, standard forward-push handling --
		 * its alpha-share is already banked in re->reserve above. */
		pfree(nbrs);
	}
	pfree(queue);
	/* residue is scratch state, allocated in CurrentMemoryContext like `reach`/`seen`; freed
	 * with the surrounding per-call context, no explicit hash_destroy needed. */
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
						int *drops, int64 *budget_remaining, int64 *graph_examined,
						bool *graph_censored)
{
	int			i;
	int			n_seeds;

	/*
	 * Nearest-first: seed selection AND admission both use distance order, and the shared
	 * graph-work budget (ADR-0020 decision 2) is consumed in that same nearest-seed-first
	 * order below -- the closer a seed is, the more of the shared pool it gets first.
	 */
	qsort(seed_buf, n_buf, sizeof(TopkItem), topk_cmp_dist);
	n_seeds = Min(m_seeds, n_buf);
	for (i = 0; i < n_seeds; i++)
		graph_reach_pull(reach, seed_buf[i].id, hops, edge_type,
						  budget_remaining, graph_examined, graph_censored);

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

/*
 * PPR twin of seedless_seed_and_admit (plan 095, tjs.graph_scoring = ppr): same nearest-in-
 * window seed selection (087) and the same vector-pool admission of every buffered candidate
 * (drop-counter reset on improve -- the TR-1 termination signal is unchanged in ppr mode), but
 * the graph leg runs graph_reach_ppr_push (forward-push reserves) instead of per-seed
 * membership BFS, and does NOT offer anything to bridge_topk here -- graph-sourced ranking is
 * deferred to a single unified finalize pass (tjs_open_pg, PPR branch) so the fused score can
 * be normalized over the whole reach set once, not per-candidate as it streams in.
 */
static void
seedless_seed_and_admit_ppr(TopkItem *seed_buf, int n_buf, int m_seeds, int hops,
							int edge_type, HTAB *reach, Topk *topk, int *drops,
							int64 *budget_remaining, int64 *graph_examined,
							bool *graph_censored)
{
	int			i;
	int			n_seeds;

	qsort(seed_buf, n_buf, sizeof(TopkItem), topk_cmp_dist);
	n_seeds = Min(m_seeds, n_buf);

	graph_reach_ppr_push(reach, seed_buf, n_seeds, hops, edge_type,
						  budget_remaining, graph_examined, graph_censored);

	for (i = 0; i < n_buf; i++)
	{
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
	tjs_graph_examined = 0;
	tjs_graph_censored = false;

	if (SPI_connect() != SPI_OK_CONNECT)
		ereport(ERROR, (errmsg("tjs_open: SPI_connect failed")));

	if (have_src)
	{
		/*
		 * FILTER-FIRST (plan 077 / ADR-0020): pull traversal (graph_store.gph_traverse_bounded,
		 * ADR-0020 §1) -> per-vertex filter probe -> distance recompute (the vector-first
		 * distance machinery, reused below) -> bounded top-k of k. Replaces the old fused
		 * single-SQL statement (a FROM-clause join against the pre-077 whole-BFS helper), which
		 * paid the WHOLE bounded-depth traversal before the first row. term_cond = 0 (the meaning every
		 * existing filter-first caller already uses) disables early termination, so an
		 * uncensored call is byte-identical to the pre-077 contract: the WHOLE bounded reach is
		 * examined and ranked, exactly as before. term_cond > 0 additionally applies the SAME
		 * ADR-0007 consecutive-drops rule vector-first uses, bounding graph work independent of
		 * reach size (an approximate, disclosed tradeoff -- the same epistemic status as
		 * vector-first's own term_cond, not a fourth termination_reason value).
		 */
		int64		graph_budget_remaining = tjs_graph_work_budget;
		Relation	heap;
		AttrNumber	vec_attno = InvalidAttrNumber;
		Oid			distproc = InvalidOid;
		Oid			ignore_op = InvalidOid;
		FmgrInfo	distfn;
		char	   *vec_col;
		char	   *relname = get_rel_name(reloid);
		char	   *nspname = get_namespace_name(get_rel_namespace(reloid));
		StringInfoData fq;
		Oid			fargs[2] = {INT8OID, INT8OID};
		SPIPlanPtr	fetch_plan;
		Oid			cargtypes[4] = {INT8OID, INT4OID, INT4OID, INT8OID};
		Datum		cvalues[4];
		Portal		portal;
		Topk		topk;
		int			drops = 0;
		int64		v0,
					v1;
		int			i;

		/*
		 * The vector column AND its distance function come from the table's hnsw index, same
		 * as vector-first — rank by whatever metric the index was built for (l2, cosine, ip),
		 * never a hardcoded L2.
		 */
		heap = table_open(reloid, AccessShareLock);
		(void) find_hnsw_index(heap, &vec_attno, &distproc, &ignore_op);
		vec_col = get_attname(reloid, vec_attno, false);
		table_close(heap, AccessShareLock);
		fmgr_info(distproc, &distfn);

		/* per-candidate fetch: the vector, iff it passes the id/self/relational-filter checks
		 * the old fused statement's WHERE clause applied (id <> src is defensive: the pull
		 * traversal already excludes the seed from its own output, matching the pre-077
		 * whole-BFS helper's contract). */
		initStringInfo(&fq);
		appendStringInfo(&fq, "SELECT %s FROM %s.%s WHERE %s = $1 AND %s <> $2",
						 qident(vec_col), qident(nspname), qident(relname),
						 qident(id_col), qident(id_col));
		if (filter[0] != '\0')
			appendStringInfo(&fq, " AND (%s)", filter);
		fetch_plan = SPI_prepare(fq.data, 2, fargs);
		if (fetch_plan == NULL)
			ereport(ERROR, (errmsg("tjs_open: filter-first fetch plan failed: %s", fq.data)));

		topk_init(&topk, k);

		cvalues[0] = Int64GetDatum(src);
		cvalues[1] = Int32GetDatum(hops);
		cvalues[2] = Int32GetDatum(edge_type);
		cvalues[3] = Int64GetDatum(graph_budget_remaining);

		v0 = tjs_read_graph_visits();
		portal = SPI_cursor_open_with_args(NULL,
										   "SELECT graph_store.gph_traverse_bounded($1, $2, $3, $4)",
										   4, cargtypes, cvalues, NULL, true, 0);
		for (;;)
		{
			int64		cand;
			bool		isnull;
			Datum		d;
			Datum		fv[2];
			int			rc;

			CHECK_FOR_INTERRUPTS();
			SPI_cursor_fetch(portal, true, 1);
			if (SPI_processed == 0)
				break;			/* pull traversal exhausted (naturally or budget-capped) */
			d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
			if (isnull)
				continue;
			cand = DatumGetInt64(d);

			fv[0] = Int64GetDatum(cand);
			fv[1] = Int64GetDatum(src);
			rc = SPI_execute_plan(fetch_plan, fv, NULL, true, 1);
			if (rc != SPI_OK_SELECT)
				ereport(ERROR, (errmsg("tjs_open: filter-first fetch failed (%d)", rc)));
			if (SPI_processed != 1)
				continue;		/* relational filter (or the defensive id<>src) excluded it */

			{
				bool		vnull;
				Datum		vd = SPI_getbinval(SPI_tuptable->vals[0],
											   SPI_tuptable->tupdesc, 1, &vnull);

				if (vnull)
					continue;

				tjs_examined++;	/* a qualifying (filter-passing) reach member (plan 074) */
				{
					float8		dist = DatumGetFloat8(
									FunctionCall2Coll(&distfn, InvalidOid, vd, query_vec));

					if (topk_offer(&topk, cand, dist))
						drops = 0;
					else if (topk.n == k && term_cond > 0 && ++drops >= term_cond)
						break;	/* ADR-0007 consecutive-drops, reused for filter-first */
				}
			}
		}
		SPI_cursor_close(portal);
		v1 = tjs_read_graph_visits();
		tjs_graph_examined += (v1 - v0);
		tjs_graph_censored = tjs_read_graph_censored();
		tjs_term_reason = TJS_TERM_FILTER_FIRST;

		qsort(topk.items, topk.n, sizeof(TopkItem), topk_cmp_dist);
		for (i = 0; i < topk.n; i++)
		{
			Datum		row[1] = {Int64GetDatum(topk.items[i].id)};
			bool		nulls[1] = {false};

			tuplestore_putvalues(tupstore, tupdesc, row, nulls);
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
		int64		graph_budget_remaining = tjs_graph_work_budget;	/* plan 077: shared pool */

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
					if (tjs_graph_scoring == TJS_SCORING_PPR)
						seedless_seed_and_admit_ppr(seed_buf, n_buf, m_seeds, hops, edge_type,
													reach, &topk, &drops,
													&graph_budget_remaining, &tjs_graph_examined,
													&tjs_graph_censored);
					else
						seedless_seed_and_admit(seed_buf, n_buf, m_seeds, hops, edge_type,
												reach, &topk, &bridge_topk, &drops,
												&graph_budget_remaining, &tjs_graph_examined,
												&tjs_graph_censored);
					seeded = true;
					continue;
				}

				/*
				 * ppr mode (plan 095): graph-sourced ranking is deferred to the unified
				 * finalize pass below (a fused score needs the whole reach set's min/max
				 * to normalize against) -- no in-stream bridge_topk offer here.
				 */
				if (tjs_graph_scoring == TJS_SCORING_MEMBERSHIP)
				{
					(void) hash_search(reach, &cand, HASH_FIND, &found);
					if (found)
					{
						(void) topk_offer(&bridge_topk, cand, dist);
						tjs_bridges_injected++;
					}
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
			if (tjs_graph_scoring == TJS_SCORING_PPR)
				seedless_seed_and_admit_ppr(seed_buf, n_buf, m_seeds, hops, edge_type,
											reach, &topk, &drops,
											&graph_budget_remaining, &tjs_graph_examined,
											&tjs_graph_censored);
			else
				seedless_seed_and_admit(seed_buf, n_buf, m_seeds, hops, edge_type,
										reach, &topk, &bridge_topk, &drops,
										&graph_budget_remaining, &tjs_graph_examined,
										&tjs_graph_censored);
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

		if (tjs_graph_scoring == TJS_SCORING_PPR)
		{
			/*
			 * PPR unified finalize (plan 095): replaces membership's incremental
			 * in-stream + phase-3b bridge_topk offers (which rank purely by vector
			 * distance) with ONE bounded pass that fuses vector-similarity and PPR-
			 * reserve -- the FR-fusion composition bench/tjs_open_ref.py measured as the
			 * winner (score = min-max-normalized vec-sim + min-max-normalized reserve),
			 * reproduced here via the operator's EXISTING bounded finalize-sort
			 * machinery over <= topk.n + |reach| candidates (never the full corpus,
			 * never a global reserve-map sort) rather than the host reference's
			 * incremental two-leg NRA/FR streaming merge -- an explicit, documented
			 * deviation the plan allows ("materializing all reserves then sorting once
			 * at finalize over <=k+bridges is fine").
			 *
			 * DEVIATION from membership's two-heap (topk / bridge_topk) architecture:
			 * ranking here is a SINGLE fused score applied to every candidate that could
			 * matter (both the vector pool and every graph-reached vertex), not "vector
			 * distance for the pool, a separate criterion for bridges only" -- because
			 * emitting the FINAL order by two incompatible scales (raw distance vs a
			 * fused score) would produce a meaningless merge sort. A reach member is
			 * skipped by the fetch loop below iff it already made the bounded vector
			 * top-k (topk, capacity k) -- its distance is already known from the stream.
			 * Any reach member the top-k heap evicted or never streamed still needs its
			 * distance fetched (a bounded, disclosed re-fetch of up to |reach| rows -- the
			 * same cost class as membership's own phase-3b drain), since normalization
			 * needs the complete pool up front. The bridge-cap floor(k/2)-min-1 guarantee
			 * (087) still applies: graph-sourced candidates get first claim on up to
			 * bridge_cap final slots (by fused score), the remaining slots go to the
			 * next-best fused-score candidates of any origin.
			 */
			PprCand    *pool = palloc(sizeof(PprCand) * Max(k + 1, 16));
			int			pcap = Max(k + 1, 16);
			int			npool = 0;
			float8		mind = 0,
						maxd = 0,
						minr = 0,
						maxr = 0;
			bool		first = true;
			int			i,
						j;

			/* seed pool with the vector-pool candidates (dist already known, reserve
			 * looked up if graph-reached) */
			for (i = 0; i < topk.n; i++)
			{
				bool		found;
				ReachEntry *re = reach != NULL ?
					(ReachEntry *) hash_search(reach, &topk.items[i].id, HASH_FIND, &found) : NULL;

				if (npool == pcap)
				{
					pcap *= 2;
					pool = (PprCand *) repalloc(pool, sizeof(PprCand) * pcap);
				}
				pool[npool].id = topk.items[i].id;
				pool[npool].dist = topk.items[i].dist;
				pool[npool].reserve = (re != NULL) ? re->reserve : 0.0;
				pool[npool].graph_sourced = (re != NULL);
				npool++;
			}

			/* every reach member NOT already in the pool needs its distance fetched --
			 * bounded to |reach|, the same class of work membership's phase-3b already
			 * pays for the reach members it drains. */
			if (reach != NULL)
			{
				StringInfoData bq;
				SPIPlanPtr	fetch_plan;
				Oid			bargs[1] = {INT8OID};
				HASH_SEQ_STATUS hs;
				ReachEntry *re;

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
				while ((re = (ReachEntry *) hash_seq_search(&hs)) != NULL)
				{
					bool		already = false;
					Datum		fv[1];
					int			rc;

					CHECK_FOR_INTERRUPTS();
					for (j = 0; j < npool; j++)
						if (pool[j].id == re->vid)
						{
							already = true;
							break;
						}
					if (already)
						continue;	/* already in the pool via topk, above */

					fv[0] = Int64GetDatum(re->vid);
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

							if (npool == pcap)
							{
								pcap *= 2;
								pool = (PprCand *) repalloc(pool, sizeof(PprCand) * pcap);
							}
							pool[npool].id = re->vid;
							pool[npool].dist = bdist;
							pool[npool].reserve = re->reserve;
							pool[npool].graph_sourced = true;
							npool++;
							/* counted per materialized bridge row, landed or not (whether
							 * or not it survives finalize) -- mirrors membership's
							 * tjs_open_bridges_injected() meaning (plan 087): a fetch that
							 * the relational filter excluded or that had a null vector
							 * never became a candidate, so it is not counted. */
							tjs_bridges_injected++;
						}
					}
				}
			}

			/* min-max normalize dist and reserve over the WHOLE bounded pool */
			for (i = 0; i < npool; i++)
			{
				if (first)
				{
					mind = maxd = pool[i].dist;
					minr = maxr = pool[i].reserve;
					first = false;
				}
				else
				{
					if (pool[i].dist < mind)
						mind = pool[i].dist;
					if (pool[i].dist > maxd)
						maxd = pool[i].dist;
					if (pool[i].reserve < minr)
						minr = pool[i].reserve;
					if (pool[i].reserve > maxr)
						maxr = pool[i].reserve;
				}
			}

			/* fused score, stored as an ascending "goodness" key (-score) so topk_cmp_dist
			 * (smaller = better, ties ascending id -- the plan's "ties by (score, id)")
			 * sorts the pool correctly with no new comparator. */
			for (i = 0; i < npool; i++)
			{
				float8		vecsim_norm = (maxd > mind) ? (maxd - pool[i].dist) / (maxd - mind) : 1.0;
				float8		res_norm = (maxr > minr) ? (pool[i].reserve - minr) / (maxr - minr) : 1.0;

				pool[i].dist = -(vecsim_norm + res_norm);	/* repurpose .dist as the sort key */
			}
			qsort(pool, npool, sizeof(PprCand), topk_cmp_dist_ppr_pool);

			{
				TopkItem   *final_items = palloc(sizeof(TopkItem) * k);
				int			n_final = 0;
				int			bridge_cap = reach != NULL ? k / 2 : 0;
				bool		any_bridge = false;

				for (i = 0; i < npool; i++)
					if (pool[i].graph_sourced)
					{
						any_bridge = true;
						break;
					}
				if (bridge_cap == 0 && any_bridge)
					bridge_cap = 1;	/* min 1 when any bridge exists (087 rule, preserved) */

				/* pass 1: graph-sourced candidates first claim, up to bridge_cap slots */
				for (i = 0; i < npool && n_final < bridge_cap; i++)
					if (pool[i].graph_sourced)
					{
						final_items[n_final].id = pool[i].id;
						final_items[n_final].dist = pool[i].dist;
						n_final++;
					}
				/* pass 2: fill the rest with the next-best fused-score candidates, any origin */
				for (i = 0; i < npool && n_final < k; i++)
				{
					bool		dup = false;

					for (j = 0; j < n_final; j++)
						if (final_items[j].id == pool[i].id)
						{
							dup = true;
							break;
						}
					if (!dup)
					{
						final_items[n_final].id = pool[i].id;
						final_items[n_final].dist = pool[i].dist;
						n_final++;
					}
				}

				/*
				 * final_items is NOT already in fused-score order: pass 1 can pull a
				 * graph-sourced candidate ahead of a higher-scoring non-bridge one (that
				 * IS the bridge-cap guarantee's point), so pass 2's in-order fill from the
				 * front of `pool` again does not yield an overall-sorted sequence. Re-sort
				 * the small (<= k) merged set by the same ascending "goodness" key
				 * (score_as_dist = -fused_score, ties ascending id) before emission --
				 * mirrors the membership branch's and filter-first's own final qsort.
				 */
				qsort(final_items, n_final, sizeof(TopkItem), topk_cmp_dist);
				for (i = 0; i < n_final; i++)
				{
					Datum		row[1] = {Int64GetDatum(final_items[i].id)};
					bool		nulls[1] = {false};

					tuplestore_putvalues(tupstore, tupdesc, row, nulls);
				}
			}
		}
		else
		{
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
				ReachEntry *re;

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
				while ((re = (ReachEntry *) hash_seq_search(&hs)) != NULL)
				{
					bool		in_stream;
					Datum		fv[1];
					int			rc;

					CHECK_FOR_INTERRUPTS();
					(void) hash_search(seen, &re->vid, HASH_FIND, &in_stream);
					if (in_stream)
						continue;	/* already offered from the stream */
					fv[0] = Int64GetDatum(re->vid);
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
							(void) topk_offer(&bridge_topk, re->vid, bdist);
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

/*
 * tjs_open_graph_examined() RETURNS bigint (plan 077 / ADR-0020) -- edge-steps the last call's
 * graph leg consumed (0 for a pure vector-first call with m_seeds = 0, which never touches the
 * graph). Orthogonal to tjs_open_candidates_examined(): that counts qualifying rows against the
 * relational/vector legs; this counts graph_store.gph_visits()-unit traversal work.
 */
PG_FUNCTION_INFO_V1(tjs_open_graph_examined_pg);
Datum
tjs_open_graph_examined_pg(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT64(tjs_graph_examined);
}

/*
 * tjs_open_graph_censored() RETURNS boolean (plan 077 / ADR-0020) -- true iff the last call's
 * graph leg hit tjs.graph_work_budget before its bounded reach exhausted naturally. A REAL
 * boolean, never NULL (unlike tjs_open_budget_capped(): the graph leg's cap is a signal this
 * operator owns and can always observe, not pgvector's unobservable stream end). Orthogonal to
 * tjs_open_termination_reason(): the graph cap is a property of reach acquisition, not of how
 * the candidate stream ended, so it is never a fourth reason value.
 */
PG_FUNCTION_INFO_V1(tjs_open_graph_censored_pg);
Datum
tjs_open_graph_censored_pg(PG_FUNCTION_ARGS)
{
	PG_RETURN_BOOL(tjs_graph_censored);
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

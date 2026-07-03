/*
 * join_order.c — TriDB cross-modal join-order heuristic ("the 20%", FR-6 / DEV-1170).
 *
 * C port of src/planner/join_order_ref.py, FROZEN against
 * docs/join_order_heuristic_v0.1.0.md §10. The Python reference is the executable
 * specification; this port produces BIT-IDENTICAL decisions for every case pinned in
 * tests/test_join_order.py / test/join_order_test.sql.
 *
 * Pure arithmetic + one GUC — no MSVBASE-private symbols — so it builds as a standalone
 * in-tree PGXS extension against the fork's PG 13.4 (built/tested on the GX10).
 *
 * SCOPE (honest): this ships the FROZEN, testable decision core + GUC + a SQL surface for
 * the parity test. The planner_hook integration (doc §10.5) is DEFERRED: the TJS operator
 * (DEV-1169) is a C SRF, not a CustomScan plan node, so "rewrite the driving child" does not
 * map onto it yet. The decision functions here are exactly what that hook will call once TJS
 * grows a CustomScan form; wiring is a documented follow-on, not part of this port.
 */

#include "postgres.h"
#include "fmgr.h"
#include "utils/builtins.h"   /* cstring_to_text, text_to_cstring */
#include "utils/guc.h"
#include <string.h>           /* strcmp */

#ifdef PG_MODULE_MAGIC
PG_MODULE_MAGIC;
#endif

/* ---- frozen types (doc §10.1, §10.3) ------------------------------------- */

typedef enum TriJoinOrder
{
	FILTER_FIRST,
	VECTOR_FIRST
} TriJoinOrder;

typedef struct LegStats
{
	int64	rel_filter_matches;	/* from pg_statistic / restriction selectivity */
	int64	table_size;			/* from pg_class.reltuples                      */
	float8	avg_out_degree;		/* from graph access method metapage (EXPLAIN, v2) */
	int32	vector_topk;		/* from LIMIT clause                            */
} LegStats;

/*
 * GUC: tridb.join_order_selectivity_threshold (float8, default 0.10, range [0.0, 1.0]).
 *
 * The default 0.10 is the SAME IEEE-754 binary64 literal as the Python reference's
 * `threshold=0.10` (both round to 0x3FB999999999999A) — that identity, plus IEEE division
 * (no -ffast-math; the Makefile filters it out), is what makes "bit-identical decisions"
 * an earned claim rather than an assumption, including the boundary case selectivity == 0.10.
 */
static double tridb_join_order_threshold = 0.10;
/* advisor plan 031 (additive): cost-mode GUCs. mode owned here; cost_ratio in
 * join_order_cost.c (extern) so the cost impl and its GUC live together. */
static char *tridb_join_order_mode = NULL;
extern double tridb_join_order_cost_ratio;

/* ---- frozen decision core (bit-identical to join_order_ref.py) ----------- */

/*
 * tridb_rel_selectivity — relational_selectivity().
 * FROZEN: table_size == 0 -> 1.0 (unknown/empty table -> the safe vector_first default).
 * The numerator is cast to float8 BEFORE the divide (int64/int64 would truncate). No clamp
 * of rel_filter_matches to table_size — a stale value > table_size yields selectivity > 1.0,
 * which correctly resolves to vector_first.
 */
static float8
tridb_rel_selectivity(const LegStats *s)
{
	if (s->table_size == 0)
		return 1.0;
	return (float8) s->rel_filter_matches / (float8) s->table_size;
}

/*
 * tridb_choose_join_order — choose_order().
 * Clamp threshold to [0.0, 1.0] (defense-in-depth vs a non-GUC caller), then FILTER_FIRST iff
 * selectivity <= threshold (FROZEN: the comparison is inclusive).
 */
static TriJoinOrder
tridb_choose_join_order(const LegStats *s, float8 threshold)
{
	float8	selectivity;

	if (threshold < 0.0)
		threshold = 0.0;
	else if (threshold > 1.0)
		threshold = 1.0;

	selectivity = tridb_rel_selectivity(s);
	return (selectivity <= threshold) ? FILTER_FIRST : VECTOR_FIRST;
}

/*
 * tridb_estimate_intermediate — estimated_intermediate_rows().
 * FILTER_FIRST -> min(rel_filter_matches, vector_topk); VECTOR_FIRST -> vector_topk * 50
 * (the v1 over-fetch placeholder). vector_topk is int32 — promoted to int64 before both the
 * min() and the *50 so a large topk cannot overflow.
 */
static int64
tridb_estimate_intermediate(const LegStats *s, TriJoinOrder order)
{
	/* Negative rel_filter_matches / vector_topk are NOT guarded — the Python reference doesn't
	 * guard them either, and a frozen port must match it, not "helpfully" diverge. Catalog/LIMIT
	 * inputs are non-negative in practice; a negative would propagate to a negative estimate. */
	int64	topk = (int64) s->vector_topk;

	switch (order)
	{
		case FILTER_FIRST:
			return (s->rel_filter_matches < topk) ? s->rel_filter_matches : topk;
		case VECTOR_FIRST:
			return topk * 50;
		default:
			ereport(ERROR,
					(errcode(ERRCODE_INTERNAL_ERROR),
					 errmsg("tridb_estimate_intermediate: unknown TriJoinOrder %d",
							(int) order)));
			return 0;			/* unreachable — keeps the compiler happy */
	}
}

/* ---- SQL surface (test/EXPLAIN parity; not the planner hot path) ---------- */

PG_FUNCTION_INFO_V1(tridb_rel_selectivity_sql);
Datum
tridb_rel_selectivity_sql(PG_FUNCTION_ARGS)
{
	LegStats	s;

	s.rel_filter_matches = PG_GETARG_INT64(0);
	s.table_size = PG_GETARG_INT64(1);
	s.avg_out_degree = 0.0;
	s.vector_topk = 0;
	PG_RETURN_FLOAT8(tridb_rel_selectivity(&s));
}

/*
 * tridb_choose_join_order(rel_filter_matches, table_size, threshold) -> text.
 * NOT STRICT: a NULL threshold means "use the GUC" (the planner-time default). matches/table_size
 * are expected non-NULL (the test never passes NULL there).
 */
PG_FUNCTION_INFO_V1(tridb_choose_join_order_sql);
Datum
tridb_choose_join_order_sql(PG_FUNCTION_ARGS)
{
	LegStats		s;
	float8			threshold;
	TriJoinOrder	order;

	/* NOT STRICT (so a NULL threshold can mean "use the GUC"), so matches/table_size can arrive
	 * NULL from any caller (e.g. a left-join result). PG_GETARG_INT64 on a NULL datum is UB —
	 * guard explicitly rather than rely on "the test never does it" (Linus review). */
	if (PG_ARGISNULL(0) || PG_ARGISNULL(1))
		ereport(ERROR,
				(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
				 errmsg("tridb_choose_join_order: rel_filter_matches and table_size must not be NULL")));

	s.rel_filter_matches = PG_GETARG_INT64(0);
	s.table_size = PG_GETARG_INT64(1);
	s.avg_out_degree = 0.0;
	s.vector_topk = 0;

	threshold = PG_ARGISNULL(2) ? tridb_join_order_threshold : PG_GETARG_FLOAT8(2);

	order = tridb_choose_join_order(&s, threshold);
	PG_RETURN_TEXT_P(cstring_to_text(order == FILTER_FIRST ? "filter_first"
													       : "vector_first"));
}

PG_FUNCTION_INFO_V1(tridb_estimate_intermediate_sql);
Datum
tridb_estimate_intermediate_sql(PG_FUNCTION_ARGS)
{
	LegStats		s;
	char		   *order_s = text_to_cstring(PG_GETARG_TEXT_PP(2));
	TriJoinOrder	order;

	s.rel_filter_matches = PG_GETARG_INT64(0);
	s.table_size = 0;
	s.avg_out_degree = 0.0;
	s.vector_topk = PG_GETARG_INT32(1);

	if (strcmp(order_s, "filter_first") == 0)
		order = FILTER_FIRST;
	else if (strcmp(order_s, "vector_first") == 0)
		order = VECTOR_FIRST;
	else
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("unknown join order \"%s\" (expected filter_first | vector_first)",
						order_s)));

	PG_RETURN_INT64(tridb_estimate_intermediate(&s, order));
}

/* ---- GUC registration ---------------------------------------------------- */

void _PG_init(void);
void
_PG_init(void)
{
	DefineCustomRealVariable("tridb.join_order_selectivity_threshold",
							 "Selectivity threshold for cross-modal join ordering.",
							 "selectivity <= threshold chooses filter_first; otherwise vector_first.",
							 &tridb_join_order_threshold,
							 0.10,	/* boot */
							 0.0, 1.0,	/* min, max — PG rejects out-of-range SETs */
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);

	/*
	 * advisor plan 031 (additive; frozen core untouched). Two GUCs for the cost-based
	 * decision path (join_order_cost.c). Default mode 'threshold' -> ZERO behavior change;
	 * the lowering only calls the cost function when mode = 'cost'.
	 */
	DefineCustomStringVariable("tridb.join_order_mode",
							   "Cross-modal join-order decision mode: 'threshold' or 'cost'.",
							   "'threshold' (default) = frozen relational-selectivity rule; "
							   "'cost' = graph-leg-aware two-cost comparison (join_order_cost.c).",
							   &tridb_join_order_mode,
							   "threshold",
							   PGC_USERSET,
							   0,
							   NULL, NULL, NULL);

	DefineCustomRealVariable("tridb.join_order_cost_ratio",
							 "Cost ratio a_vf/a_ff for the cost-based join-order decision.",
							 "vector-first per-candidate cost over filter-first per-drained-row; "
							 "calibrated 4.0 from the 1M GX10 point. Only used when mode = 'cost'.",
							 &tridb_join_order_cost_ratio,
							 4.0,	/* boot */
							 0.0, 1e9,	/* min, max */
							 PGC_USERSET,
							 0,
							 NULL, NULL, NULL);
}

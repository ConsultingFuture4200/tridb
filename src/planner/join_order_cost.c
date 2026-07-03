/*
 * join_order_cost.c — TriDB FR-6 cost-based join-order decision (advisor plan 031).
 *
 * ADDITIVE to the FROZEN decision core (join_order.c): this does NOT touch
 * tridb_rel_selectivity / tridb_choose_join_order / tridb_estimate_intermediate, which stay
 * bit-identical to join_order_ref.py. It adds a SECOND decision path behind the
 * `tridb.join_order_mode` GUC (default 'threshold' -> zero behavior change; 'cost' -> this).
 *
 * WHY (landscape review F4): the frozen threshold decides on RELATIONAL selectivity alone
 * (rel_matches/table_size) and is blind to the graph leg. At 1M with a broad ts window
 * (rel_sel ~0.6) but a tiny reachable set (deg=2000 of 1M), the JOINT selectivity is ~0.0012 —
 * filter-first is 36x faster — yet the threshold (0.6 > 0.10) picks vector_first. Conversely a
 * selective ts window over a mega-hub src would pick filter_first and drain an enormous set.
 * The fix: price both physical bodies with the graph-leg cardinality (deg) in the estimate.
 *
 * MODEL (docs/join_order_cost_model_v0.1.0.md). Filter-first drains reachable(src) ∩ filter =
 * ~deg*rel_sel rows, each an exact dim-distance. Vector-first examines ~k/joint_sel candidates
 * to fill the top-k, each a dim-distance PLUS an HNSW step + graph-membership probe — costlier
 * per candidate by a ratio R (a_vf/a_ff). Both pay the same per-row `dim` factor, so it cancels
 * in the comparison. Choose filter_first iff  drain  <  R * examined.
 *   joint_sel = rel_sel * (deg / table_size);  examined = min(k/joint_sel, table_size).
 * R is the one empirical constant (GUC tridb.join_order_cost_ratio, default 4.0), calibrated
 * from the 1M GX10 point (vector-first ~17us/candidate vs filter-first ~3.9us/drained-row).
 *
 * Reproduces (docs, §2): 1M(deg2000,rel.6,N1e6,k5)->filter; mega-hub(deg500k)->vector;
 * 2k-selective(deg4,rel.01)->filter; 2k-broad(deg4,rel.8)->filter (deg is tiny -> the drain is
 * trivial regardless of window breadth, which the threshold cannot see).
 */

#include "postgres.h"
#include "fmgr.h"
#include "utils/builtins.h"   /* cstring_to_text */
#include "utils/guc.h"

/* The cost ratio a_vf/a_ff. Registered in join_order.c's single _PG_init (one per module). */
double tridb_join_order_cost_ratio = 4.0;

/*
 * The cost-based decision. Pure arithmetic; NULL-safe at the SQL wrapper. Returns 1 for
 * filter_first, 0 for vector_first (the wrapper maps to text). Guards:
 *   - deg <= 0 (no graph leg / src unmapped) or table_size <= 0 (unknown) -> vector_first,
 *     the same safe default the frozen core uses for an unknown table.
 *   - rel_sel clamped to [0,1] (stale stats can exceed 1.0).
 *   - joint_sel ~ 0 -> vector-first cannot fill k; examined bounded by the corpus (worst case).
 */
int tridb_choose_join_order_cost(int64 deg, int64 rel_matches, int64 table_size,
								 int32 k, int32 term_cond);
int
tridb_choose_join_order_cost(int64 deg, int64 rel_matches, int64 table_size,
							 int32 k, int32 term_cond)
{
	double		rel_sel,
				joint_sel,
				drain_rows,
				examined;
	const double eps = 1e-12;

	(void) term_cond;			/* bounds `examined`; folded into the table_size clamp below */

	if (deg <= 0 || table_size <= 0 || k <= 0)
		return 0;				/* vector_first — no graph leg / unknown (frozen safe default) */

	rel_sel = (double) rel_matches / (double) table_size;
	if (rel_sel < 0.0)
		rel_sel = 0.0;
	if (rel_sel > 1.0)
		rel_sel = 1.0;			/* stale stats: matches > table_size */

	drain_rows = (double) deg * rel_sel;

	joint_sel = rel_sel * ((double) deg / (double) table_size);
	if (joint_sel < eps)
		examined = (double) table_size;
	else
	{
		examined = (double) k / joint_sel;
		if (examined > (double) table_size)
			examined = (double) table_size;
	}

	return (drain_rows < tridb_join_order_cost_ratio * examined) ? 1 : 0;
}

/*
 * SQL: tridb_choose_join_order_cost(deg bigint, rel_filter_matches bigint, table_size bigint,
 *                                   vector_topk int, term_cond int) RETURNS text
 * ('filter_first' | 'vector_first'). NOT STRICT is unnecessary — all args are required; declare
 * STRICT in SQL so a NULL argument yields NULL rather than reaching this code.
 */
PG_FUNCTION_INFO_V1(tridb_choose_join_order_cost_sql);
Datum
tridb_choose_join_order_cost_sql(PG_FUNCTION_ARGS)
{
	int64		deg = PG_GETARG_INT64(0);
	int64		rel_matches = PG_GETARG_INT64(1);
	int64		table_size = PG_GETARG_INT64(2);
	int32		k = PG_GETARG_INT32(3);
	int32		term_cond = PG_GETARG_INT32(4);
	int			ff = tridb_choose_join_order_cost(deg, rel_matches, table_size, k, term_cond);

	PG_RETURN_TEXT_P(cstring_to_text(ff ? "filter_first" : "vector_first"));
}

/*
 * join_order_legstats.c — build a FROZEN LegStats from the Postgres catalog (FR-6 / DEV-1285).
 *
 * STATUS: UNBUILT-HERE (GX10-gated); DRAFT FOR REVIEW.
 *
 * SCOPE (honest, narrow on purpose). This is the SAFE, additive half of DEV-1285: a standalone
 * helper that reads pg_class.reltuples off an open Relation and assembles the LegStats struct the
 * shipped decision core (src/planner/join_order.c) consumes. It is exactly the input that the
 * Option-B lowering (docs/decisions/0011-tjs-join-order-integration.md) will feed to
 * tridb_choose_join_order(). It does NOT:
 *   - modify tjs() / tjs_operator.cpp or the TJS patch (the risky operator change — Stage 3 in
 *     ADR-0011 — is specified there, NOT implemented anywhere yet),
 *   - modify the planner or register any hook,
 *   - modify src/planner/join_order.c or the FROZEN decision functions.
 *
 * It compiles only inside the MSVBASE fork on the GX10 (PG 13.4 server headers). Do NOT claim it
 * builds or passes on the x86 standin — there are no server headers here.
 *
 * The arithmetic is deliberately trivial and obviously correct: the FROZEN contract lives in
 * join_order.c; this file only sources the four scalar inputs from the catalog.
 */

#include "postgres.h"
#include "utils/rel.h"			/* Relation, RelationData, Form_pg_class via rd_rel */
#include "join_order_legstats.h"

/*
 * Clamp a restriction selectivity to [0.0, 1.0]. The Postgres estimators already return values in
 * range for well-formed input, but a defensive clamp keeps rel_filter_matches well-defined for any
 * caller (defense-in-depth, mirroring the threshold clamp in tridb_choose_join_order).
 */
static float8
clamp_unit(float8 v)
{
	if (v < 0.0)
		return 0.0;
	if (v > 1.0)
		return 1.0;
	return v;
}

void
tridb_build_legstats(Relation rel,
					 float8 est_filter_selectivity,
					 int32 vector_topk,
					 LegStats *out)
{
	float8	reltuples;
	float8	selectivity;

	Assert(rel != NULL);
	Assert(out != NULL);

	/*
	 * table_size <- pg_class.reltuples for the relational+vector relation. reltuples is a float
	 * ESTIMATE maintained by ANALYZE/autovacuum, not an exact count. A never-analyzed relation has
	 * reltuples == 0, which flows into the FROZEN "table_size == 0 -> selectivity 1.0" branch in
	 * tridb_rel_selectivity() (the safe vector_first default) — exactly the intended behavior, so
	 * we pass it through unmodified rather than substituting a guessed count.
	 *
	 * reltuples can be negative in modern PG as a "no stats yet" sentinel; PG 13 uses 0 for the
	 * unknown case, but clamp the floor at 0 so a sentinel can never produce a negative table_size.
	 */
	reltuples = rel->rd_rel->reltuples;
	if (reltuples < 0.0)
		reltuples = 0.0;

	out->table_size = (int64) reltuples;

	/*
	 * rel_filter_matches <- selectivity * reltuples (the standard restriction-selectivity estimate,
	 * docs/join_order_heuristic_v0.1.0.md §3). The caller computes the selectivity via
	 * clauselist_selectivity() on the canonical query's WHERE clause — that estimator needs
	 * PlannerInfo context not available in this leaf helper, so it is an INPUT here (see ADR-0011
	 * "Drafted here"). We only multiply through and round.
	 *
	 * FROZEN tolerance: we do NOT clamp rel_filter_matches to table_size. A stale-stats over-count
	 * (matches > table_size -> selectivity > 1.0) is allowed and correctly resolves to vector_first
	 * (docs §10.2, §10.3 invariant 4). Here selectivity is clamped to [0,1] only because it is a
	 * probability; matches itself is left to fall out of selectivity * reltuples.
	 */
	selectivity = clamp_unit(est_filter_selectivity);
	out->rel_filter_matches = (int64) (selectivity * reltuples);

	/* vector_topk <- the tjs() k argument, verbatim. */
	out->vector_topk = vector_topk;

	/*
	 * avg_out_degree: PLACEHOLDER 0.0.
	 *
	 * The graph store does NOT currently expose a mean out-degree: GphMeta (src/graph_store/
	 * gph_page.h) carries gm_vertex_count but no store-wide edge count, gph_edge_count is per-page
	 * only, and graph_am.c has no amanalyze hook. Adding gm_edge_count to the metapage (incremented
	 * in gph_insert_edge) and deriving avg_out_degree = gm_edge_count / NULLIF(gm_vertex_count, 0)
	 * is a graph-store-track follow-on (ADR-0011 Stage 0), NOT part of this additive draft.
	 *
	 * This is SAFE for the FR-6 decision: avg_out_degree is NOT an input to
	 * tridb_choose_join_order (FROZEN §10.1 — it is carried only for tridb_estimate_intermediate's
	 * EXPLAIN graph fan-out). The ordering decision is fully determined by rel_filter_matches,
	 * table_size, and the threshold. Only the intermediate-row estimate omits graph fan-out until
	 * Stage 0 lands — which is exactly what the simplified Python reference already does (§5).
	 */
	out->avg_out_degree = 0.0;
}

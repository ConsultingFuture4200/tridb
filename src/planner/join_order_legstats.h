/*
 * join_order_legstats.h — catalog-backed LegStats builder for the cross-modal join-order
 * heuristic (FR-6 / DEV-1285).
 *
 * STATUS: UNBUILT-HERE (GX10-gated); DRAFT FOR REVIEW. This header + join_order_legstats.c are
 * the SAFE, additive half of DEV-1285: a standalone helper that assembles the FROZEN LegStats
 * struct from the Postgres catalog. It does NOT modify tjs() (tjs_operator.cpp), the planner,
 * or join_order.c. The risky operator-shaping change (a filter-first physical path in TJS) is
 * specified in docs/decisions/0011-tjs-join-order-integration.md and is NOT implemented here.
 *
 * The struct below MIRRORS the FROZEN LegStats in docs/join_order_heuristic_v0.1.0.md §10.1 and
 * in src/planner/join_order.c. join_order.c declares LegStats file-locally (static), so this
 * header carries an identical declaration rather than reaching into that translation unit; the
 * field set, order, and types are deliberately bit-for-bit the same (a divergence here would
 * break the FROZEN contract). When the Option-B integration lands, the two declarations should be
 * consolidated into this header and included by join_order.c — a follow-on cleanup, not part of
 * this additive draft.
 *
 * Builds only inside the MSVBASE fork on the GX10 (needs PG 13.4 server headers: utils/rel.h for
 * RelationData / pg_class). Do NOT claim it "builds" or "passes" on the x86 standin.
 */
#ifndef TRIDB_PLANNER_JOIN_ORDER_LEGSTATS_H
#define TRIDB_PLANNER_JOIN_ORDER_LEGSTATS_H

#include "postgres.h"
#include "utils/rel.h"			/* Relation, RelationData, rd_rel->reltuples */

/*
 * LegStats — FROZEN (docs/join_order_heuristic_v0.1.0.md §10.1). Mirror of the declaration in
 * src/planner/join_order.c; keep the two byte-identical.
 */
typedef struct LegStats
{
	int64	rel_filter_matches;	/* est. rows passing the relational filter (selectivity * reltuples) */
	int64	table_size;			/* pg_class.reltuples (estimate; 0 if never ANALYZEd)                 */
	float8	avg_out_degree;		/* graph metapage mean out-degree (see PLACEHOLDER note in the .c)   */
	int32	vector_topk;		/* the tjs() k argument                                               */
} LegStats;

/*
 * tridb_build_legstats — assemble a LegStats from the relational+vector relation + a
 * caller-supplied restriction selectivity.
 *
 *   rel                   open Relation for the tjs() target table (caller owns the lock/ref).
 *   est_filter_selectivity  restriction selectivity of the canonical query's WHERE clause,
 *                         in [0.0, 1.0], computed by the caller via clauselist_selectivity()
 *                         (it needs PlannerInfo context that does not exist in this leaf helper —
 *                         see docs/decisions/0011 "Drafted here").
 *   vector_topk           the tjs() k argument.
 *   out                   populated on success.
 *
 * Sets out->avg_out_degree = 0.0 (PLACEHOLDER: the graph metapage has no avg_out_degree yet —
 * see the .c and ADR-0011). avg_out_degree is NOT an input to tridb_choose_join_order (FROZEN
 * §10.1), so this placeholder does not affect the FR-6 ordering decision.
 */
extern void tridb_build_legstats(Relation rel,
								 float8 est_filter_selectivity,
								 int32 vector_topk,
								 LegStats *out);

#endif							/* TRIDB_PLANNER_JOIN_ORDER_LEGSTATS_H */

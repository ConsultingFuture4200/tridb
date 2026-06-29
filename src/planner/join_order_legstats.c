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
#include "access/relation.h"	/* relation_open / relation_close                       */
#include "catalog/namespace.h"	/* RangeVarGetRelid                                     */
#include "nodes/makefuncs.h"	/* makeRangeVar                                         */
#include "storage/bufmgr.h"		/* ReadBufferExtended, LockBuffer, BufferGetPage        */
#include "utils/rel.h"			/* Relation, RelationData, Form_pg_class via rd_rel     */
#include "join_order_legstats.h"

/*
 * The graph store's internal metapage layout (gm_edge_count / gm_vertex_count). This helper reads
 * the metapage to derive avg_out_degree; the layout header lives in the graph_store source tree
 * (src/graph_store/gph_page.h).
 *
 * BUILD WIRING (GX10, follow-on): this translation unit is still a DRAFT and is NOT yet compiled
 * by src/planner/Makefile (OBJS = join_order.o only). When the Option-B lowering (ADR-0011) wires
 * tridb_build_legstats into the planner, the planner Makefile must (a) add join_order_legstats.o to
 * OBJS and (b) add the graph_store include dir, e.g. `PG_CPPFLAGS += -I$(top_srcdir)/src/graph_store`
 * (or a relative -I../graph_store), so this #include resolves. Out of plan 006's file scope to edit
 * the Makefile here; recorded so the seam is unambiguous.
 */
#include "gph_page.h"

#define GPH_SCHEMA	"graph_store"
#define GPH_RELNAME "gstore"

/*
 * Read a copy of the graph metapage (block GPH_META_BLKNO) under a share lock. Returns false if the
 * graph store relation does not exist or has not been initialized (no blocks / bad magic) — in which
 * case avg_out_degree falls back to 0.0. This mirrors graph_am.c's static gph_read_meta, re-declared
 * here because that one is file-local to the graph_store translation unit (the two extensions are
 * separately linked .so's; they share gph_page.h, not symbols). It does NOT change the FROZEN
 * LegStats contract or tridb_build_legstats's signature — it is an internal read of a second store.
 */
static bool
legstats_read_graph_meta(GphMeta *out)
{
	RangeVar   *rv = makeRangeVar(GPH_SCHEMA, GPH_RELNAME, -1);
	Oid			relid = RangeVarGetRelid(rv, AccessShareLock, true /* missing_ok */);
	Relation	rel;
	Buffer		buf;

	if (!OidIsValid(relid))
		return false;			/* graph store not installed in this database */

	rel = relation_open(relid, NoLock);	/* RangeVarGetRelid already took AccessShareLock */

	if (RelationGetNumberOfBlocks(rel) == 0)
	{
		relation_close(rel, AccessShareLock);
		return false;			/* store never initialized => no vertices/edges */
	}

	buf = ReadBufferExtended(rel, MAIN_FORKNUM, GPH_META_BLKNO, RBM_NORMAL, NULL);
	LockBuffer(buf, BUFFER_LOCK_SHARE);
	memcpy(out, GphPageRecordBase(BufferGetPage(buf)), sizeof(GphMeta));
	UnlockReleaseBuffer(buf);
	relation_close(rel, AccessShareLock);

	return out->gm_magic == GPH_MAGIC;
}

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
	 * avg_out_degree = gm_edge_count / gm_vertex_count, derived from the graph metapage (plan 006,
	 * ADR-0011 Stage 0). The store-wide directed-edge count gm_edge_count is now maintained on the
	 * metapage (incremented under GenericXLog in gph_insert_edge); we read it together with
	 * gm_vertex_count and divide, guarding the zero-vertex case (NULLIF semantics -> 0.0).
	 *
	 * If the graph store is absent/uninitialized in this database, legstats_read_graph_meta returns
	 * false and avg_out_degree stays 0.0 — the same value the old placeholder produced.
	 *
	 * This remains SAFE for the FR-6 decision: avg_out_degree is NOT an input to
	 * tridb_choose_join_order (FROZEN §10.1 — it is carried only for tridb_estimate_intermediate's
	 * EXPLAIN graph fan-out). The ordering decision is fully determined by rel_filter_matches,
	 * table_size, and the threshold; populating avg_out_degree cannot change which order is chosen.
	 */
	{
		GphMeta	gmeta;

		if (legstats_read_graph_meta(&gmeta) && gmeta.gm_vertex_count > 0)
			out->avg_out_degree =
				(float8) gmeta.gm_edge_count / (float8) gmeta.gm_vertex_count;
		else
			out->avg_out_degree = 0.0;
	}
}

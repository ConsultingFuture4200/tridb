/*
 * graph_store.c — TriDB native graph store, v0.
 *
 * Exposes graph traversal as a Volcano-style Open/Next/Close iterator
 * (PostgreSQL ValuePerCall set-returning function), honoring the TR-1
 * early-termination invariant: a LIMIT above the iterator stops it before all
 * neighbors are visited (provable via graph_store.visits()).
 *
 * v0 deliberately backs the adjacency list with a heap relation
 * (graph_store.adjacency: vid -> nbrs[]). This is adjacency-list access — a
 * vertex's out-neighbors are co-located in ONE tuple and walked by this C
 * iterator, NOT an edge join table resolved by the planner. Because the backing
 * lives in a heap under the inherited shared WAL / transaction manager, FR-7
 * (single-txn atomicity across stores) holds for free. The custom 32KB
 * adjacency-page access method (DEV-1163/1164) replaces the heap backing in v1;
 * this iterator contract is the surface that stays.
 */
#include "postgres.h"
#include "fmgr.h"
#include "funcapi.h"
#include "catalog/pg_type.h"
#include "executor/spi.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "miscadmin.h"

PG_MODULE_MAGIC;

/*
 * Session-scoped count of traversal steps (Next() calls). Lets a test prove
 * early termination: visiting K of N neighbors under LIMIT K must increment
 * this by exactly K, never N.
 */
static int64 graph_visit_counter = 0;

typedef struct GraphScanState
{
	int64  *neighbors;	 /* adjacency list materialized for the source vertex */
	int		nneighbors;
	int		cursor;		 /* Open/Next/Close cursor into neighbors[] */
	int64	src;
} GraphScanState;

PG_FUNCTION_INFO_V1(graph_neighbors);

/*
 * graph_neighbors(src bigint) RETURNS SETOF bigint
 * Yields src's out-neighbors one per Next(), lazily — the iterator the TJS
 * operator (DEV-1169) composes and the top-k terminates early.
 */
Datum
graph_neighbors(PG_FUNCTION_ARGS)
{
	FuncCallContext *funcctx;
	GraphScanState  *st;

	if (SRF_IS_FIRSTCALL())			/* === Open === */
	{
		MemoryContext oldctx;
		int64 src = PG_GETARG_INT64(0);

		funcctx = SRF_FIRSTCALL_INIT();
		oldctx = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);

		st = (GraphScanState *) palloc0(sizeof(GraphScanState));
		st->src = src;

		if (SPI_connect() != SPI_OK_CONNECT)
			elog(ERROR, "graph_store: SPI_connect failed");
		{
			Oid		argtypes[1] = { INT8OID };
			Datum	values[1]	= { Int64GetDatum(src) };
			int		ret = SPI_execute_with_args(
				"SELECT nbrs FROM graph_store.adjacency WHERE vid = $1",
				1, argtypes, values, NULL, true, 0);

			if (ret != SPI_OK_SELECT)
				elog(ERROR, "graph_store: adjacency lookup failed (%d)", ret);

			if (SPI_processed > 0)
			{
				bool	isnull;
				Datum	arr_d = SPI_getbinval(SPI_tuptable->vals[0],
											  SPI_tuptable->tupdesc, 1, &isnull);
				if (!isnull)
				{
					ArrayType  *arr = DatumGetArrayTypeP(arr_d);
					Datum	   *elems;
					bool	   *nulls;
					int			n, i;

					deconstruct_array(arr, INT8OID, 8, FLOAT8PASSBYVAL, 'd',
									  &elems, &nulls, &n);
					st->neighbors  = (int64 *) palloc(sizeof(int64) * (n > 0 ? n : 1));
					st->nneighbors = 0;
					for (i = 0; i < n; i++)
						if (!nulls[i])
							st->neighbors[st->nneighbors++] = DatumGetInt64(elems[i]);
				}
			}
		}
		SPI_finish();

		funcctx->user_fctx = st;
		MemoryContextSwitchTo(oldctx);
	}

	funcctx = SRF_PERCALL_SETUP();	/* === Next === */
	st = (GraphScanState *) funcctx->user_fctx;

	if (st->cursor < st->nneighbors)
	{
		int64 nbr = st->neighbors[st->cursor++];

		graph_visit_counter++;		/* one unit of traversal work */
		SRF_RETURN_NEXT(funcctx, Int64GetDatum(nbr));
	}

	SRF_RETURN_DONE(funcctx);		/* === Close === */
}

PG_FUNCTION_INFO_V1(graph_visits);

/* graph_visits() RETURNS bigint — session traversal-step counter (TR-1 probe). */
Datum
graph_visits(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT64(graph_visit_counter);
}

-- join_order — TriDB cross-modal join-order heuristic (FR-6 / DEV-1170).
-- SQL surface over the FROZEN decision core (docs/join_order_heuristic_v0.1.0.md §10).
-- These wrappers exist for the parity test + EXPLAIN; the planner hot path calls the C
-- functions directly (hook deferred — see join_order.c).
\echo Use "CREATE EXTENSION join_order" to load this file. \quit

-- relational_selectivity(): table_size = 0 -> 1.0; else matches/table_size (float8 divide).
CREATE FUNCTION tridb_rel_selectivity(rel_filter_matches bigint, table_size bigint)
	RETURNS float8
	AS 'MODULE_PATHNAME', 'tridb_rel_selectivity_sql'
	LANGUAGE C STRICT IMMUTABLE;

-- choose_order(): 'filter_first' iff selectivity <= threshold (inclusive), else 'vector_first'.
-- threshold clamps to [0,1]; NULL threshold uses the tridb.join_order_selectivity_threshold GUC.
-- NOT STRICT so the NULL-threshold (use-GUC) path is reachable.
CREATE FUNCTION tridb_choose_join_order(rel_filter_matches bigint, table_size bigint,
                                        threshold float8 DEFAULT NULL)
	RETURNS text
	AS 'MODULE_PATHNAME', 'tridb_choose_join_order_sql'
	LANGUAGE C;

-- estimated_intermediate_rows(): filter_first -> min(matches, topk); vector_first -> topk*50.
CREATE FUNCTION tridb_estimate_intermediate(rel_filter_matches bigint, vector_topk int,
                                            join_order text)
	RETURNS bigint
	AS 'MODULE_PATHNAME', 'tridb_estimate_intermediate_sql'
	LANGUAGE C STRICT IMMUTABLE;

-- tridb_choose_join_order_cost (advisor plan 031): graph-leg-aware cost decision.
-- ADDITIVE; the frozen tridb_choose_join_order above is unchanged. STRICT: a NULL arg -> NULL.
CREATE FUNCTION tridb_choose_join_order_cost(deg bigint, rel_filter_matches bigint,
                                             table_size bigint, vector_topk int, term_cond int)
	RETURNS text
	AS 'MODULE_PATHNAME', 'tridb_choose_join_order_cost_sql'
	-- STABLE not IMMUTABLE: the result depends on the tridb.join_order_cost_ratio GUC, so an
	-- IMMUTABLE label would let the planner const-fold + cache a stale decision across a later SET
	-- (Liotta review). Matches the file convention: the frozen GUC-reading choose_join_order is
	-- likewise not IMMUTABLE; only the pure functions are.
	LANGUAGE C STRICT STABLE;

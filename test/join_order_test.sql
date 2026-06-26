-- join_order_test.sql — FR-6 / DEV-1170 cross-modal join-order heuristic.
--
-- Proves the C port (src/planner/join_order.c) makes BIT-IDENTICAL decisions to the Python
-- reference (src/planner/join_order_ref.py) for every case the contract pins
-- (tests/test_join_order.py + docs/join_order_heuristic_v0.1.0.md §10.3). Runs under
-- psql -v ON_ERROR_STOP=1; any failed ASSERT / RAISE aborts the suite (nonzero exit).

CREATE EXTENSION join_order;

-- ---- tridb_rel_selectivity (float8 divide; table_size=0 -> 1.0; no numerator clamp) --------
DO $$
BEGIN
	ASSERT tridb_rel_selectivity(100, 10000) = 0.01::float8,  'sel basic 0.01';
	ASSERT tridb_rel_selectivity(0, 0)       = 1.0::float8,   'sel empty table -> 1.0';
	ASSERT tridb_rel_selectivity(42, 0)      = 1.0::float8,   'sel empty table guard wins over matches';
	ASSERT tridb_rel_selectivity(0, 10000)   = 0.0::float8,   'sel nothing matches -> 0.0';
	ASSERT tridb_rel_selectivity(10000, 10000) = 1.0::float8, 'sel everything matches -> 1.0';
	ASSERT tridb_rel_selectivity(12000, 10000) = 1.2::float8, 'sel stale stats > 1.0 (no clamp)';
	RAISE NOTICE 'PASS join_order: tridb_rel_selectivity matches reference';
END $$;

-- ---- tridb_choose_join_order — FR-6 acceptance + boundary + extremes (default GUC 0.10) -----
DO $$
BEGIN
	-- FR-6 inverted-selectivity acceptance (doc §8): opposite orders.
	ASSERT tridb_choose_join_order(50, 100000)  = 'filter_first', 'selective -> filter_first';
	ASSERT tridb_choose_join_order(90000, 100000) = 'vector_first', 'broad -> vector_first';
	ASSERT tridb_choose_join_order(50, 10000)   = 'filter_first', 'corpus A (0.5%) -> filter_first';
	ASSERT tridb_choose_join_order(8000, 10000) = 'vector_first', 'corpus B (80%) -> vector_first';
	-- Boundary (FROZEN inclusive <=): 0.10 exactly -> filter_first.
	ASSERT tridb_choose_join_order(1000, 10000) = 'filter_first', 'boundary == threshold -> filter_first';
	ASSERT tridb_choose_join_order(1001, 10000) = 'vector_first', 'just above threshold -> vector_first';
	ASSERT tridb_choose_join_order(999, 10000)  = 'filter_first', 'just below threshold -> filter_first';
	-- Extremes + degenerate.
	ASSERT tridb_choose_join_order(0, 10000)    = 'filter_first', 'zero selectivity -> filter_first';
	ASSERT tridb_choose_join_order(10000, 10000) = 'vector_first', 'full selectivity -> vector_first';
	ASSERT tridb_choose_join_order(0, 0)        = 'vector_first', 'empty table -> vector_first (safe default)';
	ASSERT tridb_choose_join_order(12000, 10000) = 'vector_first', 'stale stats -> vector_first';
	RAISE NOTICE 'PASS join_order: tridb_choose_join_order matches reference (acceptance + boundaries)';
END $$;

-- ---- threshold argument + clamp (doc §10.3 #5) ---------------------------------------------
DO $$
BEGIN
	ASSERT tridb_choose_join_order(500, 10000, 0.10) = 'filter_first', '0.05 sel, 0.10 thr -> filter_first';
	ASSERT tridb_choose_join_order(500, 10000, 0.01) = 'vector_first', '0.05 sel, 0.01 thr -> vector_first';
	ASSERT tridb_choose_join_order(0, 10000, 0.0)    = 'filter_first', 'thr 0.0: sel 0.0 -> filter_first';
	ASSERT tridb_choose_join_order(1, 10000, 0.0)    = 'vector_first', 'thr 0.0: any nonzero -> vector_first';
	ASSERT tridb_choose_join_order(10000, 10000, 1.0) = 'filter_first', 'thr 1.0: sel 1.0 -> filter_first';
	-- Clamp: out-of-range thresholds clamp to [0,1] so a non-GUC caller still matches the reference.
	ASSERT tridb_choose_join_order(10000, 10000, 5.0)  = 'filter_first', 'clamp high 5.0 -> 1.0';
	ASSERT tridb_choose_join_order(1, 10000, -1.0)     = 'vector_first', 'clamp low -1.0 -> 0.0';
	ASSERT tridb_choose_join_order(0, 10000, -1.0)     = 'filter_first', 'clamp low boundary: sel 0.0';
	RAISE NOTICE 'PASS join_order: threshold argument + clamp match reference';
END $$;

-- ---- tridb_estimate_intermediate (doc §5) --------------------------------------------------
DO $$
BEGIN
	ASSERT tridb_estimate_intermediate(3, 5, 'filter_first')  = 3,  'filter_first min(3,5)=3';
	ASSERT tridb_estimate_intermediate(50, 5, 'filter_first') = 5,  'filter_first min(50,5)=5';
	ASSERT tridb_estimate_intermediate(0, 5, 'filter_first')  = 0,  'filter_first no matches -> 0';
	ASSERT tridb_estimate_intermediate(50, 5, 'vector_first') = 250, 'vector_first 5*50=250';
	ASSERT tridb_estimate_intermediate(50, 1, 'vector_first') = 50,  'vector_first 1*50=50';
	ASSERT tridb_estimate_intermediate(50, 0, 'vector_first') = 0,   'vector_first topk 0 -> 0';
	-- SM-1: filter-first materially smaller; >= 5x reduction on the selective case.
	ASSERT tridb_estimate_intermediate(50, 5, 'vector_first')
	       >= 5 * tridb_estimate_intermediate(50, 5, 'filter_first'), 'SM-1 >= 5x reduction';
	RAISE NOTICE 'PASS join_order: tridb_estimate_intermediate matches reference (incl SM-1 5x)';
END $$;

-- ---- the GUC is the live knob (default-threshold call follows the SET) ----------------------
SET tridb.join_order_selectivity_threshold = 0.01;
DO $$
BEGIN
	-- 0.05 selectivity, default-threshold (GUC=0.01) call now flips to vector_first.
	ASSERT tridb_choose_join_order(500, 10000) = 'vector_first', 'GUC live: 0.05 sel > 0.01 thr';
	RAISE NOTICE 'PASS join_order: tridb.join_order_selectivity_threshold GUC is live';
END $$;
RESET tridb.join_order_selectivity_threshold;

-- ---- unknown order is rejected (doc §10.2: ereport(ERROR)) ----------------------------------
DO $$
BEGIN
	PERFORM tridb_estimate_intermediate(50, 5, 'graph_first');
	RAISE EXCEPTION 'BUG: unknown order accepted (expected error)';
EXCEPTION
	WHEN invalid_parameter_value THEN
		RAISE NOTICE 'PASS join_order: unknown order rejected (%)', SQLERRM;
END $$;

-- ---- NULL matches/table_size rejected (NOT STRICT wrapper guard, Linus review) -------------
DO $$
BEGIN
	PERFORM tridb_choose_join_order(NULL, 10000);
	RAISE EXCEPTION 'BUG: NULL rel_filter_matches accepted';
EXCEPTION
	WHEN null_value_not_allowed THEN
		RAISE NOTICE 'PASS join_order: NULL matches rejected (no UB through NOT STRICT)';
END $$;

\echo === ALL JOIN-ORDER (FR-6 / DEV-1170) PARITY TESTS PASSED ===

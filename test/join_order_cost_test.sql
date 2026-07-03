-- join_order_cost_test.sql — advisor plan 031: the graph-leg-aware cost decision.
-- ADDITIVE to the frozen core: asserts (1) the frozen tridb_choose_join_order is UNCHANGED,
-- (2) tridb_choose_join_order_cost reproduces the calibration points from the real bench data
-- (docs/join_order_cost_model_v0.1.0.md §2), and (3) the mode GUC defaults to 'threshold'.
-- Runs under psql -v ON_ERROR_STOP=1.

CREATE EXTENSION join_order;

-- (1) Frozen core unchanged (spot-check the acceptance matrix — full parity is join_order_test.sql).
DO $$
BEGIN
    ASSERT tridb_choose_join_order(50, 100000)  = 'filter_first', 'frozen: selective -> filter_first';
    ASSERT tridb_choose_join_order(90000, 100000) = 'vector_first', 'frozen: broad -> vector_first';
    RAISE NOTICE 'PASS 031: frozen tridb_choose_join_order unchanged';
END $$;

-- (2) Cost decision reproduces the calibration points (deg, rel_matches, table_size, k, term_cond).
DO $$
BEGIN
    -- 1M GX10 point: broad ts window (rel_sel 0.6) BUT tiny reachable set (deg 2000 of 1M) ->
    -- joint selectivity ~0.0012 -> filter_first is optimal. The FROZEN threshold (0.6 > 0.10)
    -- would pick vector_first here (blind to the graph leg) — this is the F4 bug the cost mode fixes.
    ASSERT tridb_choose_join_order_cost(2000, 600000, 1000000, 5, 10000) = 'filter_first',
        format('1M point must be filter_first, got %s',
               tridb_choose_join_order_cost(2000, 600000, 1000000, 5, 10000));
    ASSERT tridb_choose_join_order(600000, 1000000) = 'vector_first',
        'threshold DISAGREES on the 1M point (picks vector_first) — that is the bug cost mode fixes';

    -- Mega-hub: same broad window but a huge reachable set (deg 500k) -> draining reachable∩filter
    -- (300k rows) is far costlier than the ~17-candidate vector scan -> vector_first.
    ASSERT tridb_choose_join_order_cost(500000, 600000, 1000000, 5, 10000) = 'vector_first',
        format('mega-hub must be vector_first, got %s',
               tridb_choose_join_order_cost(500000, 600000, 1000000, 5, 10000));

    -- 2k integration corpus, selective window: deg 4, rel_sel 0.01 -> trivial drain -> filter_first.
    ASSERT tridb_choose_join_order_cost(4, 20, 2000, 1, 10000) = 'filter_first',
        '2k selective must be filter_first';

    -- 2k broad window (rel_sel 0.8): deg is still 4 so the drain is trivial regardless of window
    -- breadth -> cost mode says filter_first (correctly), where threshold says vector_first. This
    -- DIVERGENCE from threshold-mode is the whole point (the decision now sees the graph leg).
    ASSERT tridb_choose_join_order_cost(4, 1600, 2000, 2, 10000) = 'filter_first',
        '2k broad: cost mode picks filter_first (tiny deg -> trivial drain)';

    -- Guards: no graph leg / unknown table -> the frozen safe default (vector_first).
    ASSERT tridb_choose_join_order_cost(0, 20, 2000, 1, 10000) = 'vector_first', 'deg 0 -> vector_first';
    ASSERT tridb_choose_join_order_cost(4, 20, 0, 1, 10000) = 'vector_first', 'unknown table -> vector_first';
    ASSERT tridb_choose_join_order_cost(4, 20, 2000, 0, 10000) = 'vector_first', 'k<=0 guard -> vector_first';
    -- stale-stats: rel_matches > table_size -> rel_sel clamps to 1.0 (does not divide-explode).
    ASSERT tridb_choose_join_order_cost(500000, 3000000, 2000000, 5, 10000) = 'vector_first',
        'rel_sel>1 clamp: mega-hub with stale over-count still resolves (vector_first)';
    -- joint_sel -> 0 (rel_matches 0): examined clamps to table_size; tiny drain -> filter_first.
    ASSERT tridb_choose_join_order_cost(4, 0, 2000, 1, 10000) = 'filter_first',
        'joint_sel~0 (0 matches): examined=table_size, drain 0 -> filter_first';

    RAISE NOTICE 'PASS 031: cost decision reproduces all calibration points + guard branches';
END $$;

-- (3) Cost ratio GUC steers the crossover; mode defaults to threshold.
DO $$
BEGIN
    ASSERT current_setting('tridb.join_order_mode') = 'threshold', 'mode defaults to threshold (zero behavior change)';
    -- A very low cost ratio makes vector-first look cheap per candidate -> pushes toward vector_first
    -- even at the 1M point (proves the GUC is load-bearing, not decorative).
    SET tridb.join_order_cost_ratio = 0.001;
    ASSERT tridb_choose_join_order_cost(2000, 600000, 1000000, 5, 10000) = 'vector_first',
        'cost_ratio 0.001 flips the 1M point to vector_first (GUC is load-bearing)';
    RESET tridb.join_order_cost_ratio;
    RAISE NOTICE 'PASS 031: mode defaults threshold; cost_ratio GUC steers the decision';
END $$;

\echo === join_order_cost_test: ALL PASS (plan 031 cost decision, frozen core intact) ===

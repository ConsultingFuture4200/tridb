-- join_order_integration_stub.sql — FR-6 end-to-end "decision changes execution" (DEV-1285).
--
-- STATUS: GX10-GATED STUB — NOT WIRED INTO make graph-test, NOT RUNNABLE TODAY. It depends on
-- the Option-B integration that is specified but NOT implemented:
--   - the lowering computing LegStats (src/planner/join_order_legstats.c) + calling
--     tridb_choose_join_order, then passing the order into tjs() as an argument (ADR-0011 Stage 2/4),
--   - a tjs() filter-first physical path behind that argument (ADR-0011 Stage 3, the SPI/TR-1-risky
--     change, NOT started),
--   - the tjs_last_join_order() companion introspection function (ADR-0011 Stage 4) — Option B's
--     EXPLAIN-visibility tax; the SRF is opaque to EXPLAIN, so the selected driver is asserted via
--     this companion rather than a plan-node label.
--
-- This file is the EXECUTABLE SPEC of the done-criteria assertion: it shows EXACTLY how, once the
-- above lands on the GX10, we prove (a) inverted-selectivity corpora pick OPPOSITE drivers and
-- (b) the peak intermediate (SM-1) differs materially — i.e. the decision is no longer inert.
-- See docs/decisions/0011-tjs-join-order-integration.md "Test plan" item 3.
--
-- It mirrors the §8 acceptance corpora already pinned at the decision-core level in
-- test/join_order_test.sql (Corpus A 0.5% -> filter_first; Corpus B 80% -> vector_first); the
-- difference is that THIS test drives the FULL lowering -> tjs() path, not the standalone functions.
--
-- DO NOT add to make graph-test until Stages 2-4 are implemented and GX10-verified. Left as a
-- design artifact / acceptance contract.

\echo === GX10-GATED STUB: not runnable until ADR-0011 Stages 2-4 land. See header. ===
\quit

-- =====================================================================================
-- The intended shape, for review. Everything below \quit is documentation, not executed.
-- =====================================================================================

-- Corpus A — high selectivity (filter_first expected). table_size ~10000, ~0.5% pass the filter.
-- Corpus B — low selectivity  (vector_first expected). table_size ~10000, ~80% pass the filter.
-- Both built BEFORE CREATE INDEX (fork HNSW incremental-insert limitation, ADR-0007), then ANALYZEd
-- so pg_class.reltuples is populated for tridb_build_legstats.

-- After ANALYZE, the lowering builds LegStats from the catalog and chooses the order. The companion
-- introspection function reports what the operator actually ran with:

-- (a) Opposite drivers — the "EXPLAIN shows selected driver" criterion under Option B.
-- DO $$
-- BEGIN
--     PERFORM run_canonical_query('corpus_a', 5);   -- selective filter
--     ASSERT tjs_last_join_order() = 'filter_first', 'Corpus A (selective) must drive filter_first';
--
--     PERFORM run_canonical_query('corpus_b', 5);   -- broad filter
--     ASSERT tjs_last_join_order() = 'vector_first', 'Corpus B (broad) must drive vector_first';
--
--     RAISE NOTICE 'PASS dev-1285: inverted-selectivity corpora pick OPPOSITE drivers';
-- END $$;

-- (b) The decision is not inert — peak intermediate / candidates-examined (SM-1 / SM-3) differs.
-- On the selective corpus, filter_first examines materially fewer candidates than vector_first would,
-- proving the chosen order changed execution rather than merely being reported.
-- DO $$
-- DECLARE ff_examined bigint; vf_examined bigint;
-- BEGIN
--     PERFORM run_canonical_query('corpus_a', 5);              -- chooses filter_first
--     ff_examined := tjs_candidates_examined();
--     PERFORM run_canonical_query_forced('corpus_a', 5, 'vector_first');  -- force the other path
--     vf_examined := tjs_candidates_examined();
--     ASSERT vf_examined >= 5 * ff_examined,
--            format('SM-1: filter_first must be >= 5x smaller (ff=%s vf=%s)', ff_examined, vf_examined);
--     RAISE NOTICE 'PASS dev-1285: SM-1 reduction confirmed on real execution (ff=% vf=%)',
--                  ff_examined, vf_examined;
-- END $$;

-- (c) TR-1 holds for BOTH physical paths — candidates_examined << corpus (no blocking operator),
-- LIMIT k early-terminates each. Same SM-3 evidence ADR-0007 uses for the vector-first body.
-- DO $$
-- BEGIN
--     PERFORM run_canonical_query('corpus_a', 5);
--     ASSERT tjs_candidates_examined() < (SELECT count(*) FROM corpus_a),
--            'TR-1: filter_first must not examine the full corpus';
--     PERFORM run_canonical_query('corpus_b', 5);
--     ASSERT tjs_candidates_examined() < (SELECT count(*) FROM corpus_b),
--            'TR-1: vector_first must not examine the full corpus';
--     RAISE NOTICE 'PASS dev-1285: TR-1 preserved for both physical paths';
-- END $$;

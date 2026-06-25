"""FR-6 cross-modal join-order heuristic ("the 20%") tests.

Covers the FR-6 acceptance criterion (inverted-selectivity corpora pick opposite
orders) plus the decision-boundary and edge-case matrix that freezes the contract
for the C port (src/planner/join_order.c, GX10-gated). Every behavior asserted
here is part of the interface tridb_choose_join_order() / tridb_rel_selectivity()
/ tridb_estimate_intermediate() must replicate exactly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planner.join_order_ref import (  # noqa: E402
    LegStats,
    choose_order,
    estimated_intermediate_rows,
    explain,
    relational_selectivity,
)

# Default v1 threshold (doc §4); mirrors tridb.join_order_selectivity_threshold GUC.
THRESHOLD = 0.10


# --------------------------------------------------------------------------- #
# relational_selectivity                                                       #
# --------------------------------------------------------------------------- #


def test_selectivity_basic():
    assert relational_selectivity(LegStats(100, 10_000, 5.0, 5)) == 0.01


def test_selectivity_empty_table_returns_one():
    # FROZEN contract: table_size == 0 -> 1.0 (fully unselective -> vector_first).
    assert relational_selectivity(LegStats(0, 0, 0.0, 5)) == 1.0
    # Even if rel_filter_matches is nonzero (stale/absent stats), the guard wins.
    assert relational_selectivity(LegStats(42, 0, 0.0, 5)) == 1.0


def test_selectivity_extremes():
    # Nothing matches -> 0.0
    assert relational_selectivity(LegStats(0, 10_000, 5.0, 5)) == 0.0
    # Everything matches -> 1.0
    assert relational_selectivity(LegStats(10_000, 10_000, 5.0, 5)) == 1.0


def test_selectivity_can_exceed_one_with_stale_stats():
    # Defensive: if catalog stats are stale and matches > table_size, selectivity
    # exceeds 1.0. The heuristic still resolves (treated as low selectivity ->
    # vector_first) because the comparison is monotone. Pinning so the C port does
    # not "helpfully" clamp matches and silently change the decision.
    s = relational_selectivity(LegStats(12_000, 10_000, 5.0, 5))
    assert s == 1.2
    assert choose_order(LegStats(12_000, 10_000, 5.0, 5)) == "vector_first"


# --------------------------------------------------------------------------- #
# choose_order — FR-6 acceptance + boundary semantics                          #
# --------------------------------------------------------------------------- #


def test_inverted_selectivity_picks_opposite_orders():
    # FR-6 acceptance: highly selective relational filter -> filter_first.
    selective = LegStats(
        rel_filter_matches=50, table_size=100_000, avg_out_degree=5.0, vector_topk=5
    )
    # low selectivity -> vector_first
    broad = LegStats(
        rel_filter_matches=90_000, table_size=100_000, avg_out_degree=5.0, vector_topk=5
    )
    assert choose_order(selective) == "filter_first"
    assert choose_order(broad) == "vector_first"


def test_doc_section8_acceptance_corpora():
    # Reproduces doc §8 verbatim: two corpora with inverted selectivity profiles
    # must yield opposite orderings, and filter-first must be materially smaller.
    corpus_a = LegStats(  # 0.5% selectivity
        rel_filter_matches=50, table_size=10_000, avg_out_degree=5.0, vector_topk=5
    )
    corpus_b = LegStats(  # 80% selectivity
        rel_filter_matches=8_000, table_size=10_000, avg_out_degree=5.0, vector_topk=5
    )
    assert choose_order(corpus_a) == "filter_first"
    assert choose_order(corpus_b) == "vector_first"
    assert estimated_intermediate_rows(corpus_a, "filter_first") == 5
    assert estimated_intermediate_rows(corpus_b, "vector_first") == 250


def test_boundary_exactly_at_threshold_is_filter_first():
    # FROZEN: comparison is `<=`, so selectivity == threshold -> filter_first.
    at = LegStats(
        rel_filter_matches=1_000, table_size=10_000, avg_out_degree=5.0, vector_topk=5
    )
    assert relational_selectivity(at) == THRESHOLD
    assert choose_order(at) == "filter_first"


def test_boundary_just_above_threshold_is_vector_first():
    just_above = LegStats(
        rel_filter_matches=1_001, table_size=10_000, avg_out_degree=5.0, vector_topk=5
    )
    assert relational_selectivity(just_above) > THRESHOLD
    assert choose_order(just_above) == "vector_first"


def test_boundary_just_below_threshold_is_filter_first():
    just_below = LegStats(
        rel_filter_matches=999, table_size=10_000, avg_out_degree=5.0, vector_topk=5
    )
    assert relational_selectivity(just_below) < THRESHOLD
    assert choose_order(just_below) == "filter_first"


def test_zero_selectivity_is_filter_first():
    # Filter matches nothing -> maximally selective -> filter_first.
    assert choose_order(LegStats(0, 10_000, 5.0, 5)) == "filter_first"


def test_full_selectivity_is_vector_first():
    # Filter matches everything -> contributes nothing early -> vector_first.
    assert choose_order(LegStats(10_000, 10_000, 5.0, 5)) == "vector_first"


def test_empty_table_defaults_to_vector_first():
    # Degenerate: no stats -> selectivity 1.0 -> vector_first (safe default).
    assert choose_order(LegStats(0, 0, 0.0, 5)) == "vector_first"


# --------------------------------------------------------------------------- #
# choose_order — threshold (GUC) handling                                      #
# --------------------------------------------------------------------------- #


def test_custom_threshold_shifts_boundary():
    # At 5% selectivity, a 0.10 threshold -> filter_first but a 0.01 threshold
    # -> vector_first. Confirms the threshold is the live knob.
    s = LegStats(
        rel_filter_matches=500, table_size=10_000, avg_out_degree=5.0, vector_topk=5
    )
    assert relational_selectivity(s) == 0.05
    assert choose_order(s, threshold=0.10) == "filter_first"
    assert choose_order(s, threshold=0.01) == "vector_first"


def test_threshold_guc_range_endpoints():
    # GUC range is [0.0, 1.0] (doc §7).
    # threshold 0.0: only selectivity == 0.0 is filter_first.
    assert choose_order(LegStats(0, 10_000, 5.0, 5), threshold=0.0) == "filter_first"
    assert choose_order(LegStats(1, 10_000, 5.0, 5), threshold=0.0) == "vector_first"
    # threshold 1.0: everything (selectivity <= 1.0) is filter_first.
    assert (
        choose_order(LegStats(10_000, 10_000, 5.0, 5), threshold=1.0) == "filter_first"
    )


def test_threshold_out_of_range_is_clamped():
    # FROZEN: thresholds outside [0.0, 1.0] clamp to the range so the reference
    # model and the C planner hook never diverge on a misconfigured GUC.
    # Clamp-high: 5.0 -> 1.0 -> a fully-matching filter still goes filter_first.
    assert (
        choose_order(LegStats(10_000, 10_000, 5.0, 5), threshold=5.0) == "filter_first"
    )
    # Clamp-low: -1.0 -> 0.0 -> any nonzero selectivity goes vector_first.
    assert choose_order(LegStats(1, 10_000, 5.0, 5), threshold=-1.0) == "vector_first"
    # Clamp-low boundary: selectivity 0.0 still filter_first under clamped 0.0.
    assert choose_order(LegStats(0, 10_000, 5.0, 5), threshold=-1.0) == "filter_first"


# --------------------------------------------------------------------------- #
# estimated_intermediate_rows                                                  #
# --------------------------------------------------------------------------- #


def test_filter_first_reduces_intermediate_on_selective_case():
    # The whole point of SM-1: filter-first must yield a smaller intermediate set.
    selective = LegStats(
        rel_filter_matches=50, table_size=100_000, avg_out_degree=5.0, vector_topk=5
    )
    ff = estimated_intermediate_rows(selective, "filter_first")
    vf = estimated_intermediate_rows(selective, "vector_first")
    assert ff < vf


def test_sm1_reduction_is_at_least_5x():
    # SM-1 target: ≥5× intermediate-result reduction on selective queries.
    selective = LegStats(
        rel_filter_matches=50, table_size=100_000, avg_out_degree=5.0, vector_topk=5
    )
    ff = estimated_intermediate_rows(selective, "filter_first")
    vf = estimated_intermediate_rows(selective, "vector_first")
    assert vf >= 5 * ff


def test_filter_first_intermediate_is_min_matches_topk():
    # Peak = min(rel_filter_matches, vector_topk) (doc §5).
    assert estimated_intermediate_rows(LegStats(3, 10_000, 5.0, 5), "filter_first") == 3
    assert (
        estimated_intermediate_rows(LegStats(50, 10_000, 5.0, 5), "filter_first") == 5
    )


def test_filter_first_intermediate_zero_when_no_matches():
    # Degenerate: filter matches nothing -> 0 intermediate rows.
    assert estimated_intermediate_rows(LegStats(0, 10_000, 5.0, 5), "filter_first") == 0


def test_vector_first_intermediate_is_50x_topk():
    # Peak = vector_topk * 50 over-fetch (doc §5).
    assert (
        estimated_intermediate_rows(LegStats(50, 10_000, 5.0, 5), "vector_first") == 250
    )
    assert (
        estimated_intermediate_rows(LegStats(50, 10_000, 5.0, 1), "vector_first") == 50
    )


def test_vector_first_intermediate_zero_when_topk_zero():
    # Degenerate: LIMIT 0 -> no over-fetch.
    assert (
        estimated_intermediate_rows(LegStats(50, 10_000, 5.0, 0), "vector_first") == 0
    )


def test_estimated_intermediate_rejects_unknown_order():
    with pytest.raises(ValueError):
        estimated_intermediate_rows(LegStats(50, 10_000, 5.0, 5), "graph_first")


# --------------------------------------------------------------------------- #
# explain                                                                      #
# --------------------------------------------------------------------------- #


def test_explain_shape():
    out = explain(LegStats(50, 100_000, 5.0, 5))
    assert set(out) == {"order", "selectivity", "intermediate_rows", "rationale"}
    assert out["order"] == "filter_first"


def test_explain_agrees_with_choose_order_under_clamped_threshold():
    # explain must clamp identically to choose_order; the reported order and the
    # standalone decision must never disagree, even for an out-of-range threshold.
    stats = LegStats(10_000, 10_000, 5.0, 5)
    out = explain(stats, threshold=5.0)  # clamps to 1.0 -> filter_first
    assert out["order"] == choose_order(stats, threshold=5.0) == "filter_first"


def test_explain_intermediate_matches_chosen_order():
    stats = LegStats(50, 10_000, 5.0, 5)  # filter_first
    out = explain(stats)
    assert out["intermediate_rows"] == estimated_intermediate_rows(stats, out["order"])

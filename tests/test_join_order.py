"""FR-6 acceptance: the heuristic must pick opposite orders on inverted selectivity."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planner.join_order_ref import (  # noqa: E402
    LegStats,
    choose_order,
    estimated_intermediate_rows,
    explain,
    relational_selectivity,
)


def test_selectivity_basic():
    assert relational_selectivity(LegStats(100, 10000, 5.0, 5)) == 0.01
    # divide-by-zero guard
    assert relational_selectivity(LegStats(0, 0, 0.0, 5)) == 1.0


def test_inverted_selectivity_picks_opposite_orders():
    # FR-6: highly selective relational filter -> filter_first
    selective = LegStats(rel_filter_matches=50, table_size=100_000, avg_out_degree=5.0, vector_topk=5)
    # low selectivity -> vector_first
    broad = LegStats(rel_filter_matches=90_000, table_size=100_000, avg_out_degree=5.0, vector_topk=5)
    assert choose_order(selective) == "filter_first"
    assert choose_order(broad) == "vector_first"


def test_filter_first_reduces_intermediate_on_selective_case():
    selective = LegStats(rel_filter_matches=50, table_size=100_000, avg_out_degree=5.0, vector_topk=5)
    ff = estimated_intermediate_rows(selective, "filter_first")
    vf = estimated_intermediate_rows(selective, "vector_first")
    # the whole point of SM-1: filter-first must yield a smaller intermediate set
    assert ff < vf


def test_explain_shape():
    out = explain(LegStats(50, 100_000, 5.0, 5))
    assert set(out) == {"order", "selectivity", "intermediate_rows", "rationale"}
    assert out["order"] == "filter_first"

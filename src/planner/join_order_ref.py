"""
Cross-modal join-ordering heuristic (v1 "20%" planner) for database query planning.

This module implements a reference model for deciding the optimal order of
operations in cross-modal queries, specifically choosing between:
- Running a relational filter first, then vector search
- Running vector search first, then applying relational filters

The decision is based on the estimated selectivity of the relational filter,
as specified in FR-6 ("cross-modal join ordering, the 20%").
"""

from dataclasses import dataclass


@dataclass
class LegStats:
    """Statistics for a query leg in a cross-modal join."""

    rel_filter_matches: int
    table_size: int
    avg_out_degree: float
    vector_topk: int


def relational_selectivity(stats: LegStats) -> float:
    """
    Compute the selectivity of the relational filter.

    Returns rel_filter_matches / table_size, with guard against division by zero.
    """
    if stats.table_size == 0:
        return 1.0
    return stats.rel_filter_matches / stats.table_size


def choose_order(stats: LegStats, threshold: float = 0.10) -> str:
    """
    Decide the optimal order of operations based on relational selectivity.

    Args:
        stats: Statistics for the query leg
        threshold: Selectivity threshold to decide order (default 0.10)

    Returns:
        "filter_first" if selectivity <= threshold, else "vector_first"
    """
    selectivity = relational_selectivity(stats)
    return "filter_first" if selectivity <= threshold else "vector_first"


def estimated_intermediate_rows(stats: LegStats, order: str) -> int:
    """
    Estimate the size of the largest intermediate result for the given order.

    Args:
        stats: Statistics for the query leg
        order: Either "filter_first" or "vector_first"

    Returns:
        Estimated number of rows in the largest intermediate result
    """
    if order == "filter_first":
        # Spec §5: after filtering, at most rel_filter_matches rows remain; the
        # vector leg then limits to topk.  Peak intermediate is the smaller of the
        # two — the graph leg is a pass-through at this level of the model.
        # avg_out_degree is carried in LegStats for the C counterpart
        # (tridb_estimate_intermediate) which will include graph fan-out; the
        # Python reference model is intentionally simplified per §5.
        return min(stats.rel_filter_matches, stats.vector_topk)
    elif order == "vector_first":
        # ANN over-fetches by ~50x topk to maintain recall under HNSW's approximate
        # search guarantee.  vector_topk * 50 >= vector_topk always, so no max()
        # guard is needed (previously dead code).
        return stats.vector_topk * 50
    else:
        raise ValueError(f"Unknown order: {order}")


def explain(stats: LegStats, threshold: float = 0.10) -> dict:
    """
    Provide a detailed explanation of the ordering decision.

    Returns:
        Dictionary with order, selectivity, intermediate rows, and rationale
    """
    selectivity = relational_selectivity(stats)
    order = choose_order(stats, threshold)
    intermediate_rows = estimated_intermediate_rows(stats, order)

    if order == "filter_first":
        rationale = (
            f"Relational filter is highly selective (selectivity={selectivity:.2%} <= {threshold:.0%}). "
            "Filtering first reduces the search space for vector operations."
        )
    else:
        rationale = (
            f"Relational filter is low-selective (selectivity={selectivity:.2%} > {threshold:.0%}). "
            "Running vector search first allows ANN to find candidates before applying filters."
        )

    return {
        "order": order,
        "selectivity": selectivity,
        "intermediate_rows": intermediate_rows,
        "rationale": rationale,
    }


if __name__ == "__main__":
    # Demo usage
    stats = LegStats(
        rel_filter_matches=100, table_size=10000, avg_out_degree=5.0, vector_topk=10
    )

    print("Demo stats:", stats)
    print("Selectivity:", relational_selectivity(stats))
    print("Chosen order:", choose_order(stats))
    print(
        "Intermediate rows (filter_first):",
        estimated_intermediate_rows(stats, "filter_first"),
    )
    print(
        "Intermediate rows (vector_first):",
        estimated_intermediate_rows(stats, "vector_first"),
    )
    print("Explanation:", explain(stats))

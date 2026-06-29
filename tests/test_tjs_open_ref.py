"""Unit tests for the `tjs_open` (B) host reference (Plan 007).

Every behavior asserted here is part of the contract the GX10/engine-gated realization (B)
C operator must replicate: the bounded-push PPR ranking, the NRA/FR termination bound, and
the RRF fusion. The HotpotQA full-corpus run is DATA-GATED on this box (no manifest); these
tests exercise the algorithms on small synthetic graphs and run anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.tjs_open_ref import (  # noqa: E402
    bounded_push_ppr,
    build_adjacency,
    consecutive_drops_merge,
    nra_fr_merge,
    power_iteration_ppr,
    rrf_fuse,
    topk_ids,
)


def _jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0


def _ring_with_hub(n: int):
    """A connected synthetic graph: a ring 0..n-1 plus a hub (node 0) wired to a few
    spokes — enough structure for PPR mass to spread non-trivially."""
    edges = [(i, (i + 1) % n) for i in range(n)]
    edges += [(0, 5), (0, 9), (0, 13)]
    return build_adjacency(edges, n)


# --------------------------------------------------------------------------- #
# Step 1: bounded-push PPR vs the convergence oracle                          #
# --------------------------------------------------------------------------- #


def test_ppr_topk_matches_power_iteration():
    n = 20
    adj = _ring_with_hub(n)
    seeds = [0]
    reserves, _ = bounded_push_ppr(adj, seeds, alpha=0.15, r_max=1e-6)
    oracle = power_iteration_ppr(adj, seeds, n, alpha=0.15)
    j = _jaccard(topk_ids(reserves, 5), topk_ids(oracle, 5))
    assert j >= 0.9, f"bounded-push top-5 Jaccard vs power-iteration = {j} (< 0.9)"


def test_ppr_multiseed_matches_oracle():
    n = 24
    adj = _ring_with_hub(n)
    seeds = [0, 12]
    reserves, _ = bounded_push_ppr(adj, seeds, alpha=0.2, r_max=1e-7)
    oracle = power_iteration_ppr(adj, seeds, n, alpha=0.2)
    j = _jaccard(topk_ids(reserves, 6), topk_ids(oracle, 6))
    assert j >= 0.9, f"multiseed top-6 Jaccard = {j}"


def test_nodes_examined_monotone_in_rmax():
    n = 30
    adj = _ring_with_hub(n)
    seeds = [0]
    examined = []
    for r_max in (1e-1, 1e-2, 1e-3, 1e-4, 1e-5):
        _, ne = bounded_push_ppr(adj, seeds, alpha=0.15, r_max=r_max)
        examined.append(ne)
    # nodes_examined must be NON-DECREASING as r_max FALLS (work grows as floor drops)
    assert examined == sorted(examined), f"nodes_examined not monotone: {examined}"
    # and strictly larger at the tightest floor than the loosest (real growth)
    assert examined[-1] > examined[0], examined


def test_ppr_reserve_mass_concentrates_on_seed():
    n = 16
    adj = _ring_with_hub(n)
    reserves, _ = bounded_push_ppr(adj, [0], alpha=0.15, r_max=1e-6)
    # the seed itself must hold the most reserve mass
    assert topk_ids(reserves, 1) == [0], reserves


# --------------------------------------------------------------------------- #
# Step 2: NRA / FR-bound termination                                          #
# --------------------------------------------------------------------------- #


def test_fr_bound_never_stops_before_confirmable():
    # Two legs; build streams where the true top-1 by sum is unambiguous only after
    # enough items are seen. The FR run must return a member it could actually confirm:
    # we assert its top-k equals the full-merge top-k (no false early stop).
    vec = [(1, 0.9), (2, 0.8), (3, 0.5), (4, 0.2)]
    ppr = [(3, 0.9), (4, 0.85), (1, 0.3), (2, 0.1)]
    ids, examined = nra_fr_merge(vec, ppr, k=2)
    # brute-force aggregate (sum of known scores, missing=0)
    agg: dict[int, float] = {}
    for cid, sc in vec + ppr:
        agg[cid] = agg.get(cid, 0.0) + sc
    true_top2 = [c for c, _ in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
    assert set(ids) == set(true_top2), f"FR top-2 {ids} != true {true_top2}"
    assert examined >= 2


def test_fr_bound_terminates_early_when_possible():
    # A dominant candidate present on both legs at the very top should let the bound
    # stop well before consuming the long tail.
    n_tail = 50
    vec = [(0, 1.0)] + [(i, 0.4 - i * 0.001) for i in range(1, n_tail)]
    ppr = [(0, 1.0)] + [(i, 0.4 - i * 0.001) for i in range(1, n_tail)]
    ids, examined = nra_fr_merge(vec, ppr, k=1)
    assert ids == [0]
    assert examined < n_tail, f"no early termination: examined={examined}"


def test_fr_bridge_kept_alive_without_special_case():
    # A "bridge": high PPR, vector leg unseen until very late. It must still surface in
    # top-2 purely via the best-score bound — no bridge-injection special case.
    bridge = 99
    vec = [(1, 0.95), (2, 0.9)] + [(i, 0.1) for i in range(3, 20)] + [(bridge, 0.05)]
    ppr = [(bridge, 1.0), (1, 0.2)]
    ids, _ = nra_fr_merge(vec, ppr, k=2)
    assert bridge in ids, f"bridge dropped: {ids}"


def test_consecutive_drops_baseline_runs():
    ranked = [(i, 1.0 - i * 0.01) for i in range(100)]
    ids, examined = consecutive_drops_merge(ranked, k=5, term_cond=10)
    assert ids == [0, 1, 2, 3, 4]
    # with a descending stream it should stop after k + term_cond-ish items, not all 100
    assert examined < 100


# --------------------------------------------------------------------------- #
# Step 3: RRF fusion                                                          #
# --------------------------------------------------------------------------- #


def test_rrf_promotes_graph_high_vector_low_bridge():
    # bridge: top of the PPR list, absent/low in vector. distractor: vector-mid,
    # graph-zero. RRF must rank the bridge above the distractor.
    bridge, distractor = 7, 42
    vec_ranked = [1, 2, 3, distractor, 5, 6]  # distractor at vector rank 4
    ppr_ranked = [bridge, 1, 2]  # bridge at ppr rank 1, absent in vector
    fused = rrf_fuse(vec_ranked, ppr_ranked, k=6)
    assert fused.index(bridge) < fused.index(distractor), fused


def test_rrf_at_least_vector_only_on_agreement():
    # when both legs agree on the same order, RRF preserves it
    order = [10, 20, 30, 40]
    fused = rrf_fuse(order, order, k=4)
    assert fused == order


def test_rrf_window_is_bounded():
    # windowed RRF must ignore items past the window in EACH leg (the non-blocking
    # property). vec window {0..4}; a vector item beyond the window (e.g. 900) must NOT
    # contribute, but a ppr-window item (999) does -> it can outrank windowed vec items.
    vec_ranked = list(range(1000))
    ppr_ranked = [999, 900]  # 999 in ppr window; 900 is past vec window entirely
    fused = rrf_fuse(vec_ranked, ppr_ranked, k=20, window=5)
    # 900 only appears past the vector window, so its sole contribution is its ppr rank;
    # it must still be reachable via the ppr leg (proving the window is per-leg, not global)
    assert 999 in fused and 900 in fused, fused
    # no vector item beyond the window (>=5) that lacks a ppr entry may appear
    assert all((d < 5) or (d in ppr_ranked) for d in fused), fused

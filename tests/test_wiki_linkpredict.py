"""Pure-logic tests for the wiki link predictor (DEV-1354).

No embedding, no network, no hnswlib — just the neighbour-minus-existing set
subtraction and the overlap metric on a tiny hand-built fixture. Runs under
`make test`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_linkpredict import linked_fraction, predicted_unlinked  # noqa: E402


def test_predicted_unlinked_subtracts_self_edges_redirects_preserving_rank():
    # source 1; cosine-ranked neighbours (nearest first): 1(self), 2(linked),
    # 3(redirect), 4(new), 5(new)
    neighbors = [1, 2, 3, 4, 5]
    pred = predicted_unlinked(
        neighbors,
        self_id=1,
        linked={2},
        redirect_excluded={3},
    )
    # self, linked, redirect gone; nearest-first order preserved
    assert pred == [4, 5]


def test_predicted_unlinked_all_linked_yields_empty():
    assert (
        predicted_unlinked([2, 3], self_id=1, linked={2, 3}, redirect_excluded=set())
        == []
    )


def test_linked_fraction_excludes_self_and_ratios_over_neighbours():
    # neighbours (excl self 1): 2,3,4,5 ; linked: 2,3 -> 2/4
    assert linked_fraction([1, 2, 3, 4, 5], self_id=1, linked={2, 3}) == 0.5


def test_linked_fraction_empty_neighbours_is_zero():
    assert linked_fraction([1], self_id=1, linked=set()) == 0.0

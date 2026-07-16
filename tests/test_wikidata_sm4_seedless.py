"""Host tests for the SM-4 seedless recall reducer — no docker, no engine, no network.

bench/wikidata_sm4_seedless.run_point clocks a live stock-PG tjs_open sweep, but its
per-query recall is pure set math. Plan 069 extracted that math into recall_at_k so the
boundary cases (perfect / disjoint / partial / empty oracle) can be pinned without a DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_sm4_seedless import recall_at_k  # noqa: E402


def test_recall_perfect_overlap():
    assert recall_at_k([1, 2, 3], [1, 2, 3]) == 1.0


def test_recall_disjoint():
    assert recall_at_k([4, 5, 6], [1, 2, 3]) == 0.0


def test_recall_partial_fraction():
    # 2 of the 3 oracle ids retrieved -> 2/3
    assert recall_at_k([1, 2, 99], [1, 2, 3]) == 2 / 3


def test_recall_empty_oracle_is_one():
    # No ground truth -> nothing to miss. Plan 069 convention: 1.0 (the old inline
    # max(1, len(o)) denominator scored this as 0.0, understating the mean).
    assert recall_at_k([1, 2, 3], []) == 1.0
    assert recall_at_k([], []) == 1.0


def test_recall_denominator_is_oracle_not_result():
    # extra retrieved ids beyond the oracle do not inflate recall past 1.0
    assert recall_at_k([1, 2, 3, 4, 5], [1, 2]) == 1.0
    # duplicate oracle ids collapse (set semantics): 1 unique gold, retrieved -> 1.0
    assert recall_at_k([7], [7, 7, 7]) == 1.0

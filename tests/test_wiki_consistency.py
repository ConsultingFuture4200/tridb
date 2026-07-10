"""Pure-logic tests for the cross-modal consistency demonstrator (DEV-1354).

`torn` is the per-read consistency oracle (do the three store legs agree?) and `vec`
is the version-encoding embedding used to make a read's vector leg observable. Both are
pure — no Milvus/Neo4j/Postgres, no network. This pins their semantics so a silent
regression can't slip through the CI `pytest tests/` gate before the next Spark run."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wiki_consistency import EDIM, torn, vec  # noqa: E402


def test_torn_false_when_all_legs_agree():
    # all three stores read the same version -> consistent, not torn.
    assert torn((1, 1, 1)) is False


def test_torn_true_when_one_leg_lags():
    # one store still at v0 while two advanced to v1 -> torn read.
    assert torn((1, 1, 0)) is True


def test_torn_true_when_all_legs_differ():
    assert torn((0, 1, 2)) is True


def test_vec_encodes_version_in_first_component():
    v = vec(1)
    assert v[0] == 1.0
    assert v[1:] == [0.0] * (EDIM - 1)


def test_vec_length_matches_embedding_dim():
    assert len(vec(0)) == EDIM


def test_vec_zero_version_is_all_zero():
    assert vec(0) == [0.0] * EDIM

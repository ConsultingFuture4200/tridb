"""Host tests for Harness A — Wikidata edit-firehose consistency (plan 060).

Covers the pure/host-runnable layer only: edit parsing, the deterministic synthetic edit
window, and the architecture simulation (one-WAL atomic vs multi-store independent commits).
The live replay (run_live) needs the GX10/Spark stores and is not exercised here — same
DB-gated boundary as tests/test_wiki_consistency.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_consistency import (  # noqa: E402
    Edit,
    load_edits,
    parse_edit,
    simulate,
    simulate_headtohead,
    synthetic_edits,
)


def test_parse_edit_requires_entity_and_rev():
    assert parse_edit({"entity": 42, "rev": 7}) == Edit(42, 7)
    assert parse_edit({"entity": 42}) is None
    assert parse_edit({"rev": 7}) is None
    assert (
        parse_edit({"entity": "Q42", "rev": 7}) is None
    )  # unresolved ext id, not an int


def test_load_edits_jsonl(tmp_path):
    p = tmp_path / "edits.jsonl"
    p.write_text(
        '{"entity": 1, "rev": 1, "label": "A"}\n'
        "\n"  # blank line tolerated
        '{"entity": 1, "rev": 2, "statement": [31, 5]}\n',
        encoding="utf-8",
    )
    edits = load_edits(p)
    assert edits == [Edit(1, 1), Edit(1, 2)]


def test_synthetic_edits_deterministic_and_monotone():
    a = synthetic_edits(200, 20, seed=1354)
    b = synthetic_edits(200, 20, seed=1354)
    assert a == b  # deterministic given the seed
    assert len(a) == 200
    # per-entity revisions are strictly increasing in replay order
    last: dict[int, int] = {}
    for e in a:
        assert e.rev == last.get(e.entity, 0) + 1
        last[e.entity] = e.rev


def test_simulate_onewal_never_torn():
    edits = synthetic_edits(300, 50, seed=7)
    r = simulate(edits, atomic=True)
    assert r["torn"] == 0
    assert r["torn_rate"] == 0.0
    assert r["observations"] == 300  # one atomic observation per edit
    assert r["architecture"] == "one_wal_atomic"


def test_simulate_multistore_tears_on_prefinal_legs():
    edits = synthetic_edits(300, 50, seed=7)
    r = simulate(edits, atomic=False)
    # three commits per edit; the vector-only and vector+graph states are torn (2 of 3)
    assert r["observations"] == 900
    assert r["torn"] == 600
    assert abs(r["torn_rate"] - 2 / 3) < 1e-9
    assert r["examples"]  # torn examples captured


def test_simulate_single_edit_tear_shape():
    # one full cross-modal bump of a fresh entity: (1,0,0) then (1,1,0) torn, (1,1,1) clean
    r = simulate([Edit(0, 1)], atomic=False)
    assert r["torn"] == 2 and r["observations"] == 3
    ex = r["examples"][0]
    assert (ex["vector"], ex["graph"], ex["relational"]) == (1, 0, 0)


def test_headtohead_tridb_zero_multistore_positive():
    edits = synthetic_edits(120, 30, seed=99)
    h2h = simulate_headtohead(edits)
    assert h2h["tridb"]["torn"] == 0
    assert h2h["multistore"]["torn"] > 0
    assert h2h["layer"] == "host_simulation"


def test_recorded_sample_roundtrips(tmp_path):
    # a recorded EventStreams-shaped window drives the same 0-vs->0 headline
    window = [
        {"entity": 42, "rev": 1, "label": "Douglas Adams", "statement": [31, 5]},
        {"entity": 42, "rev": 2, "claim": {"P569": "+1952-03-11T00:00:00Z"}},
        {"entity": 7, "rev": 1, "label": "Universe"},
    ]
    p = tmp_path / "w.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in window), encoding="utf-8")
    edits = load_edits(p)
    h2h = simulate_headtohead(edits)
    assert h2h["tridb"]["torn"] == 0
    assert h2h["multistore"]["torn"] == 2 * len(edits)  # 2 torn commits per edit

"""Tests for the plan-060 Wikidata edit-window recorder — no network, no engine.

Drives the recorder's pure parsing/filter layer over synthetic SSE payload text
(the network loop is a thin wrapper around these functions): SSE framing (multi-line
data, comments/keepalives, id capture), the wikidatawiki/ns0/edit/Q-title filter,
malformed-line resilience, stop conditions, the parse_edit round-trip, and the
sidecar window pin.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_consistency import Edit, parse_edit  # noqa: E402
from tools.wikidata_record_edits import (  # noqa: E402
    WindowStats,
    drain_stream,
    edit_from_change,
    iter_sse_events,
    window_meta,
)


def _change(
    title,
    *,
    wiki="wikidatawiki",
    namespace=0,
    type_="edit",
    rev_new=1001,
    ts=1_760_000_000,
    **extra,
):
    obj = {
        "wiki": wiki,
        "namespace": namespace,
        "type": type_,
        "title": title,
        "timestamp": ts,
        "revision": {"old": rev_new - 1, "new": rev_new},
        **extra,
    }
    return obj


def _sse(events: list[tuple[str | None, dict | str]]) -> list[str]:
    """Render (event_id, payload) pairs as SSE text lines (payload dict -> json)."""
    lines: list[str] = []
    for event_id, payload in events:
        lines.append("event: message\n")
        if event_id is not None:
            lines.append(f"id: {event_id}\n")
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}\n")
        lines.append("\n")
    return lines


# --------------------------------------------------------------------------- #
# SSE framing
# --------------------------------------------------------------------------- #
def test_iter_sse_events_basic_framing():
    lines = _sse([("[cursor-1]", {"a": 1}), (None, {"b": 2})])
    events = list(iter_sse_events(lines))
    assert events == [("[cursor-1]", '{"a": 1}'), (None, '{"b": 2}')]


def test_iter_sse_events_multiline_data_and_comments():
    lines = [
        ": keepalive\n",
        "id: cur-7\n",
        "data: {\n",
        'data: "x": 1}\n',
        "\n",
        ": another keepalive with no event\n",
        "\n",
    ]
    events = list(iter_sse_events(lines))
    assert events == [("cur-7", '{\n"x": 1}')]  # data lines join with newline
    assert json.loads(events[0][1]) == {"x": 1}


def test_iter_sse_events_flushes_partial_final_event():
    # stream drops mid-event (no trailing blank line) — the partial event still yields
    events = list(iter_sse_events(['data: {"y": 2}\n']))
    assert events == [(None, '{"y": 2}')]


# --------------------------------------------------------------------------- #
# filter — only wikidatawiki / ns0 / edit / Q<int> titles are usable
# --------------------------------------------------------------------------- #
def test_edit_from_change_accepts_item_edit():
    rec = edit_from_change(
        _change("Q42", rev_new=2337, user="alice", comment="set label")
    )
    assert rec is not None
    assert rec["entity"] == 42 and rec["rev"] == 2337
    assert rec["ts"] == 1_760_000_000
    assert rec["user"] == "alice" and rec["comment"] == "set label"


def test_edit_from_change_rejections():
    assert edit_from_change(_change("Q42", wiki="enwiki")) is None
    assert edit_from_change(_change("Property:P31", namespace=120)) is None
    assert edit_from_change(_change("Q42", type_="new")) is None
    assert edit_from_change(_change("Q42", type_="log")) is None
    assert edit_from_change(_change("P31")) is None  # not a Q title
    assert edit_from_change(_change("Q0")) is None  # not a positive item id
    assert edit_from_change(_change("Talk:Q42")) is None
    # missing / non-int new revision
    bad = _change("Q42")
    bad["revision"] = {"old": 1}
    assert edit_from_change(bad) is None
    bad["revision"] = {"new": "2337"}
    assert edit_from_change(bad) is None


# --------------------------------------------------------------------------- #
# drain — the full parse+filter pipeline over a synthetic window
# --------------------------------------------------------------------------- #
def _window_lines():
    return _sse(
        [
            ("c1", _change("Q1", rev_new=11, ts=100)),
            ("c2", _change("Q2", rev_new=22, ts=101, wiki="enwiki")),  # filtered
            ("c3", "{not json"),  # malformed data line — skipped
            ("c4", _change("Q3", rev_new=33, ts=102, namespace=120)),  # filtered
            ("c5", _change("Q4", rev_new=44, ts=103)),
            ("c6", _change("Q1", rev_new=12, ts=104)),
        ]
    )


def test_drain_stream_filters_and_tracks_window():
    got: list[dict] = []
    stats = WindowStats()
    done = drain_stream(_window_lines(), got.append, stats)
    assert done is False  # stream ended; no stop condition hit
    assert [(r["entity"], r["rev"]) for r in got] == [(1, 11), (4, 44), (1, 12)]
    assert stats.events == 6  # every event counted, including filtered/malformed
    assert stats.edits == 3
    assert (stats.first_ts, stats.last_ts) == (100, 104)
    assert stats.last_event_id == "c6"


def test_drain_stream_stops_at_max_edits():
    got: list[dict] = []
    stats = WindowStats()
    done = drain_stream(_window_lines(), got.append, stats, max_edits=2)
    assert done is True
    assert len(got) == 2 and stats.edits == 2
    assert stats.last_event_id == "c5"  # resume cursor points at the stop event


def test_drain_stream_deadline_already_passed_stops_immediately():
    got: list[dict] = []
    stats = WindowStats()
    done = drain_stream(_window_lines(), got.append, stats, deadline=0.0)
    assert done is True
    assert stats.events == 1  # checked after the first event


# --------------------------------------------------------------------------- #
# recorded schema == bench/wikidata_consistency.parse_edit's recorded-sample schema
# --------------------------------------------------------------------------- #
def test_records_round_trip_through_parse_edit():
    got: list[dict] = []
    drain_stream(_window_lines(), got.append, WindowStats())
    for rec in got:
        line = json.dumps(rec, ensure_ascii=False)  # what record() writes per line
        edit = parse_edit(json.loads(line))
        assert edit == Edit(entity=rec["entity"], rev=rec["rev"])


# --------------------------------------------------------------------------- #
# sidecar pin
# --------------------------------------------------------------------------- #
def test_window_meta_pins_the_window(tmp_path):
    stats = WindowStats()
    drain_stream(_window_lines(), lambda _r: None, stats)
    meta = window_meta(
        "https://stream.example/sse", stats, max_edits=100, max_seconds=None
    )
    assert meta["stream_url"] == "https://stream.example/sse"
    assert meta["events_seen"] == 6 and meta["edits_recorded"] == 3
    assert meta["first_event_ts"] == 100 and meta["last_event_ts"] == 104
    assert meta["requested"] == {"edits": 100, "seconds": None}
    assert meta["interrupted"] is False
    assert "recorded_at" in meta
    # sidecar naming convention: <out>.meta.json next to the window file
    out = tmp_path / "edits.jsonl"
    Path(str(out) + ".meta.json").write_text(json.dumps(meta))
    assert (tmp_path / "edits.jsonl.meta.json").exists()

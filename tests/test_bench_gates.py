"""DONE-completeness gates for the benchmark graders (advisor plan 014).

Each grader that turns a live engine transcript into a headline metric must reject
a transcript that never reached its terminal `#... DONE` marker — otherwise a
mid-run segfault silently becomes a plausible smaller-N number. One test per gated
module: a truncated transcript (marker absent) raises SystemExit("...incomplete...");
a complete transcript grades normally.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import filtered_report, h2h_report, tjs_open_live, v2a_open  # noqa: E402
from tools import sweep_corpus  # noqa: E402


def test_v2a_open_gate():
    complete = "#V2A IDS_BEGIN qid=0\n 1\n 2\n#V2A IDS_END qid=0\n#V2A DONE\n"
    assert v2a_open.parse(complete) == {0: [1, 2]}
    with pytest.raises(SystemExit, match="incomplete"):
        v2a_open.parse("#V2A IDS_BEGIN qid=0\n 1\n#V2A IDS_END qid=0\n")  # no DONE


def test_tjs_open_live_gate():
    complete = "#TJSOPEN IDS_BEGIN qid=0\n 1\n#TJSOPEN IDS_END qid=0\n#TJSOPEN DONE\n"
    assert tjs_open_live.parse(complete) == {0: [1]}
    with pytest.raises(SystemExit, match="incomplete"):
        tjs_open_live.parse("#TJSOPEN IDS_BEGIN qid=0\n 1\n#TJSOPEN IDS_END qid=0\n")


def test_h2h_report_gate():
    complete = (
        "#H2H QSTART qid=0\n#H2H IDS_BEGIN qid=0\n 1\n#H2H IDS_END qid=0\n"
        "Time: 1.0 ms\n#H2H DONE\n"
    )
    parsed = h2h_report.parse_tridb(complete)
    assert parsed[0]["ids"] == [1] and parsed[0]["times"] == [1.0]
    with pytest.raises(SystemExit, match="incomplete"):
        h2h_report.parse_tridb("#H2H QSTART qid=0\n#H2H IDS_BEGIN qid=0\n 1\n")


def test_filtered_report_gate():
    complete = (
        "#FILT QSTART qid=0 sel=1 run=1 tag=RUN\n 5\n 7\nTime: 2.0 ms\n"
        "#FILT QEND qid=0 sel=1 run=1\n#FILT DONE\n"
    )
    parsed = filtered_report.parse(complete)
    assert parsed[(0, 1)]["ids"] == [5, 7]
    with pytest.raises(SystemExit, match="incomplete"):
        filtered_report.parse(
            "#FILT QSTART qid=0 sel=1 run=1 tag=RUN\n 5\n#FILT QEND qid=0 sel=1 run=1\n"
        )


def test_sweep_corpus_gate():
    manifest = {"entities": 1000, "k": 3, "queries": [{"qid": 0, "oracle": [1, 2, 3]}]}
    complete = (
        "#SWEEP BUILD_BEGIN cfg=16_200\nTime: 10.0 ms\n#SWEEP BUILD_END cfg=16_200\n"
        "#SWEEP RESULT cfg=16_200 tc=50 qid=0 ids=1,2,3\n#SWEEP DONE\n"
    )
    rep = sweep_corpus.report(manifest, complete)
    assert rep["sweep"][0]["mean_recall@3"] == 1.0
    with pytest.raises(SystemExit, match="incomplete"):
        sweep_corpus.report(
            manifest, "#SWEEP RESULT cfg=16_200 tc=50 qid=0 ids=1,2,3\n"
        )

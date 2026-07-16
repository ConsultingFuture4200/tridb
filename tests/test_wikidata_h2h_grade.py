"""Host tests for Harness B `baseline`/`grade` additions (plan 060, DEV-1354).

Covers the new host-runnable layer: the #WD transcript parser (mirror of
wiki_h2h.parse_tridb), grading math vs a tiny oracle (recall@k set-overlap,
median-of-runs latency, median examined), censored-point carry-through, baseline
grading (reused grade_baseline), oracle_meta honesty defaults, and the full
grade -> report round-trip (blocked and clean publication_gate paths). The live
`baseline` leg (Neo4j+pg) is GX10/Spark-gated and not exercised here — no
network, no docker.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.wikidata_h2h import (  # noqa: E402
    DEFAULT_BASELINE_GRID,
    WCfg,
    grade_baseline,
    grade_tridb,
    main,
    oracle_meta_from_env,
    parse_tridb,
    run_baseline,
)

ORACLE_META = {
    "n": 7,
    "dim": 4,
    "k": 2,
    "hops": 1,
    "queries": [{"x": 5, "p": 50, "t": 3}, {"x": 5, "p": 50, "t": 4}],
    "induced_edges": 3,
    "candidate_size_median": 2.0,
    "oracle": {"0": [0, 1], "1": [2]},
}


def _transcript(entries, done=True) -> str:
    """Synthetic psql transcript in the shape emit_tridb_sql's #WD markers produce:
    warm-up id rows between IDS/ENDIDS, the EXAMINED counter row, then the timed
    repeats (each echoing its id rows again — those must be ignored — plus Time:)."""
    L: list[str] = []
    for qid, combo, ids, examined, times in entries:
        L.append(f"#WD TRIDB qid={qid} combo={combo}")
        L.append(f"#WD IDS qid={qid} combo={combo}")
        L.append(" id ")
        L.append("----")
        L.extend(f" {i}" for i in ids)
        L.append(f"({len(ids)} rows)")
        L.append(f"#WD ENDIDS qid={qid} combo={combo}")
        L.append("                 line                  ")
        L.append(f" #WD EXAMINED qid={qid} combo={combo} examined={examined} bridges=3")
        L.append("(1 row)")
        for t in times:
            L.extend(f" {i}" for i in ids)  # timed repeats re-echo rows; not graded
            L.append(f"Time: {t:.3f} ms")
    if done:
        L.append("#WD DONE")
    return "\n".join(L) + "\n"


HEALTHY_ENTRIES = [
    (0, "m8h1t64", [0, 1], 12, [4.8, 4.6, 4.7]),
    (1, "m8h1t64", [2], 10, [4.7, 4.5, 4.9]),
]


# --------------------------------------------------------------------------- #
# transcript parsing (#WD marker mirror of wiki_h2h.parse_tridb)
# --------------------------------------------------------------------------- #
def test_parse_transcript_ids_times_examined():
    parsed = parse_tridb(_transcript(HEALTHY_ENTRIES))
    assert set(parsed) == {(0, "m8h1t64"), (1, "m8h1t64")}
    d0 = parsed[(0, "m8h1t64")]
    assert d0["ids"] == [0, 1]  # warm-up rows only; repeat echoes ignored
    assert d0["times"] == [4.8, 4.6, 4.7]
    assert d0["examined"] == 12 and d0["bridges"] == 3
    assert parsed[(1, "m8h1t64")]["ids"] == [2]


def test_parse_incomplete_transcript_refused():
    with pytest.raises(SystemExit, match="#WD DONE"):
        parse_tridb(_transcript(HEALTHY_ENTRIES, done=False))


# --------------------------------------------------------------------------- #
# grading math (grade_tridb / grade_baseline reused verbatim from wiki_h2h)
# --------------------------------------------------------------------------- #
def test_grade_tridb_median_of_runs_and_recall():
    curve = grade_tridb(
        parse_tridb(_transcript(HEALTHY_ENTRIES)), ORACLE_META["oracle"], k=2
    )
    c = curve["m8h1t64"]
    assert c["recall_at_k"] == 1.0  # {0,1} and {2} both fully recovered
    assert c["median_latency_ms"] == 4.7  # median of per-query medians (4.7, 4.7)
    assert c["median_examined"] == 11.0  # median(12, 10)
    assert c["n_queries"] == 2


def test_grade_tridb_partial_recall():
    entries = [
        (0, "m8h1t64", [0, 9], 12, [4.7]),  # 1 of the 2 oracle ids
        (1, "m8h1t64", [7], 10, [4.7]),  # 0 of 1
    ]
    curve = grade_tridb(parse_tridb(_transcript(entries)), ORACLE_META["oracle"], k=2)
    assert curve["m8h1t64"]["recall_at_k"] == pytest.approx(0.25)  # mean(0.5, 0.0)


def test_grade_baseline_recall_and_latency():
    raw = {
        "h1f64": {
            0: {"ids": [0, 1], "median_ms": 22.0},
            1: {"ids": [9], "median_ms": 20.0},
        }
    }
    curve = grade_baseline(raw, ORACLE_META["oracle"], k=2)
    c = curve["h1f64"]
    assert c["recall_at_k"] == pytest.approx(0.5)  # mean(1.0, 0.0)
    assert c["median_latency_ms"] == 21.0
    assert c["n_queries"] == 2


# --------------------------------------------------------------------------- #
# oracle_meta honesty defaults (gate inputs; grade only carries numbers through)
# --------------------------------------------------------------------------- #
def test_oracle_meta_defaults_keep_blockers_up(monkeypatch):
    for v in (
        "WH_ENGINE_EDGES",
        "WH_NEO4J_EDGES",
        "WH_HNSW_HEALTHY_BUILDS",
        "WH_HNSW_TOTAL_BUILDS",
    ):
        monkeypatch.delenv(v, raising=False)
    meta = oracle_meta_from_env(ORACLE_META, WCfg())
    assert meta["engine_edges"] is None  # undeclared -> graph-set blocker stays up
    assert meta["neo4j_edges"] == "3"  # defaults to the oracle's induced edge count
    assert meta["hnsw_healthy_builds"] is None
    assert meta["tjs_max_examined"] == 4000  # the disclosed TR-1 cap


def test_oracle_meta_env_overrides(monkeypatch):
    monkeypatch.setenv("WH_ENGINE_EDGES", "3")
    monkeypatch.setenv("WH_NEO4J_EDGES", "5")
    monkeypatch.setenv("WH_HNSW_HEALTHY_BUILDS", "3")
    monkeypatch.setenv("WH_HNSW_TOTAL_BUILDS", "3")
    meta = oracle_meta_from_env(ORACLE_META, WCfg())
    assert meta["engine_edges"] == "3" and meta["neo4j_edges"] == "5"
    assert meta["hnsw_healthy_builds"] == "3"


# --------------------------------------------------------------------------- #
# grade CLI -> the graded curves JSON `report` consumes -> publication_gate paths
# --------------------------------------------------------------------------- #
def _run_grade(tmp_path: Path, entries, baseline_raw) -> Path:
    oracle_p = tmp_path / "oracle.json"
    oracle_p.write_text(json.dumps(ORACLE_META))
    raw_p = tmp_path / "tridb_raw.txt"
    raw_p.write_text(_transcript(entries))
    base_p = tmp_path / "baseline.json"
    base_p.write_text(json.dumps(baseline_raw))
    out_p = tmp_path / "graded.json"
    assert (
        main(
            [
                "grade",
                "--tridb-raw",
                str(raw_p),
                "--baseline",
                str(base_p),
                "--oracle",
                str(oracle_p),
                "--out",
                str(out_p),
            ]
        )
        == 0
    )
    return out_p


BASELINE_RAW = {
    "h1f64": {
        "0": {"ids": [0, 1], "median_ms": 22.0},
        "1": {"ids": [2], "median_ms": 22.0},
    }
}


def _clear_gate_env(monkeypatch):
    for v in (
        "WH_ENGINE_EDGES",
        "WH_NEO4J_EDGES",
        "WH_HNSW_HEALTHY_BUILDS",
        "WH_HNSW_TOTAL_BUILDS",
        "WH_BOUNDARY_PARITY",
        "WH_MIN_HEALTHY_BUILDS",
    ):
        monkeypatch.delenv(v, raising=False)


def _pass_gate_env(monkeypatch):
    monkeypatch.setenv("WH_ENGINE_EDGES", "3")  # == oracle induced_edges
    monkeypatch.setenv("WH_HNSW_HEALTHY_BUILDS", "3")
    monkeypatch.setenv("WH_HNSW_TOTAL_BUILDS", "3")
    monkeypatch.setenv("WH_BOUNDARY_PARITY", "1")
    monkeypatch.delenv("WH_NEO4J_EDGES", raising=False)
    monkeypatch.delenv("WH_MIN_HEALTHY_BUILDS", raising=False)


def test_grade_emits_curves_only_no_headline(tmp_path, monkeypatch):
    _clear_gate_env(monkeypatch)
    graded = json.loads(_run_grade(tmp_path, HEALTHY_ENTRIES, BASELINE_RAW).read_text())
    assert set(graded) == {"tridb", "baseline", "oracle_meta"}
    t = graded["tridb"]["m8h1t64"]
    assert set(t) >= {"recall_at_k", "median_latency_ms", "median_examined"}
    b = graded["baseline"]["h1f64"]
    assert set(b) >= {"recall_at_k", "median_latency_ms"}
    # no headline math in grade: no ratio/speedup anywhere in the JSON
    assert "speedup" not in json.dumps(graded)


def test_roundtrip_report_blocked_without_declarations(tmp_path, monkeypatch):
    _clear_gate_env(monkeypatch)
    graded_p = _run_grade(tmp_path, HEALTHY_ENTRIES, BASELINE_RAW)
    md_p = tmp_path / "report.md"
    assert main(["report", "--oracle", str(graded_p), "--out", str(md_p)]) == 0
    md = md_p.read_text()
    assert "COMPARISON INVALID" in md and "speedup" not in md
    assert "graph-set" in md  # engine edges undeclared -> honest blocker


def test_roundtrip_report_clean_emits_headline(tmp_path, monkeypatch):
    _pass_gate_env(monkeypatch)
    graded_p = _run_grade(tmp_path, HEALTHY_ENTRIES, BASELINE_RAW)
    md_p = tmp_path / "report.md"
    assert main(["report", "--oracle", str(graded_p), "--out", str(md_p)]) == 0
    md = md_p.read_text()
    assert "COMPARISON INVALID" not in md
    assert "speedup: 4.68×" in md  # 22.0 / 4.7


def test_roundtrip_censored_point_blocks(tmp_path, monkeypatch):
    _pass_gate_env(monkeypatch)
    censored = [
        (0, "m8h1t64", [0, 1], 4000, [4.8, 4.6, 4.7]),  # examined == TR-1 cap
        (1, "m8h1t64", [2], 4000, [4.7, 4.5, 4.9]),
    ]
    graded_p = _run_grade(tmp_path, censored, BASELINE_RAW)
    md_p = tmp_path / "report.md"
    assert main(["report", "--oracle", str(graded_p), "--out", str(md_p)]) == 0
    md = md_p.read_text()
    assert "CENSORED" in md and "speedup" not in md


def test_roundtrip_examined_zero_blocks(tmp_path, monkeypatch):
    _pass_gate_env(monkeypatch)
    seqscan = [
        (0, "m8h1t64", [0, 1], 0, [4.8]),  # examined=0: silent seqscan / timeout
        (1, "m8h1t64", [2], 0, [4.7]),
    ]
    graded_p = _run_grade(tmp_path, seqscan, BASELINE_RAW)
    md_p = tmp_path / "report.md"
    assert main(["report", "--oracle", str(graded_p), "--out", str(md_p)]) == 0
    md = md_p.read_text()
    assert "did NOT use the HNSW index" in md and "speedup" not in md


# --------------------------------------------------------------------------- #
# baseline leg: pure pieces only (the live Neo4j+pg run is GX10/Spark-gated)
# --------------------------------------------------------------------------- #
def test_baseline_grid_shape_and_importability():
    assert callable(run_baseline)  # lazy store imports: module loads without drivers
    assert all(len(c) == 2 for c in DEFAULT_BASELINE_GRID)  # (hops, frontier)
    assert all(h >= 1 and f >= 1 for h, f in DEFAULT_BASELINE_GRID)


def test_grade_without_baseline_file(tmp_path, monkeypatch):
    _clear_gate_env(monkeypatch)
    oracle_p = tmp_path / "oracle.json"
    oracle_p.write_text(json.dumps(ORACLE_META))
    raw_p = tmp_path / "tridb_raw.txt"
    raw_p.write_text(_transcript(HEALTHY_ENTRIES))
    out_p = tmp_path / "graded.json"
    assert (
        main(
            [
                "grade",
                "--tridb-raw",
                str(raw_p),
                "--baseline",
                str(tmp_path / "missing.json"),
                "--oracle",
                str(oracle_p),
                "--out",
                str(out_p),
            ]
        )
        == 0
    )
    graded = json.loads(out_p.read_text())
    assert graded["baseline"] == {} and "m8h1t64" in graded["tridb"]
    assert not math.isnan(graded["tridb"]["m8h1t64"]["recall_at_k"])

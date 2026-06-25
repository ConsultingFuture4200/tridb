"""Validate the seed-corpus generator: determinism, shapes, and SQL emission."""

import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "tools" / "seed_corpus.py"


def _run(out_dir, seed=42, entities=40, dim=16, epn=4):
    subprocess.run(
        [
            sys.executable,
            str(GEN),
            "--entities",
            str(entities),
            "--dim",
            str(dim),
            "--edges-per-node",
            str(epn),
            "--seed",
            str(seed),
            "--out",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_generates_all_artifacts(tmp_path):
    out = tmp_path / "seed"
    _run(out)
    for name in ("entities.csv", "edges.csv", "queries.jsonl", "load.sql"):
        assert (out / name).exists(), name

    rows = list(csv.DictReader((out / "entities.csv").open()))
    assert len(rows) == 40
    # embedding is a Postgres float8[] literal
    assert rows[0]["embedding"].startswith("{") and rows[0]["embedding"].endswith("}")
    assert len(rows[0]["embedding"].strip("{}").split(",")) == 16

    queries = [json.loads(line) for line in (out / "queries.jsonl").open()]
    assert len(queries) == 10
    assert len(queries[0]["embedding"]) == 16
    assert len(queries[0]["selected_time_range"]) == 30

    sql = (out / "load.sql").read_text()
    assert "embedding float8[]" in sql
    assert "related_to" in sql


def test_no_self_loops(tmp_path):
    out = tmp_path / "seed"
    _run(out)
    for e in csv.DictReader((out / "edges.csv").open()):
        assert e["src"] != e["dst"]


def test_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _run(a, seed=7)
    _run(b, seed=7)
    assert (a / "entities.csv").read_bytes() == (b / "entities.csv").read_bytes()
    assert (a / "edges.csv").read_bytes() == (b / "edges.csv").read_bytes()


def _run_raw(out_dir, *extra):
    """Invoke the generator directly with arbitrary flags; return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(GEN), "--out", str(out_dir), *extra],
        capture_output=True,
        text=True,
    )


def test_rejects_zero_dim(tmp_path):
    r = _run_raw(tmp_path / "s", "--dim", "0")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "--dim" in r.stderr


def test_rejects_zero_entities(tmp_path):
    r = _run_raw(tmp_path / "s", "--entities", "0")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "--entities" in r.stderr


def test_rejects_negative_edges(tmp_path):
    r = _run_raw(tmp_path / "s", "--edges-per-node", "-1")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "--edges-per-node" in r.stderr


def test_rejects_narrow_time_window(tmp_path):
    r = _run_raw(tmp_path / "s", "--time-min", "1000", "--time-max", "1010")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "--time-max" in r.stderr


def test_single_entity_boundary(tmp_path):
    # --entities 1: no self-edges possible -> zero edges, but must not crash.
    out = tmp_path / "s"
    _run(out, entities=1, epn=4)
    rows = list(csv.DictReader((out / "entities.csv").open()))
    assert len(rows) == 1
    edges = list(csv.DictReader((out / "edges.csv").open()))
    assert edges == []


def test_embeddings_are_l2_normalized(tmp_path):
    import math

    out = tmp_path / "s"
    _run(out, entities=8, dim=16)
    for row in csv.DictReader((out / "entities.csv").open()):
        vals = [float(x) for x in row["embedding"].strip("{}").split(",")]
        norm = math.sqrt(sum(v * v for v in vals))
        assert abs(norm - 1.0) < 1e-4

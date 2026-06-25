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

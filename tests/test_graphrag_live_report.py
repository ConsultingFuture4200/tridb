"""Tests for bench/graphrag_live_report.py — the DONE gate of the live GraphRAG
run (advisor plan 085).

These run anywhere (no engine, no Docker): they feed hand-written #BENCH
transcripts + a tiny HotpotQA-shaped manifest through the strict validator and
grader, and drive scripts/bench_graphrag.sh through its raw-injection test seam
to prove DONE cannot print before grading succeeds.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

from bench.graphrag_live_report import (  # noqa: E402
    build_payload,
    grade,
    render_md,
    validate_transcript,
)
from bench.graphrag_report import load_slice  # noqa: E402

# --------------------------------------------------------------------------- #
# Fixtures: tiny HotpotQA-shaped manifest (6 paragraphs, 2 questions)
# --------------------------------------------------------------------------- #


@pytest.fixture
def manifest_path(tmp_path) -> Path:
    n, dim = 6, 4
    rng = np.random.default_rng(0)
    corpus_emb = rng.standard_normal((n, dim)).astype(np.float32)
    query_emb = rng.standard_normal((2, dim)).astype(np.float32)
    cpath, qpath = tmp_path / "corpus_emb.npy", tmp_path / "query_emb.npy"
    np.save(cpath, corpus_emb)
    np.save(qpath, query_emb)
    m = {
        "source": "hotpotqa-test-fixture",
        "source_slice": "synthetic",
        "graph_kind": "test-edges",
        "embed_model": "test-encoder",
        "entities": n,
        "dim": dim,
        "edges": 2,
        "num_queries": 2,
        "k": 3,
        "corpus_emb_path": str(cpath),
        "query_emb_path": str(qpath),
        "paragraphs": [
            {"id": i, "title": f"Title {i}", "text": f"body of paragraph {i}"}
            for i in range(n)
        ],
        "_edges": [[0, 1], [1, 2]],
        "questions": [
            {
                "qid": 0,
                "question": "What is thing zero?",
                "answer": "Alpha",
                "type": "bridge",
                "gold_ids": [1, 2],
            },
            {
                "qid": 1,
                "question": "What is thing one?",
                "answer": "Beta",
                "type": "comparison",
                "gold_ids": [3],
            },
        ],
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m))
    return p


def _complete_transcript() -> str:
    # q0: ids [1,4,5] vs gold [1,2] -> recall 0.5, joint 0, f1 0.4
    # q1: ids [3,0]   vs gold [3]   -> recall 1.0, joint 1, f1 2/3
    return (
        "#BENCH QSTART qid=0 src=0 k=3\n"
        " #BENCH TRIDB_RESULT qid=0 ids=1,4,5\n"
        " #BENCH TRIDB_EXAMINED qid=0 examined=7\n"
        "#BENCH QSTART qid=1 src=1 k=3\n"
        " #BENCH TRIDB_RESULT qid=1 ids=3,0\n"
        " #BENCH TRIDB_EXAMINED qid=1 examined=9\n"
        "#BENCH DONE\n"
    )


# --------------------------------------------------------------------------- #
# Step 1: strict transcript validation
# --------------------------------------------------------------------------- #


def test_complete_transcript_passes(manifest_path):
    sl = load_slice(manifest_path)
    live = validate_transcript(_complete_transcript(), sl)
    assert live == {
        0: {"ids": [1, 4, 5], "examined": 7},
        1: {"ids": [3, 0], "examined": 9},
    }


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda t: t.replace("#BENCH DONE\n", ""), "DONE"),
        (lambda t: t.replace(" #BENCH TRIDB_RESULT qid=1 ids=3,0\n", ""), "missing"),
        (
            lambda t: t.replace(" #BENCH TRIDB_EXAMINED qid=0 examined=7\n", ""),
            "missing",
        ),
        (lambda t: t.replace("ids=3,0", "ids=3,x"), "malformed"),
        (lambda t: t.replace("examined=7", "examined=oops"), "malformed"),
        (
            lambda t: t.replace(
                "#BENCH QSTART qid=1",
                " #BENCH TRIDB_RESULT qid=0 ids=2,2\n#BENCH QSTART qid=1",
            ),
            "conflicting duplicate",
        ),
        (lambda t: t.replace("ids=3,0", "ids=3,99"), "out-of-range"),
        (
            lambda t: t + " #BENCH TRIDB_RESULT qid=7 ids=1\n",
            "unexpected",
        ),
    ],
    ids=[
        "missing-done",
        "missing-result-qid",
        "missing-examined",
        "malformed-ids",
        "malformed-examined",
        "duplicate-conflict",
        "out-of-range-id",
        "unexpected-qid",
    ],
)
def test_incomplete_or_corrupt_transcript_rejected(manifest_path, mutate, msg):
    sl = load_slice(manifest_path)
    with pytest.raises(SystemExit) as ei:
        validate_transcript(mutate(_complete_transcript()), sl)
    assert msg in str(ei.value)
    assert ei.value.code != 0  # a string code -> exit status 1


def test_identical_duplicate_record_tolerated(manifest_path):
    sl = load_slice(manifest_path)
    t = _complete_transcript() + " #BENCH TRIDB_RESULT qid=0 ids=1,4,5\n"
    assert validate_transcript(t, sl)[0]["ids"] == [1, 4, 5]


def test_empty_result_is_legitimate(manifest_path):
    sl = load_slice(manifest_path)
    t = _complete_transcript().replace("ids=1,4,5", "ids=")
    live = validate_transcript(t, sl)
    assert live[0]["ids"] == []


# --------------------------------------------------------------------------- #
# Step 2: grading — exact evidence aggregates, answer EM/F1, reader failures
# --------------------------------------------------------------------------- #


def test_exact_evidence_aggregates(manifest_path):
    payload = build_payload(
        manifest_path, _complete_transcript(), k=3, term_cond=0, reader_kind="none"
    )
    assert payload["engine_live"] is True
    assert payload["n_questions"] == 2
    e = payload["evidence"]
    assert e["recall"] == pytest.approx((0.5 + 1.0) / 2)
    assert e["joint"] == pytest.approx(0.5)
    assert e["f1"] == pytest.approx((0.4 + 2 / 3) / 2)
    assert payload["examined"] == {"mean": 8.0, "min": 7, "max": 9}
    assert payload["answer"]["mode"] == "evidence-only"
    assert "em" not in payload["answer"]  # no EM/F1 claimed without a reader


class _FakeReader:
    """Deterministic stub: answers 'Alpha' iff paragraph 1 is in the context."""

    name = "fake-reader"

    def answer(self, question: str, contexts: list[str]) -> str:
        return "Alpha" if any("paragraph 1" in c for c in contexts) else "wrong guess"


def test_exact_answer_em_f1(manifest_path):
    sl = load_slice(manifest_path)
    live = validate_transcript(_complete_transcript(), sl)
    out = grade(sl, live, _FakeReader(), _FakeReader.name)
    a = out["answer"]
    # q0 gets 'Alpha' (gold Alpha -> EM 1, F1 1); q1 gets 'wrong guess' (gold Beta
    # -> EM 0, F1 0). Denominator is ALL questions.
    assert a == {
        "mode": "reader",
        "reader": "fake-reader",
        "em": pytest.approx(0.5),
        "f1": pytest.approx(0.5),
        "n": 2,
    }


class _NoneReader:
    name = "none-reader"

    def answer(self, question, contexts):
        return None


class _CrashReader:
    name = "crash-reader"

    def answer(self, question, contexts):
        raise RuntimeError("api down")


@pytest.mark.parametrize("reader", [_NoneReader(), _CrashReader()])
def test_reader_failure_fails_run_never_shrinks_denominator(manifest_path, reader):
    sl = load_slice(manifest_path)
    live = validate_transcript(_complete_transcript(), sl)
    with pytest.raises(SystemExit) as ei:
        grade(sl, live, reader, reader.name)
    assert "denominator" in str(ei.value)


# --------------------------------------------------------------------------- #
# JSON / Markdown schema
# --------------------------------------------------------------------------- #


def test_payload_and_md_identify_scope(manifest_path):
    payload = build_payload(
        manifest_path, _complete_transcript(), k=3, term_cond=5, reader_kind="none"
    )
    assert payload["corpus"]["source"] == "hotpotqa-test-fixture"
    assert payload["corpus"]["embed_model"] == "test-encoder"
    assert payload["k"] == 3 and payload["term_cond"] == 5
    assert "graphrag-h2h" in payload["scope"]  # multi-system run pointed elsewhere
    md = render_md(payload)
    assert "engine_live=True" in md
    assert "EVIDENCE-ONLY" in md
    assert "graphrag-h2h" in md
    assert "term_cond=5" in md


# --------------------------------------------------------------------------- #
# Step 3: the shell prints DONE only after the grader passes (raw-inject seam)
# --------------------------------------------------------------------------- #


def _run_shell(manifest_path, tmp_path, raw_text: str):
    raw = tmp_path / "inject_raw.txt"
    raw.write_text(raw_text)
    outdir = tmp_path / "results"
    env = dict(
        os.environ,
        GRAPHRAG_RAW_INJECT=str(raw),
        GRAPHRAG_MANIFEST=str(manifest_path),
        GRAPHRAG_OUTDIR=str(outdir),
        GRAPHRAG_PY=sys.executable,
        GRAPHRAG_READER="none",
    )
    return (
        subprocess.run(
            ["bash", str(ROOT / "scripts" / "bench_graphrag.sh")],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=120,
        ),
        outdir,
    )


def test_shell_gates_done_on_grading(manifest_path, tmp_path):
    # incomplete transcript: no DONE marker, nonzero exit, artifacts kept
    bad = _complete_transcript().replace("#BENCH DONE\n", "")
    proc, outdir = _run_shell(manifest_path, tmp_path, bad)
    assert proc.returncode != 0
    assert "[graphrag-live] DONE" not in proc.stdout
    assert (outdir / "graphrag_live_raw.txt").exists()  # evidence survives failure

    # complete transcript: grader passes, DONE printed exactly once, artifacts written
    proc, outdir = _run_shell(manifest_path, tmp_path, _complete_transcript())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("[graphrag-live] DONE") == 1
    assert (outdir / "graphrag_live_metrics.json").exists()
    assert (outdir / "graphrag_live_report.md").exists()
    payload = json.loads((outdir / "graphrag_live_metrics.json").read_text())
    assert payload["engine_live"] is True

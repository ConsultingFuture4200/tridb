"""Grade a LIVE GraphRAG tjs() run's #BENCH transcript vs the HotpotQA gold (Plan 085).

scripts/bench_graphrag.sh captures the live engine's `#BENCH ...` output; this
module is its SUCCESS GATE: the shell may print DONE only after this exits 0.

Two stages, both strict:

  1. Transcript validation (:func:`validate_transcript`): requires `#BENCH DONE`,
     exactly one well-formed TRIDB_RESULT and TRIDB_EXAMINED record per manifest
     qid, integer ids inside the corpus range, no conflicting duplicates, no
     unexpected qids. Any violation exits nonzero with a specific message —
     an incomplete run can never be graded as complete.
  2. Grading (:func:`grade`): evidence recall / joint / F1 of the LIVE retrieved
     ids against the gold supporting paragraphs, using the SAME scorers and
     reducers as bench/graphrag_report.py, plus optional downstream answer EM/F1
     via a configured existing reader over contexts built from those exact live
     ids. Every manifest qid contributes to every reported denominator: a reader
     failure FAILS answer-grade mode instead of silently shrinking EM/F1.
     With --reader none the report is explicitly labeled evidence-only.

Scope honesty: this grades the LIVE TriDB engine ONLY. The measured live
multi-system latency head-to-head is scripts/bench_graphrag_h2h.sh
(`make graphrag-h2h`) and is never implied here. Live per-query latency stays
in the raw transcript (kept as evidence next to the derived reports).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from bench.graphrag_report import (
    Slice,
    _mean,
    em_score,
    evidence_scores,
    f1_score,
    load_slice,
    make_reader,
)
from bench.live_report import _RE_EXAMINED, _RE_RESULT, parse_bench_output

# Loose markers: catch TRIDB record lines whose payload the strict regexes
# reject (e.g. non-integer ids) instead of silently reading them as "missing".
_ANY_RESULT = re.compile(r"#BENCH TRIDB_RESULT\b")
_ANY_EXAMINED = re.compile(r"#BENCH TRIDB_EXAMINED\b")


def _die(msg: str) -> SystemExit:
    return SystemExit(f"[graphrag-live-report] REJECT: {msg}")


def validate_transcript(text: str, sl: Slice) -> dict[int, dict]:
    """Strictly validate the live transcript; return qid -> {ids, examined}.

    Raises SystemExit (nonzero) with a specific message on ANY incompleteness:
    the caller must treat that as run failure, never as a gradable result.
    """
    if "#BENCH DONE" not in text:
        raise _die("transcript missing '#BENCH DONE' — the live run is incomplete")

    expected = {int(q["qid"]) for q in sl.questions}
    results: dict[int, list[int]] = {}
    examined: dict[int, int] = {}

    for line in text.splitlines():
        m = _RE_RESULT.search(line)
        if m:
            if line[m.end() :].strip():
                raise _die(f"malformed TRIDB_RESULT line (trailing junk): {line!r}")
            qid, ids = (
                int(m.group(1)),
                [int(x) for x in m.group(2).split(",") if x != ""],
            )
            if qid in results and results[qid] != ids:
                raise _die(
                    f"conflicting duplicate TRIDB_RESULT records for qid={qid}: "
                    f"{results[qid]} vs {ids}"
                )
            results[qid] = ids
            continue
        if _ANY_RESULT.search(line):
            raise _die(f"malformed TRIDB_RESULT line (non-integer ids?): {line!r}")
        m = _RE_EXAMINED.search(line)
        if m:
            qid, n = int(m.group(1)), int(m.group(2))
            if qid in examined and examined[qid] != n:
                raise _die(
                    f"conflicting duplicate TRIDB_EXAMINED records for qid={qid}: "
                    f"{examined[qid]} vs {n}"
                )
            examined[qid] = n
            continue
        if _ANY_EXAMINED.search(line):
            raise _die(f"malformed TRIDB_EXAMINED line: {line!r}")

    for name, got in (("TRIDB_RESULT", results), ("TRIDB_EXAMINED", examined)):
        unexpected = sorted(set(got) - expected)
        if unexpected:
            raise _die(f"unexpected qids in {name} (not in manifest): {unexpected}")
        missing = sorted(expected - set(got))
        if missing:
            raise _die(f"missing {name} record(s) for manifest qid(s): {missing}")

    for qid, ids in results.items():
        bad = [i for i in ids if not (0 <= i < sl.n)]
        if bad:
            raise _die(
                f"out-of-range result id(s) for qid={qid}: {bad} "
                f"(corpus has {sl.n} paragraphs)"
            )

    return {q: {"ids": results[q], "examined": examined[q]} for q in expected}


def grade(sl: Slice, live: dict[int, dict], reader, reader_name: str | None) -> dict:
    """Evidence (+ optional answer) metrics over the LIVE retrieved ids.

    Same scorers/reducers as graphrag_report (evidence_scores, em/f1, _mean).
    Every manifest qid is in every denominator: any reader failure (exception or
    None return) aborts answer-grade mode rather than shrinking the denominator.
    """
    para_text = {p["id"]: f"{p['title']}. {p['text']}" for p in sl.paragraphs}
    gold = {int(q["qid"]): q for q in sl.questions}

    ev: dict[str, list[float]] = {"recall": [], "joint": [], "f1": []}
    ans: dict[str, list[float]] = {"em": [], "f1": []}
    for qid in sorted(live):
        ids = live[qid]["ids"]
        s = evidence_scores(ids, gold[qid]["gold_ids"])
        for m in ev:
            ev[m].append(s[m])
        if reader is not None:
            try:
                pred = reader.answer(gold[qid]["question"], [para_text[i] for i in ids])
            except Exception as e:  # noqa: BLE001 — a reader crash is a run failure
                raise _die(
                    f"reader {reader_name!r} raised on qid={qid} ({e}) — "
                    "answer-grade mode requires EVERY question graded (no silent "
                    "denominator shrink); rerun with --reader none for an "
                    "evidence-only report"
                ) from e
            if pred is None:
                raise _die(
                    f"reader {reader_name!r} failed (None) on qid={qid} — "
                    "answer-grade mode requires EVERY question graded (no silent "
                    "denominator shrink); rerun with --reader none for an "
                    "evidence-only report"
                )
            ans["em"].append(em_score(pred, gold[qid]["answer"]))
            ans["f1"].append(f1_score(pred, gold[qid]["answer"]))

    exam = [live[q]["examined"] for q in sorted(live)]
    out = {
        "n_questions": len(live),
        "evidence": {m: _mean(v) for m, v in ev.items()},
        "examined": {"mean": _mean(exam), "min": min(exam), "max": max(exam)},
    }
    if reader is not None:
        out["answer"] = {
            "mode": "reader",
            "reader": reader_name,
            "em": _mean(ans["em"]),
            "f1": _mean(ans["f1"]),
            "n": len(ans["em"]),
        }
    else:
        out["answer"] = {
            "mode": "evidence-only",
            "reason": "no reader configured (--reader none); answer EM/F1 NOT measured",
        }
    return out


def render_md(payload: dict) -> str:
    c = payload["corpus"]
    a = payload["answer"]
    e = payload["evidence"]
    x = payload["examined"]
    lines = [
        "# TriDB LIVE GraphRAG run — engine-only grading (Plan 015 Phase 5 / Plan 085)",
        "",
        f"- **Scope**: `engine_live={payload['engine_live']}` — grades the LIVE "
        "TriDB engine's tjs() retrieval ONLY. The measured live multi-system "
        "latency head-to-head is `make graphrag-h2h` (NOT run here).",
        f"- **Corpus**: {c['source']} — {c['n_paragraphs']} paragraphs, "
        f"graph={c['graph_kind']}, embeddings={c['embed_model']}",
        f"- **Operating point**: k={payload['k']}, term_cond={payload['term_cond']}",
        f"- **Questions graded**: {payload['n_questions']} (every manifest qid; "
        "strict transcript validation)",
        "",
        "## Evidence retrieval (live ids vs gold supporting paragraphs)",
        "",
        "| recall | joint | F1 |",
        "|---:|---:|---:|",
        f"| {e['recall']:.3f} | {e['joint']:.3f} | {e['f1']:.3f} |",
        "",
        "## Downstream answer accuracy",
        "",
    ]
    if a["mode"] == "reader":
        lines += [
            f"Reader = {a['reader']}; {a['n']} questions (all of them).",
            "",
            "| answer EM | answer F1 |",
            "|---:|---:|",
            f"| {a['em']:.3f} | {a['f1']:.3f} |",
        ]
    else:
        lines += [f"**EVIDENCE-ONLY report** — {a['reason']}."]
    lines += [
        "",
        "## Engine observations",
        "",
        f"- `tjs_candidates_examined()`: mean {x['mean']:.1f}, "
        f"min {x['min']}, max {x['max']}",
        "- Live per-query latency (EXPLAIN ANALYZE) stays in the raw transcript "
        "kept beside this report; a measured latency comparison vs the "
        "multi-store baseline is `make graphrag-h2h`.",
        "",
        "_Generated by `bench/graphrag_live_report.py` (the DONE gate of "
        "`scripts/bench_graphrag.sh`). Numbers are observed from the live run._",
    ]
    return "\n".join(lines) + "\n"


def build_payload(
    manifest_path: Path, raw_text: str, *, k: int, term_cond: int, reader_kind: str
) -> dict:
    manifest = json.loads(manifest_path.read_text())
    sl = load_slice(manifest_path)
    live = validate_transcript(raw_text, sl)
    # parse_bench_output is the shared #BENCH scraper; cross-check agreement so
    # the strict pass and the shared parser can never drift silently.
    obs = parse_bench_output(raw_text)
    for qid, rec in live.items():
        if obs.get(qid, {}).get("tridb_ids") != rec["ids"]:
            raise _die(f"strict/shared parser disagreement for qid={qid}")

    reader = None
    reader_name = None
    if reader_kind != "none":
        reader = make_reader(reader_kind)
        reader_name = reader.name
    graded = grade(sl, live, reader, reader_name)

    return {
        "engine_live": True,
        "scope": (
            "engine-only: LIVE tjs() retrieval graded vs HotpotQA gold; the live "
            "multi-system head-to-head is scripts/bench_graphrag_h2h.sh "
            "(make graphrag-h2h)"
        ),
        "corpus": {
            "source": manifest.get("source"),
            "source_slice": manifest.get("source_slice"),
            "graph_kind": manifest.get("graph_kind"),
            "embed_model": manifest.get("embed_model"),
            "n_paragraphs": sl.n,
        },
        "k": k,
        "term_cond": term_cond,
        **graded,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True, help="captured #BENCH output")
    ap.add_argument("--k", type=int, required=True)
    ap.add_argument("--term-cond", type=int, required=True)
    ap.add_argument(
        "--reader",
        choices=["none", "extractive", "anthropic", "codex"],
        default="none",
        help="'none' -> explicitly evidence-only report (no EM/F1 claimed)",
    )
    ap.add_argument("--json-out", type=Path, required=True)
    ap.add_argument("--md-out", type=Path, required=True)
    args = ap.parse_args(argv)

    payload = build_payload(
        args.manifest,
        args.raw.read_text(),
        k=args.k,
        term_cond=args.term_cond,
        reader_kind=args.reader,
    )

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, indent=2))
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(payload))

    e = payload["evidence"]
    a = payload["answer"]
    print(
        f"[graphrag-live-report] {payload['n_questions']} questions graded: "
        f"evidence recall={e['recall']:.3f} joint={e['joint']:.3f} f1={e['f1']:.3f}"
    )
    if a["mode"] == "reader":
        print(
            f"[graphrag-live-report] answer ({a['reader']}): "
            f"EM={a['em']:.3f} F1={a['f1']:.3f} over {a['n']} questions"
        )
    else:
        print(f"[graphrag-live-report] {a['reason']}")
    print(f"[graphrag-live-report] wrote {args.json_out} + {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

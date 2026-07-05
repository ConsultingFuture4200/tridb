"""Full-wiki HotpotQA retrieval harness — retrieve-from-ALL-Wikipedia (spec Phase 3).

The dev-slice GraphRAG report (bench/graphrag_report.py) grades each question against
its own 10-paragraph HotpotQA `context`. This harness does the real task the spec
asks for: the candidate pool is the ENTIRE wiki corpus (a tools/wiki_extract
manifest), the graph is the REAL Wikipedia hyperlink graph (not the title-mention
proxy), and gold evidence is resolved into corpus article ids by
tools/wiki_hotpot_link. It grades multi-hop **joint evidence recall@k** over all of
wiki — the honest fullwiki setting where gold is present for only a fraction of
questions.

SPLIT OF MEASURABLE-HERE vs GATED (identical policy to bench/tjs_open_live.py +
scripts/bench_graphrag.sh):
  * host-side (no engine): evidence recall@k of vector_only vs graph_inject over the
    corpus, graded vs the resolved gold. Runs HERE on a slice (e.g. simplewiki
    --max-articles). Reuses bench/graphrag_report's retrievers + grading unchanged.
  * GX10/engine-gated: the LIVE `tjs_open` latency, SM-3 candidates-examined /
    pages-touched, and latency-at-fixed-accuracy. This module only EMITS the
    engine-gated SQL (--emit-sql, reusing bench/tjs_open_live.emit_sql, the single
    operator-SQL source) and NEVER fabricates a live number off-target.

SCALE: full-7M-READY, slice-runnable. On a slice the corpus is embedded here with
fastembed (BGE-768, the pinned Plan-015 encoder). At 6.8M the embeddings come from
the Spark GPU (spec Phase 1) and are passed in via --corpus-emb/--query-emb .npy;
the recall grading is still host-side numpy (the Spark's 128 GB is the point). The
inline-INSERT SQL emitter is a SLICE convenience — at scale the load is COPY + bulk
native-graph staging (docs/wiki_scale_load_design_v0.1.0.md), then only the per-
question tjs_open SELECTs run live.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# Reuse the established retrievers + grading (single source of truth) so the full-wiki
# numbers are produced by the exact same code path as the dev-slice report.
from bench.graphrag_report import RETRIEVERS, Slice, _mean, evidence_scores
from tools.wiki_hotpot_link import coverage, link_questions, load_title_index


def load_corpus(
    manifest_dir: Path, *, want_text: bool = False
) -> tuple[list[dict], dict[int, list[int]]]:
    """Stream a wiki_extract manifest into (articles[id]={id,title[,text]}, out_adj).

    Article ids are 0-based contiguous in encounter order (manifest contract), so the
    returned list is indexed BY id: articles[i] is article i, which is exactly what
    the graphrag retrievers assume (returned ids index corpus_emb). We verify
    contiguity rather than trust it.

    Article TEXT is retained ONLY when `want_text` — the slice path that embeds the
    corpus here with fastembed (embed_corpus). The recall grade and the graph
    retrievers read only corpus_emb + out_adj + gold_ids, never text, so at 6.8M with
    precomputed --corpus-emb we keep tens of GB of stripped article bodies OUT of the
    128 GB working set the benchmark is built to stress (the Phase-4 personal-wiki
    reader re-reads bodies from the shards on demand).
    """
    manifest = json.loads((manifest_dir / "manifest.json").read_text())
    by_id: dict[int, dict] = {}
    for shard in manifest["shards"]["articles"]["files"]:
        with (manifest_dir / shard["path"]).open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                aid = int(rec["id"])
                art = {"id": aid, "title": rec["title"]}
                if want_text:
                    art["text"] = rec["text"]
                by_id[aid] = art
    n = len(by_id)
    if set(by_id) != set(range(n)):
        raise SystemExit(
            "wiki corpus article ids are not contiguous 0..n-1 — the recall grading "
            "assumes id == corpus position (manifest contract). Aborting."
        )
    paragraphs = [by_id[i] for i in range(n)]
    out_adj: dict[int, list[int]] = {}
    for shard in manifest["shards"]["edges"]["files"]:
        with (manifest_dir / shard["path"]).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                s, _, d = line.partition("\t")
                out_adj.setdefault(int(s), []).append(int(d))
    return paragraphs, out_adj


def embed_corpus(
    paragraphs: list[dict], queries: list[str]
) -> tuple[np.ndarray, np.ndarray]:
    """Embed corpus (title+text) + queries with the pinned BGE-768 encoder, cosine.

    Reuses tools.hotpot_corpus.Embedder / _passage_text / query instruction so the
    encoder matches the dev-slice run exactly. GX10 note: at 6.8M this is the GPU
    Phase-1 step — pass precomputed .npy instead (see --corpus-emb/--query-emb).
    """
    from tools.hotpot_corpus import BGE_QUERY_INSTRUCTION, Embedder, _passage_text

    embedder = Embedder()
    passages = [_passage_text(p["title"], p["text"]) for p in paragraphs]
    corpus_emb = embedder.encode(passages)
    query_emb = embedder.encode([BGE_QUERY_INSTRUCTION + q for q in queries])
    corpus_emb /= np.linalg.norm(corpus_emb, axis=1, keepdims=True) + 1e-12
    query_emb /= np.linalg.norm(query_emb, axis=1, keepdims=True) + 1e-12
    return corpus_emb, query_emb


def build_slice(
    manifest_dir: Path,
    slice_path: Path,
    k: int,
    *,
    corpus_emb_path: Path | None,
    query_emb_path: Path | None,
) -> tuple[Slice, dict]:
    """Assemble a graphrag_report.Slice over the full-wiki corpus + gradeable questions.

    Gradeable = questions whose gold titles ALL resolve into the corpus (the subset on
    which retrieve-from-all-wiki recall is well defined). Returns (slice, coverage).
    """
    # Text is needed only on the embed-here path; with precomputed --corpus-emb/
    # --query-emb we never touch article bodies, so drop them from the working set.
    want_text = not (corpus_emb_path and query_emb_path)
    paragraphs, out_adj = load_corpus(manifest_dir, want_text=want_text)
    title_to_id, redirects = load_title_index(manifest_dir)
    raw = json.loads(slice_path.read_text())["questions"]
    linked = link_questions(raw, title_to_id, redirects)
    cov = coverage(linked)

    # Only fully-resolved questions are gradeable; keep the original index (orig_qid)
    # to slice precomputed query embeddings, then reindex qid 0..Q-1 so query_emb and
    # the retrievers (which index query_emb[qid]) stay aligned.
    gradeable = [q for q in linked if q["fully_resolved"]]
    if not gradeable:
        raise SystemExit(
            "no HotpotQA question fully resolved into this corpus — nothing to grade. "
            "Use a larger corpus (full enwiki) or more questions."
        )
    for new_qid, q in enumerate(gradeable):
        q["orig_qid"] = q["qid"]
        q["qid"] = new_qid

    if corpus_emb_path and query_emb_path:
        corpus_emb = np.load(corpus_emb_path)
        query_emb_all = np.load(query_emb_path)
        if corpus_emb.shape[0] != len(paragraphs):
            raise SystemExit(
                f"--corpus-emb rows ({corpus_emb.shape[0]}) != articles "
                f"({len(paragraphs)}); embeddings must be id-aligned."
            )
        # --query-emb holds embeddings for ALL slice questions in original order;
        # select the gradeable subset (undefined otherwise).
        if query_emb_all.shape[0] != len(linked):
            raise SystemExit(
                f"--query-emb rows ({query_emb_all.shape[0]}) != slice questions "
                f"({len(linked)}); it must be id-aligned to the full question list."
            )
        query_emb = query_emb_all[[q["orig_qid"] for q in gradeable]]
    else:
        corpus_emb, query_emb = embed_corpus(
            paragraphs, [q["question"] for q in gradeable]
        )

    sl = Slice(
        corpus_emb=corpus_emb,
        query_emb=query_emb,
        paragraphs=paragraphs,
        questions=gradeable,
        out_adj=out_adj,
        k=k,
    )
    return sl, cov


def sweep(sl: Slice, ks: list[int]) -> dict:
    """Evidence recall + joint over k, per retriever, overall + by question type.

    A local copy of bench/graphrag_report.sweep's reduction (kept here so the full-
    wiki harness has no hidden dependency on that module's default k list) — same
    metric, same retrievers.
    """
    from collections import defaultdict

    acc: dict = {
        name: {k: defaultdict(lambda: {"recall": [], "joint": []}) for k in ks}
        for name in RETRIEVERS
    }
    for q in sl.questions:
        qi = q["qid"]
        for name, fn in RETRIEVERS.items():
            for k in ks:
                ev = evidence_scores(fn(sl, qi, k), q["gold_ids"])
                for grp in ("all", q["type"]):
                    acc[name][k][grp]["recall"].append(ev["recall"])
                    acc[name][k][grp]["joint"].append(ev["joint"])
    red: dict = {}
    for name, byk in acc.items():
        red[name] = {}
        for k, bygrp in byk.items():
            red[name][k] = {
                grp: {"recall": _mean(d["recall"]), "joint": _mean(d["joint"])}
                for grp, d in bygrp.items()
            }
    return red


def render_md(sw: dict, sl: Slice, cov: dict, ks: list[int], headline_k: int) -> str:
    types = sorted({q["type"] for q in sl.questions})
    grp = "bridge" if "bridge" in types else "all"
    vj = sw["vector_only"][headline_k][grp]["joint"]
    gj = sw["graph_inject"][headline_k][grp]["joint"]
    lines: list[str] = []
    w = lines.append
    w("# TriDB Benchmark — Full-Wiki HotpotQA Retrieval (spec Phase 3, host-side)")
    w("")
    w(
        f"**Retrieve-from-ALL-Wikipedia, real hyperlink graph.** Over a {sl.n:,}-article "
        f"wiki corpus (tools/wiki_extract), injecting REAL Wikipedia hyperlink bridges "
        f"into the retrieved set changes multi-hop **joint** evidence recall@{headline_k} "
        f"on `{grp}` questions by **{gj - vj:+.1%}** vs vector-only "
        f"({vj:.1%} -> {gj:.1%}), graded on {len(sl.questions)} fully-resolved questions."
    )
    w("")
    w(
        "- **Candidate pool = the entire corpus** (not the per-question 10-paragraph "
        "HotpotQA context the dev-slice report uses). This is the honest fullwiki task."
    )
    w(
        "- **Graph = REAL `[[wikilink]]` edges** (redirect-resolved, embedding-"
        "independent), not the title-mention proxy of the dev slice."
    )
    w("- Embeddings = BGE-base-en-v1.5 (dim 768), cosine (pinned Plan-015 encoder).")
    w("")
    w("## Gold-resolution coverage (the honest denominator)")
    w("")
    w(
        f"- Questions: {cov['n_questions']} · fully-resolved (all gold in corpus): "
        f"**{cov['n_fully_resolved']} ({cov['frac_fully_resolved']:.1%})** · "
        f"partially-resolved: {cov['n_partially_resolved']}."
    )
    w(
        f"- Gold titles resolved: {cov['gold_titles_resolved']}/"
        f"{cov['gold_titles_total']} ({cov['frac_gold_titles_resolved']:.1%})."
    )
    w(
        "- Recall is graded ONLY on fully-resolved questions (retrieve-from-all-wiki is "
        "undefined when a gold paragraph is not in the corpus). A slice resolves few; "
        "full enwiki resolves most — coverage is a corpus-size signal, reported not hidden."
    )
    w("")
    w(f"## Evidence recall vs k — `{grp}` questions (no LLM, host-computable)")
    w("")
    w("| k | " + " | ".join(f"{n} joint" for n in RETRIEVERS) + " | inject lift |")
    w("|---:|" + "---:|" * (len(RETRIEVERS) + 1))
    for k in ks:
        cells = [f"{sw[n][k][grp]['joint']:.3f}" for n in RETRIEVERS]
        lift = sw["graph_inject"][k][grp]["joint"] - sw["vector_only"][k][grp]["joint"]
        w(f"| {k} | " + " | ".join(cells) + f" | {lift:+.3f} |")
    w("")
    w("Any-gold recall@k (same group):")
    w("")
    w("| k | " + " | ".join(f"{n}" for n in RETRIEVERS) + " | inject lift |")
    w("|---:|" + "---:|" * (len(RETRIEVERS) + 1))
    for k in ks:
        cells = [f"{sw[n][k][grp]['recall']:.3f}" for n in RETRIEVERS]
        lift = (
            sw["graph_inject"][k][grp]["recall"] - sw["vector_only"][k][grp]["recall"]
        )
        w(f"| {k} | " + " | ".join(cells) + f" | {lift:+.3f} |")
    w("")
    w("## What is GATED (never claimed here)")
    w("")
    w(
        "- **Live `tjs_open` latency + SM-3 candidates-examined / pages-touched** at "
        "fixed accuracy — GX10/engine-gated. Emit the operator SQL with `--emit-sql` and "
        "run it on the Spark (`scripts/graph_test.sh`); this harness refuses to fabricate "
        "a latency number off-target."
    )
    w(
        "- **The at-scale (6.8M) corpus embeddings** come from the Spark GPU (spec Phase "
        "1); pass them via `--corpus-emb/--query-emb`. The inline-INSERT SQL here is a "
        "SLICE convenience — at scale load via COPY + bulk native-graph staging "
        "(`docs/wiki_scale_load_design_v0.1.0.md`), then run only the tjs_open SELECTs."
    )
    w(
        "- The spec's honest failure mode stands: if fused `tjs_open` is not faster at "
        "I/O-bound scale, TriDB's value is one-WAL consistency, not speed. This host-side "
        "report measures RETRIEVAL QUALITY only; it does NOT pre-announce a latency win."
    )
    w("")
    w(
        "_Generated by `bench/wiki_scale_report.py` (`make wiki-scale`). Numbers observed._"
    )
    return "\n".join(lines) + "\n"


def emit_engine_sql(
    sl: Slice, out_path: Path, *, k: int, seeds: int, hops: int, term_cond: int
) -> None:
    """Write the GX10-gated tjs_open SQL, reusing bench/tjs_open_live.emit_sql.

    Builds the manifest shape that emitter expects (paragraphs / _edges / questions
    with qid+gold_ids) from the Slice. SLICE-SCALE ONLY: it inlines every vector as an
    INSERT — fine for a smoke slice, absurd at 6.8M (use COPY, see the load design doc).
    """
    from bench.tjs_open_live import emit_sql

    edges = [[s, d] for s, adj in sl.out_adj.items() for d in adj]
    m = {
        "paragraphs": sl.paragraphs,
        "_edges": edges,
        "questions": [
            {"qid": q["qid"], "gold_ids": q["gold_ids"]} for q in sl.questions
        ],
    }
    out_path.write_text(
        emit_sql(
            m,
            sl.corpus_emb,
            sl.query_emb,
            k=k,
            seeds=seeds,
            hops=hops,
            term_cond=term_cond,
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Full-wiki HotpotQA retrieve-from-all recall harness."
    )
    ap.add_argument(
        "--wiki-manifest-dir",
        type=Path,
        default=Path("data/wiki/simplewiki_slice"),
        help="tools/wiki_extract manifest dir (the corpus)",
    )
    ap.add_argument("--slice", type=Path, default=Path("data/hotpot/dev_slice.json"))
    ap.add_argument("--ks", type=int, nargs="+", default=[2, 3, 5, 10])
    ap.add_argument("--headline-k", type=int, default=5)
    ap.add_argument(
        "--corpus-emb", type=Path, help="precomputed corpus .npy (GPU Phase-1)"
    )
    ap.add_argument("--query-emb", type=Path, help="precomputed query .npy")
    ap.add_argument(
        "--emit-sql", type=Path, help="write GX10-gated tjs_open SQL here (slice-scale)"
    )
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--term-cond", type=int, default=0)
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/results/wiki_scale_metrics.json")
    )
    ap.add_argument(
        "--md-out",
        type=Path,
        default=Path("bench/results/wiki_scale_report.md"),
        help="generated recall table (the authored plan doc is "
        "docs/benchmark_wiki_scale_v0.1.0.md)",
    )
    args = ap.parse_args(argv)

    ks = sorted(set(args.ks) | {args.headline_k})
    sl, cov = build_slice(
        args.wiki_manifest_dir,
        args.slice,
        k=args.headline_k,
        corpus_emb_path=args.corpus_emb,
        query_emb_path=args.query_emb,
    )
    print(
        f"[wiki-scale] corpus={sl.n} articles, edges={sum(len(v) for v in sl.out_adj.values())}, "
        f"gradeable={len(sl.questions)}/{cov['n_questions']} questions "
        f"({cov['frac_fully_resolved']:.1%} fully-resolved), ks={ks}"
    )
    sw = sweep(sl, ks)

    if args.emit_sql:
        emit_engine_sql(
            sl,
            args.emit_sql,
            k=args.headline_k,
            seeds=args.seeds,
            hops=args.hops,
            term_cond=args.term_cond,
        )
        print(
            f"[wiki-scale] wrote GX10-gated tjs_open SQL -> {args.emit_sql} "
            "(slice-scale inline load; at 6.8M use COPY, see the load design doc)"
        )

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(
            {
                "wiki_manifest_dir": str(args.wiki_manifest_dir),
                "n_articles": sl.n,
                "ks": ks,
                "headline_k": args.headline_k,
                "coverage": cov,
                "n_gradeable": len(sl.questions),
                "sweep": sw,
            },
            indent=2,
        )
    )
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(sw, sl, cov, ks, args.headline_k))

    grp = "bridge" if any(q["type"] == "bridge" for q in sl.questions) else "all"
    vj = sw["vector_only"][args.headline_k][grp]["joint"]
    gj = sw["graph_inject"][args.headline_k][grp]["joint"]
    print(
        f"[wiki-scale] joint recall@{args.headline_k} ({grp}): vector={vj:.3f} "
        f"graph_inject={gj:.3f} lift={gj - vj:+.3f} -> {args.md_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

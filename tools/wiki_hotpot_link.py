"""Link HotpotQA gold/support titles to FULL-WIKI article ids (Phase 3, spec §3).

The dev-slice GraphRAG harness (tools/hotpot_corpus.py + bench/graphrag_report.py)
grades evidence recall against each question's OWN 10-paragraph `context` — a
self-contained candidate pool where the gold is guaranteed present. That measures
retrieval quality but NOT the real task: *retrieve the evidence from all of
Wikipedia*. This module bridges the two by resolving each HotpotQA question's gold
supporting titles into article ids of a wiki corpus produced by tools/wiki_extract
(the manifest contract), so the full-wiki harness (bench/wiki_scale_report.py) can
grade multi-hop joint evidence recall@k over the ENTIRE corpus.

It mirrors `attach_questions` in tools/build_wiki_graph.py — same "resolve gold
supporting titles to corpus ids" job — but keyed on the REAL Wikipedia title space
(tools/wiki_extract.normalize_title + redirect resolution) instead of the slice's
lossy token key. Reusing wiki_extract's `normalize_title`/`resolve_edge` guarantees
the linker keys into EXACTLY the id space the extractor's edges/redirects use, so a
resolved gold id is the same vertex the graph leg traverses.

Not every HotpotQA gold title exists in a given corpus: a simplewiki slice omits
most, and even full enwiki has title drift vs the HotpotQA 2017 dump. A question is
`fully_resolved` only when ALL its gold titles land on corpus articles — that is the
subset on which retrieve-from-all-wiki recall is well defined (the fullwiki-config
reality: gold present for only a fraction of questions). Coverage is reported, never
hidden.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.wiki_extract import normalize_title, resolve_edge


def load_title_index(manifest_dir: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Rebuild (title_to_id, redirects) from a wiki_extract manifest directory.

    title_to_id: normalized article title -> id, streamed from the article shards
    (the manifest contract's documented reconstruction). redirects: source_title ->
    target_title from redirects.tsv (already normalized keys). Together they are the
    same maps the extractor's pass 1 held, so any title resolves identically.
    """
    manifest = json.loads((manifest_dir / "manifest.json").read_text())
    title_to_id: dict[str, int] = {}
    for shard in manifest["shards"]["articles"]["files"]:
        with (manifest_dir / shard["path"]).open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                title_to_id[normalize_title(rec["title"])] = int(rec["id"])
    redirects: dict[str, str] = {}
    rpath = manifest_dir / "redirects.tsv"
    if rpath.exists():
        with rpath.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                src, _, dst = line.partition("\t")
                if dst:
                    redirects[src] = dst
    return title_to_id, redirects


def resolve_title(
    title: str, title_to_id: dict[str, int], redirects: dict[str, str]
) -> int | None:
    """Resolve one raw title to a corpus article id, following redirects (or None).

    Pure: normalize the title into the extractor's key space, then reuse
    wiki_extract.resolve_edge (direct hit -> redirect chain -> real article). Returns
    None for a title absent from the corpus (and not reachable via a redirect).
    """
    key = normalize_title(title)
    if not key:
        return None
    return resolve_edge(key, title_to_id, redirects)


def link_questions(
    questions: list[dict],
    title_to_id: dict[str, int],
    redirects: dict[str, str],
) -> list[dict]:
    """Resolve every HotpotQA question's gold supporting titles to wiki ids.

    `questions` are the raw fetch_hotpot rows (each with `supporting_facts` =
    list[[title, sent_id]]). Mirrors build_wiki_graph.attach_questions but in the
    real-wiki id space and records resolution coverage: `gold_ids` are the resolved
    corpus ids, `fully_resolved` is True iff every gold title resolved (the subset
    the full-wiki recall metric is defined on).
    """
    out: list[dict] = []
    for qi, q in enumerate(questions):
        gold_titles = sorted({t for t, _ in q["supporting_facts"]})
        gold_ids: list[int] = []
        for t in gold_titles:
            aid = resolve_title(t, title_to_id, redirects)
            if aid is not None and aid not in gold_ids:
                gold_ids.append(aid)
        out.append(
            {
                "qid": qi,
                "hotpot_id": q["id"],
                "question": q["question"],
                "answer": q["answer"],
                "type": q.get("type", ""),
                "level": q.get("level", ""),
                "gold_titles": gold_titles,
                "gold_ids": gold_ids,
                "n_gold": len(gold_titles),
                "n_gold_resolved": len(gold_ids),
                "fully_resolved": len(gold_ids) == len(gold_titles),
            }
        )
    return out


def coverage(linked: list[dict]) -> dict:
    """Resolution coverage summary (honest denominator for the recall metric)."""
    n = len(linked)
    full = sum(1 for q in linked if q["fully_resolved"])
    any_hit = sum(1 for q in linked if q["gold_ids"])
    gold_total = sum(q["n_gold"] for q in linked)
    gold_hit = sum(q["n_gold_resolved"] for q in linked)
    return {
        "n_questions": n,
        "n_fully_resolved": full,
        "n_partially_resolved": any_hit,
        "frac_fully_resolved": (full / n) if n else 0.0,
        "gold_titles_total": gold_total,
        "gold_titles_resolved": gold_hit,
        "frac_gold_titles_resolved": (gold_hit / gold_total) if gold_total else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Link HotpotQA gold titles to full-wiki article ids."
    )
    ap.add_argument(
        "--wiki-manifest-dir",
        type=Path,
        required=True,
        help="directory holding a tools/wiki_extract manifest.json + shards",
    )
    ap.add_argument(
        "--slice",
        type=Path,
        default=Path("data/hotpot/dev_slice.json"),
        help="HotpotQA dev slice from tools/fetch_hotpot",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("data/wiki/hotpot_link.json"),
        help="output eval-mapping JSON",
    )
    args = ap.parse_args(argv)

    questions = json.loads(args.slice.read_text())["questions"]
    title_to_id, redirects = load_title_index(args.wiki_manifest_dir)
    linked = link_questions(questions, title_to_id, redirects)
    cov = coverage(linked)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "wiki_manifest_dir": str(args.wiki_manifest_dir),
                "slice": str(args.slice),
                "n_articles": len(title_to_id),
                "n_redirects": len(redirects),
                "coverage": cov,
                "questions": linked,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        f"[wiki_hotpot_link] {cov['n_questions']} questions vs {len(title_to_id)} "
        f"articles: {cov['n_fully_resolved']} fully-resolved "
        f"({cov['frac_fully_resolved']:.1%}), "
        f"{cov['frac_gold_titles_resolved']:.1%} of gold titles hit -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

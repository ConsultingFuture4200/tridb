"""Build the REAL, embedding-independent graph for the HotpotQA benchmark (Plan 015).

THE CRUX OF PLAN 015
--------------------
`tools/real_corpus.py` synthesizes the graph from vector proximity, so "graph-
constrained vector search" is tested against topology that is a function of the
very vectors it constrains — it cannot show that REAL topology adds information.
This module builds a graph whose edges come from SURFACE TEXT, not embeddings:

    edge A -> B   iff   the title of paragraph B occurs (whole-word) in A's text.

That is a title-MENTION graph. Wikipedia hyperlinks are, in the overwhelming
majority, exactly title mentions (an article links to another by naming it), so
this is a faithful, reachable proxy for the fullwiki hyperlink graph WITHOUT the
gated/dead Wikipedia-with-links dump. It is stated honestly as a proxy in the
report. Crucially it is EMBEDDING-INDEPENDENT: rebuild it with a different encoder
and the edges do not move. That is what makes the vector-only-vs-graph ablation a
real test of the GraphRAG thesis on this slice.

(The GX10/fullwiki path in scripts/bench_graphrag.sh can swap this proxy for the
official hyperlink dump when that corpus is available on-target; the manifest
records which edge source was used.)

OUTPUT
------
- corpus: list of paragraphs (id, title, text, sentences) — the de-duplicated
  union of every question's 10-paragraph context (the dev-slice candidate pool).
- edges: list of (src_id, dst_id) directed mention edges (mirrors tjs out-edges).
- per-question gold: the gold paragraph ids (from supporting_facts) used to grade
  evidence recall, plus the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens (titles + text tokenized identically)."""
    return _WORD.findall(text.lower())


def _norm_title(title: str) -> str:
    """Whitespace-normalized token key for a title (matches _tokens join)."""
    return " ".join(_tokens(title))


@dataclass
class Paragraph:
    id: int
    title: str
    sentences: list[str]
    text: str


@dataclass
class HotpotCorpus:
    paragraphs: list[Paragraph]
    edges: list[tuple[int, int]]
    title_to_id: dict[str, int]
    # qid -> {"question","answer","gold_ids","gold_titles","type","level","seed_title"}
    questions: list[dict] = field(default_factory=list)


def build_corpus(questions: list[dict]) -> tuple[list[Paragraph], dict[str, int]]:
    """De-duplicate every question's context paragraphs into one corpus.

    Keyed by normalized title; first occurrence wins (HotpotQA ships identical
    text for a title across questions). Returns (paragraphs, title_key -> id).
    """
    title_to_id: dict[str, int] = {}
    paragraphs: list[Paragraph] = []
    for q in questions:
        for title, sents in q["context"]:
            key = _norm_title(title)
            if not key or key in title_to_id:
                continue
            pid = len(paragraphs)
            title_to_id[key] = pid
            paragraphs.append(
                Paragraph(
                    id=pid,
                    title=title,
                    sentences=list(sents),
                    text=" ".join(sents).strip(),
                )
            )
    return paragraphs, title_to_id


def build_mention_edges(
    paragraphs: list[Paragraph],
    title_to_id: dict[str, int],
    *,
    min_title_tokens: int = 1,
) -> list[tuple[int, int]]:
    """Directed title-mention edges A->B (B's title occurs whole-word in A's text).

    Efficient: index titles by their token tuple, then slide every n-gram (up to
    the longest title length) over each paragraph's tokens and test set membership
    — O(corpus_tokens * max_title_len), not O(n^2) substring scans.
    """
    # title token-string -> id, plus the set of distinct title lengths to slide.
    by_key = {
        k: i for k, i in title_to_id.items() if len(k.split()) >= min_title_tokens
    }
    if not by_key:
        return []
    maxlen = max(len(k.split()) for k in by_key)
    edges: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for para in paragraphs:
        toks = _tokens(para.text)
        own = title_to_id.get(_norm_title(para.title))
        n = len(toks)
        for L in range(1, maxlen + 1):
            if L > n:
                break
            for i in range(0, n - L + 1):
                gram = " ".join(toks[i : i + L])
                dst = by_key.get(gram)
                if dst is None or dst == own:
                    continue
                e = (para.id, dst)
                if e not in seen:
                    seen.add(e)
                    edges.append(e)
    return edges


def attach_questions(questions: list[dict], title_to_id: dict[str, int]) -> list[dict]:
    """Resolve each question's gold supporting titles to corpus paragraph ids.

    `seed_title` (the gold paragraph most lexically aligned with the question) is
    only advisory; retrieval seeds from the vector hit, never from gold.
    """
    out: list[dict] = []
    for qi, q in enumerate(questions):
        gold_titles = sorted({t for t, _ in q["supporting_facts"]})
        gold_ids = [
            title_to_id[_norm_title(t)]
            for t in gold_titles
            if _norm_title(t) in title_to_id
        ]
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
                "n_gold_resolved": len(gold_ids),
                "n_gold": len(gold_titles),
            }
        )
    return out


def build_graph(questions: list[dict]) -> HotpotCorpus:
    """Full Phase-2 build: corpus + real mention edges + resolved gold."""
    paragraphs, title_to_id = build_corpus(questions)
    edges = build_mention_edges(paragraphs, title_to_id)
    q = attach_questions(questions, title_to_id)
    return HotpotCorpus(
        paragraphs=paragraphs,
        edges=edges,
        title_to_id=title_to_id,
        questions=q,
    )

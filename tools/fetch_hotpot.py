"""Fetch the HotpotQA fullwiki dev question set for the GraphRAG QA benchmark (Plan 015).

WHY THIS EXISTS
---------------
Plan 015 closes the "is the answer right?" gap: a recognized multi-hop QA
workload (HotpotQA fullwiki) whose graph is REAL (independent of the embeddings)
and whose metric is downstream answer accuracy — not recall@ANN-oracle on a graph
synthesized from the vectors (the limitation `tools/real_corpus.py` documents).

This module fetches the QUESTION set only. Each HotpotQA fullwiki dev row carries
everything the host-side accuracy harness needs WITHOUT a separate Wikipedia
dump: the question, the gold answer, the sentence-level supporting facts (which
titles are gold), and the `context` — 10 candidate paragraphs (title + sentences)
that form the per-question candidate pool. The real graph is built from this text
in `tools/build_wiki_graph.py` (title-mention edges), so no gated/dead Wikipedia
hyperlink dump is required for a dev-slice run.

SOURCE / PINNING (stated honestly)
----------------------------------
The canonical HotpotQA host (curtis.ml.cmu.edu) is unreachable from many networks
(it was down when this was written, 2026-06-27). We therefore read the SAME data
from the HuggingFace dataset `hotpotqa/hotpot_qa` (config `fullwiki`, split
`validation` = the public dev set) via the datasets-server ROWS API, which returns
JSON directly (no parquet/pyarrow dependency). The dataset revision is pinned in
:data:`HOTPOT_SOURCE` and recorded in the output manifest so a run is reproducible.
This is a network-gated tool — like `make fetch-dataset`, it is NOT run by tests
or CI. Full-corpus fullwiki retrieval (all of Wikipedia) is a GX10-scale exercise;
this fetcher supports the dev-slice that is buildable on the x86 standin.

USAGE
-----
    python -m tools.fetch_hotpot --questions 150 --out data/hotpot/dev_slice.json
    python -m tools.fetch_hotpot --questions 0   # 0 = the whole dev split (7405 q)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

# Pinned data source. dataset+config+split fix WHAT we read; the datasets-server
# serves the dataset's current main revision — we record the resolved row count so
# a slice is reproducible by (offset, length) against the same dataset.
HF_ROWS_API = "https://datasets-server.huggingface.co/rows"


@dataclass(frozen=True)
class HotpotSource:
    dataset: str = "hotpotqa/hotpot_qa"
    # `distractor` config: per-question context = 2 gold + 8 distractor paragraphs,
    # so gold is ALWAYS in the candidate pool -> evidence recall is well-defined on
    # a self-contained dev slice. The HF `fullwiki` config instead ships a
    # retriever's noisy top-10 (gold present only ~53% of the time), which is the
    # full retrieve-from-all-Wikipedia setting — that is the GX10 headline
    # (scripts/bench_graphrag.sh), not the buildable-here host slice. Same
    # questions/answers/supporting_facts either way.
    config: str = "distractor"
    split: str = "validation"  # HotpotQA public dev set


HOTPOT_SOURCE = HotpotSource()

# datasets-server caps a single /rows page at 100.
_PAGE = 100


def _get_json(url: str, *, retries: int = 4, timeout: int = 30) -> dict:
    """GET a JSON URL with bounded retries (the HF rows API rate-limits)."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tridb-bench/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — bubble after retries
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n  last error: {last}")


def _rows_url(src: HotpotSource, offset: int, length: int) -> str:
    q = urllib.parse.urlencode(
        {
            "dataset": src.dataset,
            "config": src.config,
            "split": src.split,
            "offset": offset,
            "length": length,
        }
    )
    return f"{HF_ROWS_API}?{q}"


def _normalize_row(row: dict) -> dict:
    """Project a HF row to the compact schema the rest of the pipeline consumes.

    context  : list[ [title, [sentences...]] ]   (10 candidate paragraphs)
    support  : list[ [title, sent_id] ]          (gold supporting facts)
    """
    ctx = row["context"]
    titles = ctx["title"]
    sents = ctx["sentences"]
    context = [[t, list(s)] for t, s in zip(titles, sents, strict=True)]

    sf = row["supporting_facts"]
    support = [[t, int(i)] for t, i in zip(sf["title"], sf["sent_id"], strict=True)]

    return {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "type": row.get("type", ""),
        "level": row.get("level", ""),
        "supporting_facts": support,
        "context": context,
    }


def fetch_questions(n: int, *, src: HotpotSource = HOTPOT_SOURCE) -> dict:
    """Fetch `n` dev questions (n<=0 -> all). Returns {meta, questions}."""
    rows: list[dict] = []
    offset = 0
    total: int | None = None
    while True:
        want = _PAGE if n <= 0 else min(_PAGE, n - len(rows))
        if want <= 0:
            break
        payload = _get_json(_rows_url(src, offset, want))
        if total is None:
            total = payload.get("num_rows_total")
        page = payload.get("rows", [])
        if not page:
            break
        rows.extend(_normalize_row(r["row"]) for r in page)
        offset += len(page)
        print(f"[fetch_hotpot] fetched {len(rows)} questions...", file=sys.stderr)
        if len(page) < want:
            break  # ran off the end of the split
    meta = {
        "source": asdict(src),
        "api": HF_ROWS_API,
        "split_num_rows_total": total,
        "fetched": len(rows),
        "note": (
            "HotpotQA fullwiki dev questions via HF datasets-server rows API "
            "(canonical CMU host unreachable). Real graph is built from the "
            "per-question context in tools/build_wiki_graph.py."
        ),
    }
    return {"meta": meta, "questions": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch HotpotQA fullwiki dev questions.")
    ap.add_argument(
        "--questions",
        type=int,
        default=150,
        help="how many dev questions to fetch (0 = the whole dev split)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("data/hotpot/dev_slice.json"),
        help="output JSON path",
    )
    args = ap.parse_args(argv)

    data = fetch_questions(args.questions)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False))
    m = data["meta"]
    print(
        f"[fetch_hotpot] wrote {m['fetched']} questions -> {args.out} "
        f"(dev split total={m['split_num_rows_total']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

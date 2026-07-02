"""Live engine recall for the tjs_open (B) operator (ADR-0012 realization B).

Sibling of bench/v2a_open.py, but instead of composing ANN ∪ graph.neighbors host-side
(the blocking realization A), this drives the REAL fused engine operator `tjs_open` on the
forked-MSVBASE engine and grades its top-k against gold. It is the reproducible engine-recall
harness for the operator (the value the original measurement got ad-hoc).

Flow (mirrors v2a_open):
  python -m bench.tjs_open_live --manifest data/hotpot/manifest.json --emit-sql /tmp/tjsopen.sql
  bash scripts/graph_test.sh tridb/msvbase:dev /tmp/tjsopen.sql > /tmp/tjsopen_raw.txt 2>&1
  python -m bench.tjs_open_live --manifest data/hotpot/manifest.json --raw /tmp/tjsopen_raw.txt

Recall graded host-side vs gold supporting paragraphs (joint multi-hop recall, same metric as
v2a_open). The engine run is GX10/engine-gated (needs tridb/msvbase:dev); recall itself is exact.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

_INT = re.compile(r"^\s*(\d+)\s*$")
_B = re.compile(r"#TJSOPEN IDS_BEGIN qid=(\d+)")
_E = re.compile(r"#TJSOPEN IDS_END qid=(\d+)")


def _vec(v) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def emit_sql(
    m: dict,
    corpus_emb,
    query_emb,
    *,
    k: int,
    seeds: int,
    hops: int,
    term_cond: int,
) -> str:
    n, dim = len(m["paragraphs"]), corpus_emb.shape[1]
    out: list[str] = []
    w = out.append
    w("\\set ON_ERROR_STOP on")
    w("CREATE EXTENSION vectordb;")
    w("CREATE EXTENSION graph_store;")
    w(
        f"CREATE TABLE entities (id bigint PRIMARY KEY, ts int, embedding float8[{dim}]);"
    )
    for i in range(0, n, 500):
        vals = ",".join(
            f"({j},0,'{_vec(corpus_emb[j])}'::float8[])"
            for j in range(i, min(i + 500, n))
        )
        w(f"INSERT INTO entities (id,ts,embedding) VALUES {vals};")
    w("CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)")
    w(f"    WITH (dimension = {dim}, distmethod = l2_distance);")
    for s, d in m["_edges"]:
        w(f"SELECT graph_store.add_edge({int(s)}, {int(d)});")
    for q in m["questions"]:
        qid = q["qid"]
        qv = _vec(query_emb[qid])
        w(f"\\echo #TJSOPEN IDS_BEGIN qid={qid}")
        # The fused engine operator: seedless ANN top-`seeds` -> hops-reachable graph union
        # (bridges) -> vector-ranked top-k with bridges injected past the frontier, early-terminating.
        w(
            f"SELECT id FROM tjs_open('entities', {k}, {term_cond}, {seeds}, {hops}, "
            f"'id', '', 'embedding <-> ''{qv}''') AS t(id bigint);"
        )
        w(f"\\echo #TJSOPEN IDS_END qid={qid}")
    w("\\echo #TJSOPEN DONE")
    return "\n".join(out) + "\n"


def parse(raw: str) -> dict:
    if "#TJSOPEN DONE" not in raw:
        raise SystemExit(
            "tjs_open_live transcript did not reach '#TJSOPEN DONE' — incomplete"
        )
    res: dict = {}
    cur = None
    for line in raw.splitlines():
        b = _B.search(line)
        if b:
            cur = int(b[1])
            res[cur] = []
            continue
        if _E.search(line):
            cur = None
            continue
        if cur is not None:
            mi = _INT.match(line)
            if mi:
                res[cur].append(int(mi[1]))
    return res


def grade(manifest: dict, parsed: dict, k: int) -> dict:
    gold = {q["qid"]: set(q.get("gold_ids", [])) for q in manifest["questions"]}
    recs = []
    for qid, ids in parsed.items():
        g = gold.get(qid, set())
        if g:
            recs.append(len(g & set(ids[:k])) / len(g))
    return {
        "k": k,
        "operator": "tjs_open",
        "n_queries": len(parsed),
        "recall_at_k": float(np.mean(recs)) if recs else float("nan"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="live tjs_open(B) engine recall.")
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--term-cond", type=int, default=0)
    ap.add_argument("--emit-sql", type=Path)
    ap.add_argument("--raw", type=Path)
    ap.add_argument(
        "--json-out",
        type=Path,
        default=Path("bench/results/tjs_open_live_metrics.json"),
    )
    args = ap.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    if args.emit_sql:
        ce = np.load(manifest["corpus_emb_path"])
        qe = np.load(manifest["query_emb_path"])
        args.emit_sql.write_text(
            emit_sql(
                manifest,
                ce,
                qe,
                k=args.k,
                seeds=args.seeds,
                hops=args.hops,
                term_cond=args.term_cond,
            )
        )
        print(
            f"[tjs_open_live] wrote SQL -> {args.emit_sql} "
            f"({len(manifest['paragraphs'])} paras, seeds={args.seeds}, hops={args.hops}, "
            f"term_cond={args.term_cond})"
        )
        return 0
    if not args.raw:
        ap.error("need --emit-sql or --raw")
    res = grade(manifest, parse(args.raw.read_text()), args.k)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(res, indent=2))
    print(
        f"[tjs_open_live] engine tjs_open recall@{args.k} = {res['recall_at_k']:.3f} "
        f"over {res['n_queries']} queries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

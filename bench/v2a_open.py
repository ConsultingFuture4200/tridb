"""v2 realization (A): open retrieval by COMPOSING engine primitives (ADR-0012).

Proves the forked-MSVBASE engine can do open-domain (seedless) graph-augmented
retrieval by composing what it already has — the HNSW ANN index (`ORDER BY embedding
<-> q LIMIT m`, the vector-seed leg) UNION `graph_store.neighbors()` (the graph leg),
vector-reranked — recovering the ~open-retrieval recall that the single-`src` `tjs()`
loses (0.22 in benchmark_h2h_v0.1.0.md) on the SAME HotpotQA corpus.

This is the ADR-0012 realization (A): a BLOCKING composition (it materialises seeds +
reachable set), so it is a REFERENCE/oracle, NOT a shippable operator — TR-1 (golden
rule 1) is satisfied only by the fused early-terminating `tjs_open` (realization B, the
GX10-gated fork patch). recall graded host-side vs gold; runs live on the x86 engine.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

_INT = re.compile(r"^\s*(\d+)\s*$")
_B = re.compile(r"#V2A IDS_BEGIN qid=(\d+)")
_E = re.compile(r"#V2A IDS_END qid=(\d+)")


def _vec(v) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def emit_sql(m: dict, corpus_emb, query_emb, *, k: int, seeds: int) -> str:
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
        w(f"\\echo #V2A IDS_BEGIN qid={qid}")
        # COMPOSITION: ANN-top-`seeds` (HNSW vector leg) UNION their graph neighbours
        # (graph leg), vector-reranked over the reachable union, top-k.
        w(
            f"WITH seeds AS (SELECT id FROM entities ORDER BY embedding <-> '{qv}' LIMIT {seeds}),"
        )
        w("  reach AS (")
        w("    SELECT id FROM seeds")
        w("    UNION")
        w(
            "    SELECT nb FROM seeds s CROSS JOIN LATERAL graph_store.neighbors(s.id) AS nb"
        )
        w("  ),")
        w("  ranked AS (")
        w("    SELECT e.id,")
        w("      (SELECT sum((e.embedding[i]-q[i])*(e.embedding[i]-q[i]))")
        w(
            f"       FROM generate_subscripts(e.embedding,1) i, (SELECT '{qv}'::float8[] q) qq) AS d2"
        )
        w("    FROM entities e JOIN reach r ON r.id = e.id")
        w("  )")
        w(f"SELECT id FROM ranked ORDER BY d2, id LIMIT {k};")
        w(f"\\echo #V2A IDS_END qid={qid}")
    w("\\echo #V2A DONE")
    return "\n".join(out) + "\n"


def parse(raw: str) -> dict:
    if "#V2A DONE" not in raw:
        raise SystemExit("v2a_open transcript did not reach '#V2A DONE' — incomplete")
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
        "n_queries": len(parsed),
        "recall_at_k": float(np.mean(recs)) if recs else float("nan"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="v2 realization A: open retrieval by composition."
    )
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--emit-sql", type=Path)
    ap.add_argument("--raw", type=Path)
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/results/v2a_open_metrics.json")
    )
    args = ap.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    if args.emit_sql:
        ce = np.load(manifest["corpus_emb_path"])
        qe = np.load(manifest["query_emb_path"])
        args.emit_sql.write_text(emit_sql(manifest, ce, qe, k=args.k, seeds=args.seeds))
        print(
            f"[v2a] wrote SQL -> {args.emit_sql} ({len(manifest['paragraphs'])} paras, seeds={args.seeds})"
        )
        return 0
    if not args.raw:
        ap.error("need --emit-sql or --raw")
    res = grade(manifest, parse(args.raw.read_text()), args.k)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(res, indent=2))
    print(
        f"[v2a] open-composition recall@{args.k} = {res['recall_at_k']:.3f} over {res['n_queries']} queries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

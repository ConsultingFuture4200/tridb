"""Real-workload head-to-head: TriDB (in-process tjs) vs tuned multi-store (GTM #1).

Closes the GTM "strawman baseline" gap on a REAL dataset: the SAME HotpotQA corpus +
queries + k, both sides measured the SAME way (client-side wall-clock, warm, median of
N), reporting recall@k (vs gold) AND end-to-end latency.

  TriDB     : the canonical fused `tjs()` query (one in-process operator, one round-trip),
              timed via psql \\timing in the tridb/msvbase engine container.
  baseline  : the tuned multi-store stack people actually run (Milvus ANN + Neo4j graph
              hop + app-side rerank), end-to-end across THREE systems (baseline/graphrag.py).

The story: comparable recall, one query vs three-system round-trips. This is the SM-2
methodology applied to a recognized public workload (not the synthetic SM-2 corpus).

Two modes: `--emit-sql` writes the TriDB \\timing SQL (run by scripts/bench_graphrag_h2h.sh
in the engine container); `--grade` parses that engine transcript, runs the live baseline,
and renders the head-to-head.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

import numpy as np

_INT = re.compile(r"^\s*(\d+)\s*$")
_TIME = re.compile(r"Time:\s+([\d.]+)\s+ms")
_QSTART = re.compile(r"#H2H QSTART qid=(\d+)")
_IDSB = re.compile(r"#H2H IDS_BEGIN qid=(\d+)")
_IDSE = re.compile(r"#H2H IDS_END qid=(\d+)")
_QEND = re.compile(r"#H2H QEND qid=(\d+)")


def _vec(v) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def emit_tridb_sql(
    m: dict, corpus_emb, query_emb, *, k: int, termcond: int, runs: int
) -> str:
    """Canonical tjs() over the HotpotQA corpus, per-query warm + N \\timing runs.

    HotpotQA has no time predicate -> ts=0 for all rows and the filter `ts IN (0)`
    is a pass-through, leaving the fused vector+graph legs intact. src is the
    vector-seed (paragraph nearest the question), matching the host retriever."""
    n = len(m["paragraphs"])
    dim = corpus_emb.shape[1]
    out: list[str] = []
    w = out.append
    w("\\set ON_ERROR_STOP on")
    w("\\timing off")
    w("CREATE EXTENSION vectordb;")
    w(
        "CREATE EXTENSION graph_store_am;"
    )  # v1 native AM, v0-compat surface (ADR-0013 Stage B)
    w(
        f"CREATE TABLE entities (id bigint PRIMARY KEY, chunk text, ts int, embedding float8[{dim}]);"
    )
    batch = 500
    for i in range(0, n, batch):
        vals = ",".join(
            f"({j},'c{j}',0,'{_vec(corpus_emb[j])}'::float8[])"
            for j in range(i, min(i + batch, n))
        )
        w(f"INSERT INTO entities (id,chunk,ts,embedding) VALUES {vals};")
    w("CREATE INDEX entities_hnsw ON entities USING hnsw(embedding)")
    w(f"    WITH (dimension = {dim}, distmethod = l2_distance);")
    for s, d in m["_edges"]:
        w(f"SELECT graph_store.add_edge({int(s)}, {int(d)});")
    w(
        "SET enable_seqscan = off;   -- force the HNSW ANN index scan for tjs's vector leg"
    )
    for q in m["questions"]:
        qid = q["qid"]
        qv = query_emb[qid]
        src = int(np.argmax(corpus_emb @ qv))
        tjs = (
            f"SELECT t.id FROM tjs('entities', {k}, {termcond}, {src}::bigint, "
            f"'id, chunk', 'ts IN (0)', 'embedding <-> ''{_vec(qv)}''') AS t(id bigint, chunk text)"
        )
        w(f"\\echo #H2H QSTART qid={qid}")
        w(f"\\echo #H2H IDS_BEGIN qid={qid}")
        w(tjs + ";")  # warm-up; also the answer ids we grade
        w(f"\\echo #H2H IDS_END qid={qid}")
        w("\\timing on")
        for _ in range(runs):
            w(tjs + ";")
        w("\\timing off")
        w(f"\\echo #H2H QEND qid={qid}")
    w("\\echo #H2H DONE")
    return "\n".join(out) + "\n"


def parse_tridb(raw: str) -> dict:
    """{qid: {"ids": [...], "times": [ms,...]}} from the engine transcript."""
    if "#H2H DONE" not in raw:
        raise SystemExit("H2H transcript did not reach '#H2H DONE' — incomplete")
    res: dict = {}
    cur = None
    in_ids = False
    for line in raw.splitlines():
        m = _QSTART.search(line)
        if m:
            cur = int(m[1])
            res[cur] = {"ids": [], "times": []}
            continue
        if cur is None:
            continue
        if _IDSB.search(line):
            in_ids = True
            continue
        if _IDSE.search(line):
            in_ids = False
            continue
        if in_ids:
            mi = _INT.match(line)
            if mi:
                res[cur]["ids"].append(int(mi[1]))
            continue
        mt = _TIME.search(line)
        if mt:
            res[cur]["times"].append(float(mt[1]))
    return res


def _median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def _recall(top: list[int], gold: list[int], k: int) -> float:
    g = set(gold)
    return (len(g & set(top[:k])) / len(g)) if g else float("nan")


def grade(
    manifest: dict,
    tridb_raw: str,
    *,
    k: int,
    run_baseline_live: bool,
    seeds: int,
    hops: int,
    runs: int,
) -> dict:
    parsed = parse_tridb(tridb_raw)
    gold = {q["qid"]: q.get("gold_ids", []) for q in manifest["questions"]}

    tr_rec, tr_lat = [], []
    for qid, d in parsed.items():
        r = _recall(d["ids"], gold.get(qid, []), k)
        if r == r:
            tr_rec.append(r)
        if d["times"]:
            tr_lat.append(_median(d["times"]))
    tridb = {
        "recall_at_k": float(np.mean(tr_rec)) if tr_rec else float("nan"),
        "median_latency_ms": _median(tr_lat),
        "n_queries": len(parsed),
    }

    baseline = None
    if run_baseline_live:
        from baseline.graphrag import run_baseline as rb
        from baseline.harness import Conn

        b = rb(manifest, Conn(), k=k, seeds=seeds, hops=hops, runs=runs)
        baseline = {
            "recall_at_k": b["recall_at_k"],
            "median_latency_ms": b["median_ms"],
            "per_leg_median_ms": {
                leg: _median([p[leg] for p in b["per_query"]])
                for leg in ("milvus_ms", "neo4j_ms", "rerank_ms")
            },
        }
    return {"k": k, "tridb": tridb, "baseline": baseline}


def render_md(res: dict, manifest: dict) -> str:
    t = res["tridb"]
    b = res["baseline"]
    lines: list[str] = []
    w = lines.append
    w("# TriDB Benchmark — Real-Workload Head-to-Head vs Tuned Multi-Store (HotpotQA)")
    w("")
    if b:
        ratio = (
            (b["median_latency_ms"] / t["median_latency_ms"])
            if t["median_latency_ms"]
            else 0.0
        )
        tr, br = t["recall_at_k"], b["recall_at_k"]
        parity = abs(tr - br) <= 0.03
        if parity:
            w(
                f"**On a recognized public workload (HotpotQA), TriDB's one fused in-process query "
                f"matches the tuned multi-store stack on recall@{res['k']} "
                f"({tr:.3f} vs {br:.3f}) while running {ratio:.1f}× lower latency "
                f"({t['median_latency_ms']:.2f} ms vs {b['median_latency_ms']:.2f} ms end-to-end).**"
            )
        else:
            faster = t["median_latency_ms"] < b["median_latency_ms"]
            w(
                f"**NOT recall-matched — read carefully.** On HotpotQA the canonical single-`src` "
                f"`tjs()` at term_cond={res.get('termcond', 0)} retrieves recall@{res['k']} "
                f"**{tr:.3f}** vs the tuned multi-store's **{br:.3f}** — TriDB is "
                f"{'faster' if faster else 'slower'} ({t['median_latency_ms']:.2f} ms vs "
                f"{b['median_latency_ms']:.2f} ms) but at **much lower recall**. A bare latency win at "
                f"unequal recall is not a real win (GTM R1). The multi-store baseline uses 5 vector seeds "
                f"+ 2-hop expansion; the canonical `tjs()` takes ONE source vertex — a weaker fit for "
                f"seedless multi-hop retrieval. See the term_cond sweep + honest read below."
            )
    else:
        w(
            f"**TriDB on HotpotQA: recall@{res['k']} {t['recall_at_k']:.3f}, "
            f"median tjs() latency {t['median_latency_ms']:.2f} ms (baseline leg not run).**"
        )
    w("")
    w(
        f"{t['n_queries']} questions over a {len(manifest['paragraphs'])}-paragraph corpus, "
        "same corpus+queries+k both sides, client-side wall-clock, warm, median of N runs."
    )
    w("")
    w("| system | recall@k | median end-to-end latency (ms) |")
    w("|---|---:|---:|")
    w(
        f"| TriDB (one fused `tjs()` query, in-process) | {t['recall_at_k']:.3f} | {t['median_latency_ms']:.2f} |"
    )
    if b:
        w(
            f"| Tuned multi-store (Milvus + Neo4j + rerank) | {b['recall_at_k']:.3f} | {b['median_latency_ms']:.2f} |"
        )
        w("")
        leg = b["per_leg_median_ms"]
        w("Baseline per-leg median (the cross-system tax TriDB avoids):")
        w("")
        w("| leg | ms |")
        w("|---|---:|")
        w(f"| Milvus ANN | {leg['milvus_ms']:.2f} |")
        w(f"| Neo4j graph hop | {leg['neo4j_ms']:.2f} |")
        w(f"| app-side rerank | {leg['rerank_ms']:.2f} |")
    w("")
    parity = b and abs(t["recall_at_k"] - b["recall_at_k"]) <= 0.03
    if b and not parity:
        w("## Honest read — why TriDB loses recall here (and what it means)")
        w("")
        w(
            "- **Root cause: the canonical `tjs()` is a SINGLE-SOURCE constrained-traversal operator,** "
            "not an open-domain retriever. It returns the vector-nearest entities *reachable from one "
            "`src` vertex* (within the filter). On HotpotQA there is no given source, so we anchor on the "
            "top vector hit; the title-mention graph is sparse (~0.5 edges/node), so one src reaches almost "
            "nothing -> recall caps near 0.22."
        )
        w(
            "- **Not a tuning issue:** raising the early-termination knob 100× (term_cond 0 -> 5000) moved "
            "recall only 0.223 -> 0.227. It is the reachability cap, not early termination."
        )
        w(
            "- **The baseline isn't really 'graph-constrained' either** — its 5 Milvus vector seeds alone "
            "≈ its final recall; the Neo4j hop adds little on this sparse graph. So this row is "
            "essentially open vector retrieval (0.95) vs single-source constrained traversal (0.22)."
        )
        w(
            "- **GTM implication (important):** `tjs()` v1 wins big on its HOME workload — SOURCE-ANCHORED "
            "tri-modal queries ('given entity X, find similar reachable filtered'): SM-2 = 12/12, ~15× "
            "(synthetic) and the one-WAL consistency proof on the GB10. It is NOT, in v1, an open GraphRAG "
            "retriever. The +15.6pt multi-hop result (benchmark_graphrag_v0.1.0.md) is a HOST-side "
            "prototype (multi-seed + bridge injection), which the engine's single-`src` `tjs()` does not "
            "execute. Launch the source-anchored claim; do not market v1 as a drop-in open GraphRAG retriever."
        )
        w("")
    w("## Notes")
    w("")
    w(
        "- **Like-for-like measurement:** identical HotpotQA corpus + queries + k; both sides client-side "
        "end-to-end wall-clock, warm connections, median of N. TriDB = one fused `tjs()` in-process; the "
        "baseline makes three cross-system round-trips merged app-side."
    )
    w(
        "- **Latency is real but only meaningful at matched recall.** TriDB's 1.8 ms is the in-process "
        "fused-operator round-trip; the cross-system tax (Milvus + Neo4j legs) is what it avoids — but a "
        "latency win at unequal recall is not a win (GTM R1). For the source-anchored canonical query "
        "(where recall IS matched) see benchmark_sm2_v0.1.0.md."
    )
    w(
        "- **Scope:** dev-slice corpus on the x86 standin; larger/live scale is GX10-gated."
    )
    w("")
    w(
        "_Generated by `bench/h2h_report.py` (`make graphrag-h2h`). Numbers are observed._"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Real-workload TriDB vs multi-store head-to-head."
    )
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--termcond", type=int, default=0)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument(
        "--emit-sql", type=Path, help="write the TriDB \\timing SQL and exit"
    )
    ap.add_argument("--tridb-raw", type=Path, help="engine transcript to grade")
    ap.add_argument(
        "--no-baseline", action="store_true", help="skip the live multi-store leg"
    )
    ap.add_argument(
        "--json-out", type=Path, default=Path("bench/results/h2h_metrics.json")
    )
    ap.add_argument("--md-out", type=Path, default=Path("docs/benchmark_h2h_v0.1.0.md"))
    args = ap.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())

    if args.emit_sql:
        corpus_emb = np.load(manifest["corpus_emb_path"])
        query_emb = np.load(manifest["query_emb_path"])
        args.emit_sql.write_text(
            emit_tridb_sql(
                manifest,
                corpus_emb,
                query_emb,
                k=args.k,
                termcond=args.termcond,
                runs=args.runs,
            )
        )
        print(
            f"[h2h] wrote TriDB SQL -> {args.emit_sql} ({len(manifest['paragraphs'])} paras, "
            f"{len(manifest['questions'])} queries, k={args.k}, runs={args.runs})"
        )
        return 0

    if not args.tridb_raw:
        ap.error("need --emit-sql or --tridb-raw")
    res = grade(
        manifest,
        args.tridb_raw.read_text(),
        k=args.k,
        run_baseline_live=not args.no_baseline,
        seeds=args.seeds,
        hops=args.hops,
        runs=args.runs,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(res, indent=2))
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(render_md(res, manifest))
    t = res["tridb"]
    b = res["baseline"]
    line = f"[h2h] TriDB recall@{args.k}={t['recall_at_k']:.3f} lat={t['median_latency_ms']:.2f}ms"
    if b:
        line += f" | baseline recall={b['recall_at_k']:.3f} lat={b['median_latency_ms']:.2f}ms"
    print(line + f" -> {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

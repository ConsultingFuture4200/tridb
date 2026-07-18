"""HotpotQA membership-vs-PPR recall gate on the STOCK engine (plan 095 spike).

The spike's actual PRODUCT: loads the SAME local HotpotQA corpus the host reference
(bench/tjs_open_ref.py) uses -- data/hotpot/{manifest.json,corpus_emb.npy,query_emb.npy}
(1490 paragraphs, 745 title-mention edges, 150 graded questions, BGE-768) -- into a STOCK
PG (graph_store_am + tjs_pg, no fork) image, and runs all 150 questions through the SEEDLESS
`tjs_open` in BOTH `tjs.graph_scoring` modes (membership, ppr) at k in {5,10}, term_cond in
{8,32,128}, fixed tjs.graph_work_budget. Grades with bench.graphrag_report.evidence_scores
(recall / joint) against `gold_ids`. Honesty bars (plan 095): identical inputs both modes;
censored fraction reported next to every point; never tune membership down.

Two-phase, no live TCP connection needed (mirrors scripts/tjs_parity_test.sh's pattern):
  1. `--gen-sql OUT.sql`   writes one big deterministic SQL script (data load + the full
     mode x k x term_cond x question sweep, one '#R ...' tagged output line per query).
  2. `--parse LOG --out results.json --md OUT.md`
     parses the captured psql stdout, grades every point, writes the JSON + a markdown table
     for the ADR-0012 addendum.

Orchestration (build the stock image, run psql -f, capture stdout) is
scripts/hotpot_stock_gate.sh, mirroring scripts/pg17_graph_test.sh's docker pattern.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.graphrag_report import evidence_scores  # noqa: E402

M_SEEDS = 5
HOPS = (
    4  # host reference PPR is depth-unbounded (pure r_max); the engine bounds by hops
)
# (a required tjs_open argument for BOTH scoring modes) -- 4 is generous on this sparse
# graph (745 edges / 1490 nodes, mean degree ~1) so the hop bound is not the limiting
# factor; disclosed as a host-vs-engine deviation in the ADR-0012 addendum.
KS = (5, 10)
TERM_CONDS = (8, 32, 128)
MODES = ("membership", "ppr")


def _vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in v.tolist()) + "]"


def gen_sql(manifest_path: Path, out_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    corpus_emb = np.load(
        Path(manifest_path).parent / Path(manifest["corpus_emb_path"]).name
    )
    query_emb = np.load(
        Path(manifest_path).parent / Path(manifest["query_emb_path"]).name
    )
    n = manifest["entities"]
    dim = manifest["dim"]
    edges = manifest["_edges"]
    questions = manifest["questions"]
    assert corpus_emb.shape == (n, dim)
    assert query_emb.shape == (len(questions), dim)

    lines: list[str] = []
    w = lines.append

    w("CREATE EXTENSION IF NOT EXISTS vector;")
    w("CREATE EXTENSION IF NOT EXISTS graph_store_am;")
    w("CREATE EXTENSION IF NOT EXISTS tjs_pg;")
    w(f"CREATE TABLE paragraphs (id bigint PRIMARY KEY, embedding vector({dim}));")
    w(f"CREATE TABLE queries (qid int PRIMARY KEY, embedding vector({dim}));")

    # Batched multi-row INSERTs (paragraphs: 1490 rows, ~500/statement to keep line length sane).
    def batched_insert(table: str, rows: list[str], batch: int = 500) -> None:
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            w(f"INSERT INTO {table} VALUES\n  " + ",\n  ".join(chunk) + ";")

    para_rows = [f"({i}, '{_vec_literal(corpus_emb[i])}')" for i in range(n)]
    batched_insert("paragraphs", para_rows)
    q_rows = [f"({q['qid']}, '{_vec_literal(query_emb[q['qid']])}')" for q in questions]
    batched_insert("queries", q_rows)

    w(
        "CREATE INDEX paragraphs_hnsw ON paragraphs USING hnsw (embedding vector_l2_ops) "
        "WITH (m = 16, ef_construction = 64);"
    )

    # Dense vids 0..n-1 (ext id == vid by upsert order, the established convention).
    w("DO $$")
    w("DECLARE g int; v bigint;")
    w("BEGIN")
    w(f"  FOR g IN 0..{n - 1} LOOP")
    w("    v := graph_store.gph_upsert_vertex(g);")
    w("    IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;")
    w("  END LOOP;")
    w("END $$;")
    w(
        "SELECT set_config('tjs.htype', graph_store.register_edge_type('HOTPOT')::text, false);"
    )

    # Undirected: insert BOTH directions (matches the host reference's build_adjacency,
    # which unions both endpoints' neighbor lists -- the title-mention graph is a symmetric
    # co-occurrence proxy). Same edge count both scoring modes see (honesty bar: identical
    # inputs both modes).
    edge_rows = []
    for s, d in edges:
        edge_rows.append(f"({s}, {d}, current_setting('tjs.htype')::int)")
        edge_rows.append(f"({d}, {s}, current_setting('tjs.htype')::int)")
    for i in range(0, len(edge_rows), 500):
        chunk = edge_rows[i : i + 500]
        w(
            "SELECT count(*) FROM (SELECT graph_store.gph_insert_edge(s, d, t) "
            "FROM (VALUES " + ", ".join(chunk) + ") AS v(s,d,t)) e;"
        )

    w("SET hnsw.iterative_scan = relaxed_order;")
    w(
        "SET hnsw.max_scan_tuples = 1000000;"
    )  # never budget-cap the pgvector stream itself
    # tjs.graph_work_budget stays at its ADR-0020 default (65536) -- fixed budget both modes,
    # per the plan's honesty bar.

    for mode in MODES:
        w(f"SET tjs.graph_scoring = '{mode}';")
        for k in KS:
            for tc in TERM_CONDS:
                for q in questions:
                    qid = q["qid"]
                    w(
                        f"SELECT '#R mode={mode} k={k} tc={tc} qid={qid} ids=' || "
                        "coalesce(array_to_string(array_agg(t), ','), '') "
                        f"FROM tjs_open('paragraphs', {k}, {tc}, {M_SEEDS}, {HOPS}, 'id', '', "
                        f"(SELECT embedding FROM queries WHERE qid = {qid})) AS t;"
                    )
                    w(
                        f"SELECT '#C mode={mode} k={k} tc={tc} qid={qid} "
                        "examined=' || tjs_open_graph_examined()::text || "
                        "' censored=' || tjs_open_graph_censored()::text;"
                    )

    out_path.write_text("\n".join(lines) + "\n")
    print(
        f"[hotpot_stock_gate] wrote {out_path} ({len(lines)} statements)",
        file=sys.stderr,
    )


_R_RE = re.compile(
    r"^#R mode=(?P<mode>\w+) k=(?P<k>\d+) tc=(?P<tc>\d+) qid=(?P<qid>\d+) ids=(?P<ids>.*)$"
)
_C_RE = re.compile(
    r"^#C mode=(?P<mode>\w+) k=(?P<k>\d+) tc=(?P<tc>\d+) qid=(?P<qid>\d+) "
    r"examined=(?P<examined>\d+) censored=(?P<censored>true|false)$"
)


def parse_log(log_path: Path) -> dict[tuple[str, int, int, int], dict]:
    """One entry per (mode, k, term_cond, qid): {ids, examined, censored}."""
    points: dict[tuple[str, int, int, int], dict] = {}
    for raw in log_path.read_text(errors="replace").splitlines():
        line = raw.strip()
        m = _R_RE.match(line)
        if m:
            key = (m["mode"], int(m["k"]), int(m["tc"]), int(m["qid"]))
            ids = [int(x) for x in m["ids"].split(",") if x]
            points.setdefault(key, {})["ids"] = ids
            continue
        m = _C_RE.match(line)
        if m:
            key = (m["mode"], int(m["k"]), int(m["tc"]), int(m["qid"]))
            points.setdefault(key, {})["examined"] = int(m["examined"])
            points.setdefault(key, {})["censored"] = m["censored"] == "true"
    return points


def grade(manifest_path: Path, points: dict[tuple[str, int, int, int], dict]) -> dict:
    manifest = json.loads(manifest_path.read_text())
    gold_by_qid = {q["qid"]: q["gold_ids"] for q in manifest["questions"]}
    n_q = len(gold_by_qid)

    rows = []
    for mode in MODES:
        for k in KS:
            for tc in TERM_CONDS:
                recalls, joints, examined, censored_n = [], [], [], 0
                missing = 0
                for qid, gold in gold_by_qid.items():
                    key = (mode, k, tc, qid)
                    p = points.get(key)
                    if p is None or "ids" not in p or "examined" not in p:
                        missing += 1
                        continue
                    sc = evidence_scores(p["ids"], gold)
                    recalls.append(sc["recall"])
                    joints.append(sc["joint"])
                    examined.append(p["examined"])
                    if p["censored"]:
                        censored_n += 1
                if missing:
                    print(
                        f"[hotpot_stock_gate] WARNING: {missing}/{n_q} missing points for "
                        f"mode={mode} k={k} tc={tc} (dropped, not padded)",
                        file=sys.stderr,
                    )
                n = len(recalls)
                rows.append(
                    {
                        "mode": mode,
                        "k": k,
                        "term_cond": tc,
                        "n": n,
                        "recall": sum(recalls) / n if n else float("nan"),
                        "joint": sum(joints) / n if n else float("nan"),
                        "graph_examined_mean": sum(examined) / n if n else float("nan"),
                        "censored_fraction": censored_n / n if n else float("nan"),
                    }
                )
    return {"m_seeds": M_SEEDS, "hops": HOPS, "rows": rows}


def render_md(res: dict) -> str:
    w = []
    w.append(
        f"m_seeds={res['m_seeds']}, hops={res['hops']} (both scoring modes, fixed "
        "tjs.graph_work_budget default 65536)."
    )
    w.append("")
    w.append(
        "| mode | k | term_cond | n | recall | joint | graph_examined (mean) | censored fraction |"
    )
    w.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in res["rows"]:
        w.append(
            f"| {r['mode']} | {r['k']} | {r['term_cond']} | {r['n']} | "
            f"{r['recall']:.3f} | {r['joint']:.3f} | {r['graph_examined_mean']:.1f} | "
            f"{r['censored_fraction']:.3f} |"
        )
    return "\n".join(w) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path("data/hotpot/manifest.json"))
    ap.add_argument("--gen-sql", type=Path, help="write the sweep SQL script here")
    ap.add_argument(
        "--parse", type=Path, help="captured psql stdout log to parse+grade"
    )
    ap.add_argument(
        "--out", type=Path, default=Path("bench/results/hotpot_stock_gate.json")
    )
    ap.add_argument(
        "--md", type=Path, default=Path("bench/results/hotpot_stock_gate.md")
    )
    args = ap.parse_args(argv)

    if args.gen_sql:
        gen_sql(args.manifest, args.gen_sql)
        return 0

    if args.parse:
        points = parse_log(args.parse)
        res = grade(args.manifest, points)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(res, indent=2))
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_md(res))
        print(
            f"[hotpot_stock_gate] {len(points)} points parsed -> {args.out}, {args.md}"
        )
        return 0

    ap.error("pass --gen-sql or --parse")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

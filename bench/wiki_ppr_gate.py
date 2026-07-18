"""Wiki-scale membership-vs-PPR gate: held-out link prediction at 200k (advisor plan 096).

The default-adoption gate ADR-0012's 2026-07-17 addendum named before any "make
`tjs.graph_scoring=ppr` the default" ADR can be proposed. Plan 095's HotpotQA gate found
`term_cond` and `tjs.graph_work_budget` byte-inert (1490 nodes, mean degree ~= 1 -- never
under real pressure). This gate runs the SAME membership-vs-ppr comparison on the real
200k-article enwiki hyperlink slice (mean degree two orders of magnitude denser), where
those knobs have a chance to actually bite.

THE DESIGN TRAP THIS AVOIDS (read before touching the grading logic): `bench/wiki_h2h.py`'s
oracle is MEMBERSHIP-SHAPED (exact ANN seeds union hops-reachable set, cosine-reranked) --
grading PPR against it would structurally penalize exactly the deviation PPR exists to make.
The gold used here instead is scoring-agnostic: HELD-OUT LINK PREDICTION. For each query
article we remove a handful of its real hyperlink targets from the loaded graph and ask
whether retrieval recovers them. Neither mode can reach a held-out target via the removed
edge; both see the identical remaining graph; the gold comes from Wikipedia's editors, not
from either scoring definition. Residual tilt (disclosed in the ADR addendum, not hidden):
link prediction inherently rewards graph-structure exploitation (the capability under test),
and gold targets are by construction semantically related to the query article.

Loader precedents mirrored here: `bench/hotpot_stock_gate.py` (095's stock-dialect
gen-sql/parse/grade split) and `tools/wikidata_engine_load.py` (COPY-staged load + the typed
BATCHED `graph_store.gph_insert_edges(src, dsts[], type_id)` from plan 091, grouped by
(src, type) for O(1)-per-edge inserts instead of the per-edge scalar path).

Two-phase, no live TCP connection needed (same shape as hotpot_stock_gate):
  1. `--gen-sql OUT.sql --meta-out META.json`  writes ONE deterministic SQL script (load +
     the whole mode x k x term_cond x budget x query sweep, `#WPG`-tagged output lines) plus
     a sidecar JSON with the query/gold samples and the mode-independent reachability
     diagnostic (both pure, computed host-side, no docker needed for this half).
  2. `--parse LOG --meta META.json --out results.json --md OUT.md`  parses the captured
     psql stdout, grades every point against META's gold, writes JSON + a markdown table.

Orchestration (build the stock image, run psql -f, capture stdout) is
scripts/wiki_ppr_gate.sh, mirroring scripts/hotpot_stock_gate.sh's docker pattern.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.graphrag_report import evidence_scores  # noqa: E402

# --------------------------------------------------------------------------------------
# Fixed experiment shape (plan 096 "The experiment").
# --------------------------------------------------------------------------------------
DEFAULT_N = 200_000
DEFAULT_DIM = 384
DEFAULT_Q = 300
DEFAULT_SEED = 42
DEFAULT_HOLD_OUT = 5
DEFAULT_MIN_OUTDEGREE = 8
M_SEEDS = 8
HOPS = 2
KS = (10, 20)
TERM_CONDS = (8, 32, 128)
BUDGETS = (2048, 8192, 65536)
MODES = ("membership", "ppr")
EDGE_TYPE_NAME = "related_to"
TABLE = "articles"


# ========================================================================================
# PURE: slice adjacency (host-side, no docker) -- mirrors bench/wiki_h2h.py's shard-index
# math (edge shards are src-partitioned + index-aligned to article shards) but DEDUPS to
# distinct out-links (plan 096 "distinct out-links" requirement) and drops self-loops.
# ========================================================================================
def edge_shard_paths(manifest: dict, manifest_dir: Path, n: int) -> list[Path]:
    shard_size = (
        manifest.get("shard_size") or manifest["shards"]["articles"]["files"][0]["rows"]
    )
    max_shard = (n - 1) // shard_size
    files = manifest["shards"].get("edges", {}).get("files")
    if files:
        paths = list(dict.fromkeys(s["path"] for s in files))
    else:
        n_article_shards = len(
            list(
                dict.fromkeys(
                    s["path"] for s in manifest["shards"]["articles"]["files"]
                )
            )
        )
        paths = [f"edges-{i:05d}.tsv" for i in range(n_article_shards)]
    return [manifest_dir / p for p in paths[: max_shard + 1]]


def parse_edge_lines(paths: list[Path], n: int) -> dict[int, list[int]]:
    """Directed within-slice (both endpoints < n) adjacency, deduped per src, self-loops
    dropped, sorted for determinism."""
    adj: dict[int, set[int]] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open() as fh:
            for line in fh:
                tab = line.find("\t")
                if tab < 0:
                    continue
                s = int(line[:tab])
                if s >= n:
                    continue
                d = int(line[tab + 1 :])
                if d >= n or d == s:
                    continue
                adj.setdefault(s, set()).add(d)
    return {s: sorted(ds) for s, ds in adj.items()}


def load_slice_adjacency(manifest_dir: Path, n: int) -> dict[int, list[int]]:
    manifest = json.loads((manifest_dir / "manifest.json").read_text())
    return parse_edge_lines(edge_shard_paths(manifest, manifest_dir, n), n)


# ========================================================================================
# PURE: deterministic query + held-out-gold sampling.
# ========================================================================================
def sample_queries_and_holdouts(
    adj: dict[int, list[int]],
    q: int,
    seed: int,
    hold_out: int,
    min_outdegree: int,
) -> list[dict]:
    """Q articles with >= min_outdegree distinct within-slice out-links (seed-42
    deterministic); hold_out targets held out per query (same RNG stream, so re-running
    is byte-identical). Candidates and each query's targets are drawn from SORTED
    sequences so the only source of randomness is the seeded RNG, never dict/set order."""
    import random

    candidates = sorted(s for s, ds in adj.items() if len(ds) >= min_outdegree)
    if len(candidates) < q:
        raise SystemExit(
            f"only {len(candidates)} candidates with >= {min_outdegree} distinct "
            f"out-links; need {q}"
        )
    rng = random.Random(seed)
    chosen = sorted(rng.sample(candidates, q))
    samples = []
    for qid in chosen:
        gold = sorted(rng.sample(adj[qid], hold_out))
        samples.append({"qid": qid, "gold": gold})
    return samples


# ========================================================================================
# PURE: the LOADED graph (symmetrized, held-out edges excluded in BOTH directions).
# ========================================================================================
def build_load_edges(adj: dict[int, list[int]]) -> set[tuple[int, int]]:
    """Undirected load graph: every directed hyperlink s->d contributes BOTH (s,d) and
    (d,s) (matches hotpot_stock_gate's / the reader's undirected adjacency convention)."""
    edges: set[tuple[int, int]] = set()
    for s, ds in adj.items():
        for d in ds:
            edges.add((s, d))
            edges.add((d, s))
    return edges


def exclude_holdouts(
    edges: set[tuple[int, int]], samples: list[dict]
) -> set[tuple[int, int]]:
    """Remove every held-out (qid, gold) pair in BOTH directions from the load graph.
    Applied AFTER symmetrization: a genuine reverse hyperlink (gold -> qid) would
    otherwise resurrect the held-out edge as its own symmetrized twin -- excluding both
    directions post-symmetrization is the only way to guarantee neither mode can reach a
    held-out target via the removed edge."""
    remove: set[tuple[int, int]] = set()
    for s in samples:
        qid = s["qid"]
        for g in s["gold"]:
            remove.add((qid, g))
            remove.add((g, qid))
    return edges - remove


def adjacency_from_edges(edges: set[tuple[int, int]]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for s, d in edges:
        out.setdefault(s, []).append(d)
    return {s: sorted(ds) for s, ds in out.items()}


def reachable_within_hops(adj: dict[int, list[int]], src: int, hops: int) -> set[int]:
    frontier = {src}
    seen = {src}
    for _ in range(hops):
        nxt: set[int] = set()
        for s in frontier:
            nxt.update(adj.get(s, ()))
        nxt -= seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    seen.discard(src)
    return seen


def mean_gold_reachable_fraction(
    load_adj: dict[int, list[int]], samples: list[dict], hops: int
) -> float:
    """Mode-independent diagnostic: mean, over queries, of the fraction of a query's gold
    ids reachable within `hops` in the LOADED (post-exclusion) graph. Same for both scoring
    modes -- context for how much headroom graph scoring even has, not a per-mode metric."""
    if not samples:
        return float("nan")
    fracs = []
    for s in samples:
        reach = reachable_within_hops(load_adj, s["qid"], hops)
        gold = set(s["gold"])
        fracs.append(len(gold & reach) / len(gold) if gold else 0.0)
    return sum(fracs) / len(fracs)


# ========================================================================================
# PURE: grading. Query id is dropped from the retrieved set before scoring (plan 096: "the
# query article itself is excluded from scoring").
# ========================================================================================
def grade_point(retrieved: list[int], qid: int, gold: list[int]) -> dict:
    filtered = [x for x in retrieved if x != qid]
    return evidence_scores(filtered, gold)


# ========================================================================================
# SQL generation (COPY-staged; generator, streamed to file -- 200k vectors + ~15M edge
# pairs do not comfortably fit as one in-memory joined string).
# ========================================================================================
def _vec_literal(v) -> str:
    return "[" + ",".join(f"{float(x):.8g}" for x in v) + "]"


def iter_load_sql(emb: np.ndarray, load_edges: set[tuple[int, int]], n: int, dim: int):
    yield "\\set ON_ERROR_STOP on\n\\pset pager off\n"
    yield "CREATE EXTENSION IF NOT EXISTS vector;\n"
    yield "CREATE EXTENSION IF NOT EXISTS graph_store_am;\n"
    yield "CREATE EXTENSION IF NOT EXISTS tjs_pg;\n"
    yield f"CREATE TABLE {TABLE} (id bigint PRIMARY KEY, embedding vector({dim}));\n"
    yield "\\echo #WPG COPY_START\n"
    yield f"COPY {TABLE} (id, embedding) FROM stdin;\n"
    for i in range(n):
        yield f"{i}\t{_vec_literal(emb[i])}\n"
    yield "\\.\n"
    yield "\\echo #WPG COPY_DONE\n"

    # Dense vids 0..n-1: ext id == vid by upsert-in-order (the established shared-id
    # invariant, ADR-0013/ADR-0018 (c)) -- fails LOUDLY on any drift, never silently.
    yield "\\echo #WPG VERTEX_UPSERT_START\n"
    yield "DO $$\nDECLARE g bigint; v bigint;\nBEGIN\n"
    yield f"  FOR g IN 0..{n - 1} LOOP\n"
    yield "    v := graph_store.gph_upsert_vertex(g);\n"
    yield "    IF v <> g THEN RAISE EXCEPTION 'dense vid drift: % != %', v, g; END IF;\n"
    yield "  END LOOP;\n"
    yield "END $$;\n"
    yield "\\echo #WPG VERTEX_UPSERT_DONE\n"

    yield (
        f"SELECT set_config('tjs.htype', "
        f"graph_store.register_edge_type('{EDGE_TYPE_NAME}')::text, false);\n"
    )
    yield "CREATE TEMP TABLE edge_stage (src bigint, dst bigint);\n"
    yield "\\echo #WPG COPY_EDGES_START\n"
    yield "COPY edge_stage (src, dst) FROM stdin;\n"
    n_edges = len(load_edges)
    for s, d in sorted(load_edges):
        yield f"{s}\t{d}\n"
    yield "\\.\n"
    yield "\\echo #WPG COPY_EDGES_DONE\n"

    yield "\\echo #WPG EDGE_INSERT_START\n"
    yield "DO $$\nDECLARE n bigint;\nBEGIN\n"
    yield (
        "  SELECT coalesce(sum(graph_store.gph_insert_edges(g.src, g.dsts, "
        "current_setting('tjs.htype')::int)), 0) INTO n\n"
        "  FROM (\n"
        "    SELECT src, array_agg(dst ORDER BY dst) AS dsts\n"
        "    FROM edge_stage GROUP BY src ORDER BY src\n"
        "  ) g;\n"
    )
    yield f"  IF n <> {n_edges} THEN\n"
    yield f"    RAISE EXCEPTION 'edge insert count % != staged {n_edges}', n;\n"
    yield "  END IF;\nEND $$;\n"
    yield "DROP TABLE edge_stage;\n"
    yield "\\echo #WPG EDGE_INSERT_DONE\n"
    yield "DO $$\nDECLARE ec bigint; vc bigint;\nBEGIN\n"
    yield "  SELECT graph_store.gph_edge_count() INTO ec;\n"
    yield "  SELECT graph_store.gph_vertex_count() INTO vc;\n"
    yield f"  IF ec <> {n_edges} THEN RAISE EXCEPTION 'gph_edge_count % != expected {n_edges}', ec; END IF;\n"
    yield f"  IF vc <> {n} THEN RAISE EXCEPTION 'gph_vertex_count % != expected {n}', vc; END IF;\n"
    yield "END $$;\n"

    # Identity mode: safe ONLY because the loop above verified ext id == vid for every
    # row (tools/wikidata_engine_load.py's documented precedent) -- flips the tjs_open
    # read path to skip the id-map lookup.
    yield "SELECT graph_store.gph_set_identity_mode(true);\n"

    yield (
        f"CREATE INDEX {TABLE}_hnsw ON {TABLE} USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64);\n"
    )
    yield "SET hnsw.iterative_scan = relaxed_order;\n"
    yield "SET hnsw.max_scan_tuples = 1000000;\n"
    yield "\\echo #WPG LOAD_DONE\n"


def iter_probe_sql(qid: int, gold0: int):
    """Step-2 requirement: prove a known held-out edge is absent from the loaded graph,
    in BOTH directions, via the native gph_neighbors read surface."""
    yield (
        f"SELECT '#WPG PROBE fwd_absent=' || "
        f"(NOT EXISTS (SELECT 1 FROM graph_store.gph_neighbors({qid}) t WHERE t = {gold0}))::text || "
        f"' rev_absent=' || "
        f"(NOT EXISTS (SELECT 1 FROM graph_store.gph_neighbors({gold0}) t WHERE t = {qid}))::text;\n"
    )


def iter_sweep_sql(samples: list[dict]):
    for mode in MODES:
        yield f"SET tjs.graph_scoring = '{mode}';\n"
        for budget in BUDGETS:
            yield f"SET tjs.graph_work_budget = {budget};\n"
            for k in KS:
                for tc in TERM_CONDS:
                    for s in samples:
                        qid = s["qid"]
                        yield "\\timing on\n"
                        yield (
                            f"SELECT '#R mode={mode} k={k} tc={tc} bud={budget} qid={qid} ids=' || "
                            "coalesce(array_to_string(array_agg(t), ','), '') "
                            f"FROM tjs_open('{TABLE}', {k}, {tc}, {M_SEEDS}, {HOPS}, 'id', '', "
                            f"(SELECT embedding FROM {TABLE} WHERE id = {qid})) AS t;\n"
                        )
                        yield "\\timing off\n"
                        yield (
                            f"SELECT '#C mode={mode} k={k} tc={tc} bud={budget} qid={qid} "
                            "examined=' || tjs_open_graph_examined()::text || "
                            "' censored=' || tjs_open_graph_censored()::text || "
                            "' term=' || tjs_open_termination_reason();\n"
                        )
    yield "\\echo #WPG SWEEP_DONE\n"


# ========================================================================================
# gen-sql driver
# ========================================================================================
def gen_sql(
    manifest_dir: Path,
    emb_path: Path,
    out_path: Path,
    meta_out: Path,
    *,
    n: int,
    q: int,
    seed: int,
    hold_out: int,
    min_outdegree: int,
    dim: int,
) -> None:
    t0 = time.time()
    adj = load_slice_adjacency(manifest_dir, n)
    print(
        f"[wiki_ppr_gate] adjacency loaded: {len(adj)} srcs ({time.time() - t0:.1f}s)",
        file=sys.stderr,
    )

    samples = sample_queries_and_holdouts(adj, q, seed, hold_out, min_outdegree)
    load_edges = exclude_holdouts(build_load_edges(adj), samples)
    load_adj = adjacency_from_edges(load_edges)
    diag = mean_gold_reachable_fraction(load_adj, samples, HOPS)
    print(
        f"[wiki_ppr_gate] {len(samples)} queries, {len(load_edges)} loaded directed pairs, "
        f"gold-reachable-within-hops={diag:.3f}",
        file=sys.stderr,
    )

    mm = np.load(emb_path, mmap_mode="r")
    if mm.shape[0] < n:
        raise SystemExit(f"embeddings have {mm.shape[0]} rows < n={n}")
    if mm.shape[1] != dim:
        raise SystemExit(f"embeddings dim {mm.shape[1]} != --dim {dim}")
    norms = np.linalg.norm(np.asarray(mm[: min(5000, n)]), axis=1)
    if not np.allclose(norms, 1.0, atol=1e-2):
        raise SystemExit(
            f"embeddings not unit-normalized (norm range [{norms.min():.4f}, "
            f"{norms.max():.4f}]) -- cosine opclass would be wrong; STOP, do not guess"
        )
    emb = np.ascontiguousarray(mm[:n])

    probe = samples[0]
    with out_path.open("w") as fh:
        for chunk in iter_load_sql(emb, load_edges, n, dim):
            fh.write(chunk)
        for chunk in iter_probe_sql(probe["qid"], probe["gold"][0]):
            fh.write(chunk)
        for chunk in iter_sweep_sql(samples):
            fh.write(chunk)
    print(
        f"[wiki_ppr_gate] wrote {out_path} ({time.time() - t0:.1f}s total)",
        file=sys.stderr,
    )

    meta = {
        "n": n,
        "dim": dim,
        "q": q,
        "seed": seed,
        "hold_out": hold_out,
        "min_outdegree": min_outdegree,
        "m_seeds": M_SEEDS,
        "hops": HOPS,
        "ks": list(KS),
        "term_conds": list(TERM_CONDS),
        "budgets": list(BUDGETS),
        "modes": list(MODES),
        "samples": samples,
        "probe_qid": probe["qid"],
        "probe_gold0": probe["gold"][0],
        "gold_reachable_within_hops_mean": diag,
        "n_loaded_edges": len(load_edges),
    }
    meta_out.write_text(json.dumps(meta, indent=2))
    print(f"[wiki_ppr_gate] wrote {meta_out}", file=sys.stderr)


# ========================================================================================
# parse + grade
# ========================================================================================
_R_RE = re.compile(
    r"^#R mode=(?P<mode>\w+) k=(?P<k>\d+) tc=(?P<tc>\d+) bud=(?P<bud>\d+) "
    r"qid=(?P<qid>\d+) ids=(?P<ids>.*)$"
)
_C_RE = re.compile(
    r"^#C mode=(?P<mode>\w+) k=(?P<k>\d+) tc=(?P<tc>\d+) bud=(?P<bud>\d+) "
    r"qid=(?P<qid>\d+) examined=(?P<examined>\d+) censored=(?P<censored>true|false) "
    r"term=(?P<term>\S+)$"
)
_TIME_RE = re.compile(r"^Time:\s+([\d.]+)\s+ms")
_PROBE_RE = re.compile(r"^#WPG PROBE fwd_absent=(\w+) rev_absent=(\w+)$")


def parse_log(
    log_path: Path,
) -> tuple[dict[tuple[str, int, int, int, int], dict], dict | None]:
    """One entry per (mode, k, tc, budget, qid): {ids, examined, censored, term, latency_ms}."""
    points: dict[tuple[str, int, int, int, int], dict] = {}
    probe: dict | None = None
    pending_key = None
    for raw in log_path.read_text(errors="replace").splitlines():
        line = raw.strip()
        m = _R_RE.match(line)
        if m:
            key = (m["mode"], int(m["k"]), int(m["tc"]), int(m["bud"]), int(m["qid"]))
            ids = [int(x) for x in m["ids"].split(",") if x]
            points.setdefault(key, {})["ids"] = ids
            pending_key = key
            continue
        m = _TIME_RE.match(line)
        if m and pending_key is not None:
            points[pending_key]["latency_ms"] = float(m.group(1))
            pending_key = None
            continue
        m = _C_RE.match(line)
        if m:
            key = (m["mode"], int(m["k"]), int(m["tc"]), int(m["bud"]), int(m["qid"]))
            points.setdefault(key, {})["examined"] = int(m["examined"])
            points.setdefault(key, {})["censored"] = m["censored"] == "true"
            points.setdefault(key, {})["term"] = m["term"]
            continue
        m = _PROBE_RE.match(line)
        if m:
            # boolean::text in Postgres renders the full word ('true'/'false'), not 't'/'f'.
            probe = {
                "fwd_absent": m.group(1) == "true",
                "rev_absent": m.group(2) == "true",
            }
    return points, probe


def grade(meta: dict, points: dict[tuple[str, int, int, int, int], dict]) -> dict:
    gold_by_qid = {s["qid"]: s["gold"] for s in meta["samples"]}
    n_q = len(gold_by_qid)

    rows = []
    for mode in meta["modes"]:
        for k in meta["ks"]:
            for tc in meta["term_conds"]:
                for bud in meta["budgets"]:
                    recalls, examined, censored_n, unknown_n, latencies = (
                        [],
                        [],
                        0,
                        0,
                        [],
                    )
                    missing = 0
                    for qid, gold in gold_by_qid.items():
                        key = (mode, k, tc, bud, qid)
                        p = points.get(key)
                        if p is None or "ids" not in p or "examined" not in p:
                            missing += 1
                            continue
                        sc = grade_point(p["ids"], qid, gold)
                        recalls.append(sc["recall"])
                        examined.append(p["examined"])
                        if p["censored"]:
                            censored_n += 1
                        if p.get("term") == "stream_end_unknown":
                            unknown_n += 1
                        if "latency_ms" in p:
                            latencies.append(p["latency_ms"])
                    if missing:
                        print(
                            f"[wiki_ppr_gate] WARNING: {missing}/{n_q} missing points for "
                            f"mode={mode} k={k} tc={tc} bud={bud} (dropped, not padded)",
                            file=sys.stderr,
                        )
                    n = len(recalls)
                    rows.append(
                        {
                            "mode": mode,
                            "k": k,
                            "term_cond": tc,
                            "budget": bud,
                            "n": n,
                            "recall": sum(recalls) / n if n else float("nan"),
                            "graph_examined_mean": sum(examined) / n
                            if n
                            else float("nan"),
                            "censored_fraction": censored_n / n if n else float("nan"),
                            "stream_end_unknown_fraction": unknown_n / n
                            if n
                            else float("nan"),
                            "latency_ms_mean": sum(latencies) / len(latencies)
                            if latencies
                            else float("nan"),
                        }
                    )
    return {
        "m_seeds": meta["m_seeds"],
        "hops": meta["hops"],
        "n": meta["n"],
        "q": meta["q"],
        "gold_reachable_within_hops_mean": meta["gold_reachable_within_hops_mean"],
        "n_loaded_edges": meta["n_loaded_edges"],
        "rows": rows,
    }


def render_md(res: dict, probe: dict | None) -> str:
    w = []
    w.append(
        f"n={res['n']}, q={res['q']}, m_seeds={res['m_seeds']}, hops={res['hops']} "
        "(both scoring modes, identical inputs)."
    )
    w.append(
        f"Diagnostic (mode-independent): mean fraction of gold reachable within "
        f"hops in the loaded graph = {res['gold_reachable_within_hops_mean']:.3f} "
        f"over {res['n_loaded_edges']} loaded directed pairs."
    )
    if probe is not None:
        w.append(
            f"Held-out-edge-absence probe: fwd_absent={probe['fwd_absent']}, "
            f"rev_absent={probe['rev_absent']}."
        )
    w.append("")
    w.append(
        "| mode | k | term_cond | budget | n | recall | graph_examined (mean) | "
        "censored fraction | stream_end_unknown fraction | latency ms (mean) |"
    )
    w.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in res["rows"]:
        w.append(
            f"| {r['mode']} | {r['k']} | {r['term_cond']} | {r['budget']} | {r['n']} | "
            f"{r['recall']:.3f} | {r['graph_examined_mean']:.1f} | "
            f"{r['censored_fraction']:.3f} | {r['stream_end_unknown_fraction']:.3f} | "
            f"{r['latency_ms_mean']:.2f} |"
        )
    return "\n".join(w) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest-dir", type=Path, default=Path("data/wiki/enwiki"))
    ap.add_argument(
        "--emb", type=Path, default=Path("data/wiki/enwiki/emb/dense_id_aligned.npy")
    )
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--q", type=int, default=DEFAULT_Q)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--hold-out", type=int, default=DEFAULT_HOLD_OUT)
    ap.add_argument("--min-outdegree", type=int, default=DEFAULT_MIN_OUTDEGREE)
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM)
    ap.add_argument("--gen-sql", type=Path, help="write the load+sweep SQL script here")
    ap.add_argument(
        "--meta-out", type=Path, help="write the sidecar sample/gold JSON here"
    )
    ap.add_argument(
        "--parse", type=Path, help="captured psql stdout log to parse+grade"
    )
    ap.add_argument("--meta", type=Path, help="sidecar JSON written by --gen-sql")
    ap.add_argument(
        "--out", type=Path, default=Path("bench/results/wiki_ppr_gate.json")
    )
    ap.add_argument("--md", type=Path, default=Path("bench/results/wiki_ppr_gate.md"))
    args = ap.parse_args(argv)

    if args.gen_sql:
        if not args.meta_out:
            ap.error("--gen-sql requires --meta-out")
        gen_sql(
            args.manifest_dir,
            args.emb,
            args.gen_sql,
            args.meta_out,
            n=args.n,
            q=args.q,
            seed=args.seed,
            hold_out=args.hold_out,
            min_outdegree=args.min_outdegree,
            dim=args.dim,
        )
        return 0

    if args.parse:
        if not args.meta:
            ap.error("--parse requires --meta")
        meta = json.loads(args.meta.read_text())
        points, probe = parse_log(args.parse)
        res = grade(meta, points)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({**res, "probe": probe}, indent=2))
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_md(res, probe))
        print(f"[wiki_ppr_gate] {len(points)} points parsed -> {args.out}, {args.md}")
        return 0

    ap.error("pass --gen-sql (with --meta-out) or --parse (with --meta)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

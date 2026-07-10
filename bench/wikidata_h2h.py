"""Harness B — Wikidata fused KBQA HEAD-TO-HEAD, filter-first (plan 060, ADR-0018).

The Wikidata analogue of bench/wiki_h2h.py. Same MATCHED-RECALL discipline, same honesty gate
(`publication_gate` reused verbatim), a different query shape: entity-centric KBQA on the
FILTER-FIRST physical path (the one already green at 1M, DEV-1290) — so this harness does NOT
depend on the blocked seedless/vector-first leg (plan 043).

QUERY (out-direction only — backlinks are gated on the ADR-0016 reverse index). Anchored on an
entity X, a property P, and an entity-type T:

    "entities e such that X --P--> e (within `hops`) AND e is of type T (P31 ∋ T),
     ranked by embedding similarity to X, top-k."

The graph + type constraint is the SELECTIVE leg (filter-first); the vector rank orders the small
surviving candidate set. This is exactly the shape tjs_open's filter-first mode serves and the
multi-store must assemble app-side (Postgres type filter + Neo4j typed traversal + Milvus rank).

ID SPACE. The engine assigns DENSE vids at load (gph_upsert_vertex, ADR-0013) in shard/emission
order, and the relational PK == that vid. So the oracle works in the DENSE id space: dense id ==
the entity's 0-based emission ordinal across the `entities-*.jsonl` shards, and the sparse Q-ids in
the edges/claims shards are remapped through that same map — the "measure the right store"
discipline (ADR-0013). The gate's engine-edges == oracle/Neo4j-edges blocker enforces that the
oracle's remapped graph equals the engine's actually-loaded adjacency before any headline.

THREE COMMANDS (mirror wiki_h2h): `oracle` (exact fused ground truth, runs anywhere on a slice's
assets), `tridb-emit` (the filter-first tjs_open sweep SQL — GX10), `baseline` (Milvus+Neo4j+pg —
GX10), `report` (grade both vs the oracle, gate the headline). Only `oracle` + the pure helpers run
on the x86 standin; the live legs are GX10/Spark-gated, same boundary as wiki_h2h.

HONESTY (inherited): COMPUTE-BOUND at 1M (RAM-resident dim-D floats); the I/O-locality thesis is
dead (wiki-scale memory). Value = fusion speed + one-WAL consistency (Harness A). Latency /
pages-touched are reported ONLY at matched recall; the seedless mode is labeled blocked-on-043.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Reuse the generic scoring + the honesty gate VERBATIM (plan 060: "Reuse publication_gate()
# unchanged"). operating_point / _vec_lit / publication_gate are query-shape-agnostic.
from bench.wiki_h2h import (
    _vec_lit,
    operating_point,
    publication_gate,
)


@dataclass
class WCfg:
    # slice assets (produced by tools/wikidata_ingest + the embedder)
    slice_dir: Path = Path(os.environ.get("WD_SLICE", "data/wikidata_slice"))
    emb_path: Path = Path(
        os.environ.get("WD_EMB", "data/wikidata_slice/emb/dense_id_aligned.npy")
    )
    dim: int = int(os.environ.get("WD_DIM", "384"))
    # engine (filter-first tjs_open)
    engine_container: str = os.environ.get("WD_ENGINE", "tridb-wikidata")
    engine_db: str = os.environ.get("WD_ENGINE_DB", "postgres")
    engine_table: str = os.environ.get("WD_ENGINE_TABLE", "entities")
    # baseline multi-store (isolated ports; mirror wiki_h2h layout)
    milvus_host: str = os.environ.get("WD_MILVUS_HOST", "localhost")
    milvus_port: str = os.environ.get("WD_MILVUS_PORT", "19531")
    milvus_collection: str = os.environ.get("WD_MILVUS_COLLECTION", "wikidata_entities")
    neo4j_uri: str = os.environ.get("WD_NEO4J_URI", "bolt://localhost:7688")
    pg_host: str = os.environ.get("WD_PGHOST", "localhost")
    pg_port: str = os.environ.get("WD_PGPORT", "5434")
    pg_db: str = os.environ.get("WD_PGDB", "tridb_wikidata")
    pg_table: str = os.environ.get("WD_PGTABLE", "wd_entity")
    # TR-1 work cap — MUST match the C default (mirror wiki_h2h WH_TJS_MAX_EXAMINED).
    tjs_max_examined: int = int(os.environ.get("WD_TJS_MAX_EXAMINED", "4000"))


# ======================================================================================
# Slice assets — dense id map, embeddings, typed adjacency, entity types (all dense space)
# ======================================================================================
def _shard_paths(manifest: dict, kind: str) -> list[str]:
    return list(dict.fromkeys(s["path"] for s in manifest["shards"][kind]["files"]))


def load_manifest(cfg: WCfg) -> dict:
    return json.loads((cfg.slice_dir / "manifest.json").read_text())


def load_dense_map(cfg: WCfg, manifest: dict) -> tuple[dict[int, int], list[int]]:
    """Build Q-id -> dense id and dense -> Q-id from the entities shards, in emission order.

    Dense id == the 0-based ordinal of the entity across the entities-*.jsonl shards, which is
    the order a loader upserts vids (gph_upsert_vertex), so dense id == engine vid == table PK.
    """
    qid_to_dense: dict[int, int] = {}
    dense_to_qid: list[int] = []
    for path in _shard_paths(manifest, "entities"):
        for line in (cfg.slice_dir / path).read_text().splitlines():
            if not line.strip():
                continue
            q = json.loads(line)["id"]
            if q not in qid_to_dense:
                qid_to_dense[q] = len(dense_to_qid)
                dense_to_qid.append(q)
    return qid_to_dense, dense_to_qid


def load_emb(cfg: WCfg, n: int) -> np.ndarray:
    """First-N id-aligned rows (row i == dense entity i), L2-normalized (cosine == dot)."""
    mm = np.load(cfg.emb_path, mmap_mode="r")
    if mm.shape[0] < n:
        raise SystemExit(
            f"embeddings have {mm.shape[0]} rows < N={n}; cannot ground-truth the slice"
        )
    emb = np.ascontiguousarray(mm[:n]).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-12
    return emb


def load_typed_adj(
    cfg: WCfg, manifest: dict, qmap: dict[int, int]
) -> dict[int, list[tuple[int, int]]]:
    """Dense typed out-adjacency: src_dense -> [(p_id, dst_dense)], induced on the slice.

    Edges shards store sparse Q-ids (src\\tp\\tdst); we remap src/dst through `qmap` and drop any
    edge whose endpoint is outside the slice (the ingest already dropped dangling dst, but the
    remap is defensive so the oracle graph == the engine's loaded adjacency).
    """
    adj: dict[int, list[tuple[int, int]]] = {}
    for path in _shard_paths(manifest, "edges"):
        spath = cfg.slice_dir / path
        if not spath.exists():
            continue
        with spath.open() as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                s = qmap.get(int(parts[0]))
                d = qmap.get(int(parts[2]))
                if s is None or d is None:
                    continue
                adj.setdefault(s, []).append((int(parts[1]), d))
    return adj


def load_types(cfg: WCfg, manifest: dict, qmap: dict[int, int]) -> dict[int, set[int]]:
    """Dense entity types: dense_id -> {dense type ids} from each claims row's P31 list."""
    types: dict[int, set[int]] = {}
    for path in _shard_paths(manifest, "claims"):
        spath = cfg.slice_dir / path
        if not spath.exists():
            continue
        for line in spath.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            e = qmap.get(row["id"])
            if e is None:
                continue
            ts = {qmap[t] for t in row.get("P31", []) if t in qmap}
            if ts:
                types[e] = ts
    return types


# ======================================================================================
# GROUND TRUTH — the exact fused FILTER-FIRST oracle. Both systems approximate it.
# ======================================================================================
def typed_reach(
    adj: dict[int, list[tuple[int, int]]], src: int, p_id: int | None, hops: int
) -> set[int]:
    """Dense ids reachable from `src` via `p_id`-typed OUT-edges within `hops` (excludes src).

    Out-direction only (ADR-0016). `p_id=None` means "any property" (untyped traversal); a
    concrete `p_id` filters — including dense id 0, which is a valid property, so the sentinel
    is None, never 0.
    """
    seen: set[int] = set()
    frontier: deque[tuple[int, int]] = deque([(src, 0)])
    visited = {src}
    while frontier:
        cur, depth = frontier.popleft()
        if depth >= hops:
            continue
        for pe, dst in adj.get(cur, ()):
            if p_id is not None and pe != p_id:
                continue
            if dst not in seen:
                seen.add(dst)
            if dst not in visited:
                visited.add(dst)
                frontier.append((dst, depth + 1))
    return seen


def candidates(
    adj: dict[int, list[tuple[int, int]]],
    types: dict[int, set[int]],
    x: int,
    p_id: int | None,
    t_id: int | None,
    hops: int,
) -> list[int]:
    """The filter-first candidate set: X's p-typed out-reach, filtered to entities of type T.

    `t_id=None` skips the type filter; a concrete `t_id` (including dense id 0) filters.
    """
    reach = typed_reach(adj, x, p_id, hops)
    if t_id is not None:
        return sorted(e for e in reach if t_id in types.get(e, ()))
    return sorted(reach)


def compute_oracle(
    emb: np.ndarray,
    adj: dict[int, list[tuple[int, int]]],
    types: dict[int, set[int]],
    queries: list[dict],
    *,
    k: int,
    hops: int,
) -> dict[int, list[int]]:
    """Per query {x,p,t}: exact filter-first candidate set, ranked by cosine to X -> top-k.

    Exact and blocking (the ground truth realization A) — the approximate tjs_open / multi-store
    both chase this. Ranking vector == the anchor X's embedding (description similarity to X).
    """
    oracle: dict[int, list[int]] = {}
    for qi, qy in enumerate(queries):
        cand = candidates(adj, types, qy["x"], qy["p"], qy["t"], hops)
        if not cand:
            oracle[qi] = []
            continue
        cand_arr = np.fromiter(cand, dtype=np.int64, count=len(cand))
        sims = emb[cand_arr] @ emb[qy["x"]]
        top = cand_arr[np.argsort(-sims)][:k].tolist()
        oracle[qi] = [int(x) for x in top]
    return oracle


def sample_queries(
    adj: dict[int, list[tuple[int, int]]],
    types: dict[int, set[int]],
    q: int,
    seed: int,
    *,
    hops: int,
    min_candidates: int,
) -> list[dict]:
    """Sample Q well-formed KBQA queries {x, p, t} with a non-trivial candidate set.

    For an anchor X with p-typed out-edges, pick the property P and a type T shared by >=
    `min_candidates` of X's p-reach, so the fused query is selective but non-empty. Deterministic
    given `seed`. Anchors are drawn from entities that actually have out-edges (KBQA subjects).
    """
    rng = np.random.default_rng(seed)
    subjects = np.array(sorted(adj.keys()), dtype=np.int64)
    if subjects.size == 0:
        return []
    out: list[dict] = []
    order = rng.permutation(subjects.size)
    for oi in order:
        x = int(subjects[oi])
        props = sorted({p for p, _ in adj[x]})
        chosen = None
        for p in props:
            reach = typed_reach(adj, x, p, hops)
            tcount: dict[int, int] = {}
            for e in reach:
                for t in types.get(e, ()):
                    tcount[t] = tcount.get(t, 0) + 1
            good = sorted(t for t, c in tcount.items() if c >= min_candidates)
            if good:
                chosen = {"x": x, "p": int(p), "t": int(good[0])}
                break
        if chosen is not None:
            out.append(chosen)
        if len(out) >= q:
            break
    return out


# ======================================================================================
# TriDB SIDE — emit the filter-first tjs_open sweep SQL (GX10-gated; mirrors wiki_h2h).
# ======================================================================================
def emit_tridb_sql(
    cfg: WCfg,
    emb: np.ndarray,
    queries: list[dict],
    grid: list[tuple[int, int, int]],
    *,
    k: int,
    runs: int,
) -> str:
    """Per (query, knob-combo): warm-up (graded ids + SM-3 counters) then `runs` timed repeats.

    FILTER-FIRST call: the entity-type + typed-edge constraint is the selective seed (the filter
    slot carries the P31 == T predicate and the P-typed traversal is rooted at X), the vector
    ranks the survivors. GX10-gated — emitted here, executed against the loaded engine on the
    Spark; any harvested timing is trustworthy ONLY if EXAMINED > 0 (the report gate enforces it).
    """
    out: list[str] = []
    w = out.append
    w("\\set ON_ERROR_STOP on")
    w("\\pset pager off")
    w("SET enable_seqscan = off;  -- gate on EXAMINED>0 (see wiki_h2h docstring)")
    w(
        f"SET vectordb.tjs_open_max_examined = {cfg.tjs_max_examined};"
        "  -- TR-1 work cap; examined>=cap => CENSORED point, gated out"
    )
    w("\\timing off")
    for qi, qy in enumerate(queries):
        qv = _vec_lit(emb[qy["x"]])
        expr = f"'embedding <-> ''{qv}'''"
        # filter-first predicate: entity-type constraint + the P-typed edge rooted at the anchor.
        filt = f"'P31 @> ARRAY[{qy['t']}] AND src={qy['x']} AND ptype={qy['p']}'"
        for ms, hops, tc in grid:
            tag = f"m{ms}h{hops}t{tc}"
            call = (
                f"SELECT t.id FROM tjs_open('{cfg.engine_table}', {k}, {tc}, {ms}, {hops}, "
                f"'id', {filt}, {expr}) AS t(id bigint)"
            )
            w(f"\\echo #WD TRIDB qid={qi} combo={tag}")
            w(f"\\echo #WD IDS qid={qi} combo={tag}")
            w(call + ";")
            w(f"\\echo #WD ENDIDS qid={qi} combo={tag}")
            w(
                f"SELECT '#WD EXAMINED qid={qi} combo={tag} examined=' || "
                f"tjs_open_candidates_examined() || ' bridges=' || "
                f"tjs_open_bridges_injected() AS line;"
            )
            w("\\timing on")
            for _ in range(runs):
                w(call + ";")
            w("\\timing off")
    w("\\echo #WD DONE")
    return "\n".join(out) + "\n"


# ======================================================================================
# REPORT — operating points + the reused honesty gate (host-runnable; latency pre-graded on GX10)
# ======================================================================================
def render_report(
    tridb_curve: dict[str, dict],
    baseline_curve: dict[str, dict],
    oracle_meta: dict,
    *,
    target: float,
) -> tuple[str, list[str]]:
    """Pick each side's fixed-recall operating point, run publication_gate, render markdown.

    The recall/latency CURVES are graded on the GX10 (where the timed transcript + the multi-store
    baseline live); this host step renders the comparison and REFUSES a headline ratio while any
    gate blocker stands. Returns (markdown, blockers)."""
    tp = operating_point(tridb_curve, target)
    bp = operating_point(baseline_curve, target)
    blockers = publication_gate(tp, bp, oracle_meta)
    L: list[str] = [
        "# TriDB Wikidata h2h — filter-first `tjs_open` vs multi-store (matched)",
        "",
    ]
    L.append(
        "> COMPUTE-BOUND regime (RAM-resident); value = fusion speed + one-WAL consistency. "
        "Latency/pages reported ONLY at matched recall. Seedless/vector-first mode blocked on 043."
    )
    L.append("")
    if blockers:
        L.append(
            "> **COMPARISON INVALID — no headline ratio emitted.** Reconcile first:"
        )
        L.append("")
        for b in blockers:
            L.append(f"> - {b}")
        return "\n".join(L) + "\n", blockers
    t_lat = tp[1]["median_latency_ms"]
    b_lat = bp[1]["median_latency_ms"]
    L.append(f"**Matched at recall≈{target:.2f}** (TriDB {tp[0]}, baseline {bp[0]}):")
    L.append("")
    L.append(f"- TriDB filter-first `tjs_open`: {t_lat:.2f} ms")
    L.append(f"- multi-store (Milvus+Neo4j+pg): {b_lat:.2f} ms")
    L.append(f"- **speedup: {b_lat / t_lat:.2f}×**")
    return "\n".join(L) + "\n", blockers


# ======================================================================================
# CLI
# ======================================================================================
DEFAULT_GRID = [(8, 1, 64), (16, 2, 128), (32, 2, 256)]


def _load_all(cfg: WCfg):
    manifest = load_manifest(cfg)
    qmap, dense_to_qid = load_dense_map(cfg, manifest)
    n = len(dense_to_qid)
    adj = load_typed_adj(cfg, manifest, qmap)
    types = load_types(cfg, manifest, qmap)
    return manifest, qmap, dense_to_qid, n, adj, types


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Matched Wikidata tjs_open vs multi-store h2h."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("oracle", "tridb-emit", "report"):
        p = sub.add_parser(name)
        p.add_argument("--k", type=int, default=10)
        p.add_argument("--queries", type=int, default=int(os.environ.get("WD_Q", "50")))
        p.add_argument("--seed", type=int, default=1354)
        p.add_argument("--hops", type=int, default=2)
        p.add_argument("--min-candidates", type=int, default=10)
        p.add_argument("--runs", type=int, default=3)
        p.add_argument("--target", type=float, default=0.90)
        p.add_argument(
            "--oracle",
            type=Path,
            default=Path("bench/results/wikidata_h2h_oracle.json"),
        )
        p.add_argument("--tridb-raw", type=Path)
        p.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    cfg = WCfg()

    if args.cmd == "oracle":
        manifest, qmap, dense_to_qid, n, adj, types = _load_all(cfg)
        emb = load_emb(cfg, n)
        queries = sample_queries(
            adj,
            types,
            args.queries,
            args.seed,
            hops=args.hops,
            min_candidates=args.min_candidates,
        )
        oracle = compute_oracle(emb, adj, types, queries, k=args.k, hops=args.hops)
        cand_sizes = [
            len(candidates(adj, types, qy["x"], qy["p"], qy["t"], args.hops))
            for qy in queries
        ]
        out = args.out or args.oracle
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "n": n,
                    "dim": cfg.dim,
                    "k": args.k,
                    "hops": args.hops,
                    "queries": queries,
                    "induced_edges": sum(len(v) for v in adj.values()),
                    "candidate_size_median": (
                        float(np.median(cand_sizes)) if cand_sizes else 0.0
                    ),
                    "oracle": {str(q): ids for q, ids in oracle.items()},
                },
                indent=2,
            )
        )
        print(
            f"[wikidata_h2h oracle] {len(queries)} queries, k={args.k}, N={n}, "
            f"induced_edges={sum(len(v) for v in adj.values())}, "
            f"median candidate set={float(np.median(cand_sizes)) if cand_sizes else 0:.0f}"
        )
        return 0

    if args.cmd == "tridb-emit":
        manifest, qmap, dense_to_qid, n, adj, types = _load_all(cfg)
        emb = load_emb(cfg, n)
        meta = json.loads(args.oracle.read_text())
        queries = meta["queries"]
        sql = emit_tridb_sql(
            cfg, emb, queries, DEFAULT_GRID, k=meta["k"], runs=args.runs
        )
        out = args.out or Path("/tmp/wikidata_h2h_tridb.sql")
        out.write_text(sql)
        print(
            f"[wikidata_h2h tridb-emit] {len(queries)} queries x {len(DEFAULT_GRID)} combos "
            f"-> {out} (GX10: docker exec -i {cfg.engine_container} psql -f - < {out})"
        )
        return 0

    if args.cmd == "report":
        # graded-curves JSON: {"tridb":{tag:{recall_at_k,median_latency_ms,median_examined}},
        # "baseline":{tag:{recall_at_k,median_latency_ms}}, "oracle_meta":{...}}. Grading raw
        # transcripts -> curves happens on the GX10 (where latency is measured); this renders+gates.
        graded = json.loads(args.oracle.read_text())
        md, blockers = render_report(
            graded.get("tridb", {}),
            graded.get("baseline", {}),
            graded.get("oracle_meta", {}),
            target=args.target,
        )
        out = args.out or Path("bench/results/wikidata_h2h_report.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        print(
            f"[wikidata_h2h report] {'BLOCKED (' + str(len(blockers)) + ' blockers)' if blockers else 'headline emitted'}"
            f" -> {out}"
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

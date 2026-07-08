"""MATCHED wiki head-to-head: TriDB fused `tjs_open` vs an isolated multi-store baseline.

MILESTONE A — make the wiki-scale head-to-head EXECUTE at the loaded N and the existing
dim-384 embeddings, comparing latency + pages-touched ONLY at FIXED, EQUAL recall. This is
the matched query harness the feasibility report (`docs/benchmark_wiki_scale_h2h_v0.1.0.md`,
"Blocker 3") said did not yet exist.

    TriDB     : the fused engine operator `tjs_open` (seedless ANN top-`m_seeds` on the
                HNSW vector leg -> native graph bridges within `hops` -> vector-ranked
                early-terminating top-k), one in-process round-trip. Latency via psql
                `\\timing`; pages-touched via `tjs_open_candidates_examined()` (SM-3 proxy).
    baseline  : the gBrain-style multi-store pipeline people actually run — Milvus ANN
                (seed) -> Neo4j hop expansion -> pgvector rerank/filter, fused app-side
                across THREE systems, end-to-end wall-clock.

RECALL TUNING (the whole point). Both sides are APPROXIMATIONS of one exact ground-truth:
the fused blocking oracle (realization A, `bench/tjs_open_ref.py`) computed host-side from
the SAME dim-384 embeddings + the SAME induced graph — exact ANN top-`m_seeds` UNION their
`hops`-reachable graph neighbors, reranked exactly by cosine, top-k. Recall@k of each side =
overlap with that oracle. We sweep each side's knobs (TriDB: m_seeds / hops / term_cond;
baseline: Milvus ef / seeds / hops), emit the recall curve, and only THEN read latency +
pages-touched at the operating point where the two recalls match a fixed target.

HONESTY (may become a public GTM claim):
  * This is the COMPUTE-BOUND, RAM-RESIDENT regime. dim-384 float32 over N=1e6 is a few GB;
    the engine's float8[] + HNSW fits in the Spark's 128 GB. It is NOT the spec's I/O-bound
    thesis (dim-768 float8 / chunk-scale > 128 GB = Milestone B). A latency number here does
    NOT vindicate or kill the speed thesis. State it in the report.
  * Compare latency ONLY at equal recall. A bare latency win at unequal recall is not a win.
  * N is whatever the engine actually loaded; the baseline is matched to the SAME N and the
    number is labelled honestly. No fabricated win.
  * ADR-0017 prior stands: TriDB's value is one-WAL consistency + source-anchored fused
    retrieval, not raw speed. This harness does not overturn it on a compute-regime number.

RUN LOCATION: the Spark, where all four stores + the id-aligned embeddings live. The engine
is a persistent container reached by `docker exec`; Milvus/Neo4j/PG by their mapped ports.

Phases (mirrors bench/h2h_report.py's emit/grade split):
    python -m bench.wiki_h2h oracle    --out bench/results/wiki_h2h_oracle.json   # ground truth
    python -m bench.wiki_h2h tridb-emit --oracle ... --out /tmp/wh_tridb.sql        # engine SQL
    docker exec -i <engine> psql -U postgres -d postgres -f - < /tmp/wh_tridb.sql > raw.txt
    python -m bench.wiki_h2h baseline  --oracle ... --out bench/results/wiki_h2h_baseline.json
    python -m bench.wiki_h2h report    --oracle ... --tridb-raw raw.txt --baseline ... [--target 0.9]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ======================================================================================
# Loaded-state contract (defaults match the live `tridb-wiki` project on the Spark,
# 2026-07-07). Every value is env-overridable so the harness is not pinned to one box.
# ======================================================================================


@dataclass
class Cfg:
    n: int = int(os.environ.get("WH_N", "1000000"))
    dim: int = int(os.environ.get("WH_DIM", "384"))
    # id-aligned dense embeddings: row i == article id i (float32[max_id+1, dim]).
    emb_path: Path = Path(
        os.environ.get("WH_EMB", "data/wiki/enwiki/emb/dense_id_aligned.npy")
    )
    manifest_dir: Path = Path(os.environ.get("WH_MANIFEST", "data/wiki/enwiki"))
    # engine (fused tjs_open): a persistent container, reached by docker exec.
    engine_container: str = os.environ.get("WH_ENGINE", "ecstatic_wright")
    engine_db: str = os.environ.get("WH_ENGINE_DB", "postgres")
    engine_table: str = os.environ.get("WH_ENGINE_TABLE", "articles")
    # baseline multi-store (isolated tridb-wiki ports).
    milvus_host: str = os.environ.get("WH_MILVUS_HOST", "localhost")
    milvus_port: str = os.environ.get("WH_MILVUS_PORT", "19531")
    milvus_collection: str = os.environ.get("WH_MILVUS_COLLECTION", "wiki_articles")
    milvus_metric: str = os.environ.get("WH_MILVUS_METRIC", "COSINE")
    neo4j_uri: str = os.environ.get("WH_NEO4J_URI", "bolt://localhost:7688")
    neo4j_user: str = os.environ.get("WH_NEO4J_USER", "neo4j")
    neo4j_password: str = os.environ.get("WH_NEO4J_PASSWORD", "wikipassword")
    neo4j_node_label: str = os.environ.get("WH_NEO4J_LABEL", "Article")
    neo4j_rel: str = os.environ.get("WH_NEO4J_REL", "RELATED")
    pg_host: str = os.environ.get("WH_PGHOST", "localhost")
    pg_port: str = os.environ.get("WH_PGPORT", "5434")
    pg_db: str = os.environ.get("WH_PGDB", "tridb_wiki")
    pg_user: str = os.environ.get("WH_PGUSER", "postgres")
    pg_password: str = os.environ.get("WH_PGPASSWORD", "postgres")
    pg_table: str = os.environ.get("WH_PGTABLE", "wiki_article")
    # TR-1 work cap — MUST match the C default `tjs_open_max_examined_guc` (4000). Used both to
    # SET the GUC explicitly in the emitted SQL (disclosed, NOT swept) and as the CENSORED gate:
    # a TriDB point whose median examined >= this cap is a truncated drain, not a natural finish.
    tjs_max_examined: int = int(os.environ.get("WH_TJS_MAX_EXAMINED", "4000"))


# ======================================================================================
# Corpus assets (embeddings + induced graph), shared by the oracle and the baseline rerank.
# ======================================================================================


def load_emb(cfg: Cfg) -> np.ndarray:
    """First-N id-aligned rows of the dense embedding matrix, L2-normalized (cosine).

    dense_id_aligned.npy is float32[max_id+1, dim] with row i == article id i, so a plain
    prefix [:N] is exactly the loaded slice's vectors (ids 0..N-1). BGE-small vectors are
    already unit-norm; we renormalize defensively so a dot product == cosine == the rank
    order of L2 on the unit sphere (the engine's `<->` l2 and Milvus COSINE agree)."""
    mm = np.load(cfg.emb_path, mmap_mode="r")
    if mm.shape[0] < cfg.n:
        raise SystemExit(
            f"embeddings have {mm.shape[0]} rows < N={cfg.n}; cannot ground-truth the slice"
        )
    emb = np.ascontiguousarray(mm[: cfg.n]).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-12
    return emb


def _article_shards(manifest: dict) -> list[str]:
    # order-preserving dedup: the enwiki manifest lists shard paths more than once
    # (extractor truncate/append) — reading a path twice double-counts edges.
    return list(
        dict.fromkeys(s["path"] for s in manifest["shards"]["articles"]["files"])
    )


def _edge_shards(manifest: dict) -> list[str]:
    files = manifest["shards"].get("edges", {}).get("files")
    if files:
        return list(dict.fromkeys(s["path"] for s in files))
    n = len(_article_shards(manifest))
    return [f"edges-{i:05d}.tsv" for i in range(n)]


def load_induced_adj(cfg: Cfg) -> dict[int, list[int]]:
    """out-adjacency of the induced subgraph on ids [0, N) (src<N AND dst<N).

    Same induced set the engine loader (`tools/wiki_engine_load.py`) staged, derived
    identically from the manifest so the oracle's graph == the loaded slice's graph. Edge
    shards are src-partitioned + index-aligned to article shards (shard i holds src in
    [i*shard_size, ...)), so only shards 0..floor((N-1)/shard_size) can carry an induced
    edge — the rest would rescan the 224M-edge corpus for nothing."""
    manifest = json.loads((cfg.manifest_dir / "manifest.json").read_text())
    shard_size = (
        manifest.get("shard_size")
        or manifest["shards"]["articles"]["files"][0]["rows"]
    )
    max_shard = (cfg.n - 1) // shard_size
    adj: dict[int, list[int]] = {}
    for idx, shard in enumerate(_edge_shards(manifest)):
        if idx > max_shard:
            break
        spath = cfg.manifest_dir / shard
        if not spath.exists():
            continue
        with spath.open() as fh:
            for line in fh:
                tab = line.find("\t")
                if tab < 0:
                    continue
                s = int(line[:tab])
                if s >= cfg.n:
                    continue
                d = int(line[tab + 1 :])
                if d >= cfg.n:
                    continue
                adj.setdefault(s, []).append(d)
    return adj


def sample_queries(cfg: Cfg, q: int, seed: int, emb: np.ndarray) -> list[int]:
    """Q article-anchored query ids from [0, N), fixed seed — the SAME queries both sides.

    Each query's vector is that article's own embedding (article-anchored multi-hop probe):
    this is `tjs_open`'s home regime (source-anchored fused retrieval, ADR-0017) and it is
    well defined for the whole loaded slice, unlike HotpotQA link-resolution which resolves
    too few gold titles into a 1M-article prefix to grade (tools/wiki_hotpot_link).

    The real corpus id space is SPARSE (289,612 gap ids across 0..7.19M); a gap id maps to a
    zero-filled row in dense_id_aligned.npy, so after L2-normalize it is a ~zero vector whose
    self-similarity is ~0 everywhere and whose oracle top-k is arbitrary — a degenerate noise
    query (reviewer finding, line 170). Draw only from rows with a non-degenerate unit norm so
    the recall average is not polluted by phantom-gap ids that neither store holds a row for."""
    norms = np.linalg.norm(emb, axis=1)
    valid = np.flatnonzero(norms > 0.5)  # normalized rows are ~1.0; gap rows are ~0.0
    if valid.size < q:
        raise SystemExit(
            f"only {valid.size} non-degenerate rows in [0,{cfg.n}); cannot sample {q} queries"
        )
    rng = np.random.default_rng(seed)
    return sorted(int(x) for x in rng.choice(valid, size=q, replace=False))


# ======================================================================================
# GROUND TRUTH — the exact fused blocking oracle (realization A). Both systems approximate it.
# ======================================================================================


def expand(adj: dict[int, list[int]], seeds: list[int], hops: int) -> set[int]:
    frontier = set(seeds)
    seen = set(seeds)
    for _ in range(hops):
        nxt: set[int] = set()
        for s in frontier:
            nxt.update(adj.get(s, ()))
        nxt -= seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return seen


def compute_oracle(
    emb: np.ndarray,
    adj: dict[int, list[int]],
    qids: list[int],
    *,
    k: int,
    m_seeds: int,
    hops: int,
) -> dict[int, list[int]]:
    """Per query: exact top-`m_seeds` by cosine over the whole slice, UNION their
    `hops`-reachable induced-graph neighbors, reranked exactly by cosine -> top-k.

    Generous (m_seeds, hops) define the IDEAL fused answer the approximate operators chase;
    grading both against it is the matched-recall definition. Exact ANN via one dense matmul
    per query (N x dim, RAM-resident — the compute-bound regime, stated honestly)."""
    oracle: dict[int, list[int]] = {}
    for qid in qids:
        sims = emb @ emb[qid]
        seed_ids = np.argpartition(-sims, m_seeds)[:m_seeds].tolist()
        cand = expand(adj, seed_ids, hops) | set(seed_ids)
        cand_arr = np.fromiter(cand, dtype=np.int64, count=len(cand))
        cs = sims[cand_arr]
        top = cand_arr[np.argsort(-cs)][:k].tolist()
        oracle[qid] = [int(x) for x in top]
    return oracle


def recall_at_k(top: list[int], gold: list[int], k: int) -> float:
    g = set(gold[:k])
    return (len(g & set(top[:k])) / len(g)) if g else float("nan")


# ======================================================================================
# TriDB SIDE — emit the sweep SQL for the engine's fused tjs_open, then parse the transcript.
# ======================================================================================

_QB = re.compile(r"#WH TRIDB qid=(\d+) combo=(\S+)")
_IDS = re.compile(r"#WH IDS qid=(\d+) combo=(\S+)")
_EX = re.compile(r"#WH EXAMINED qid=(\d+) combo=(\S+) examined=(\d+) bridges=(\d+)")
_TIME = re.compile(r"Time:\s+([\d.]+)\s+ms")
_INT = re.compile(r"^\s*(\d+)\s*$")


def _vec_lit(v) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def combo_tag(m_seeds: int, hops: int, term_cond: int) -> str:
    return f"m{m_seeds}h{hops}t{term_cond}"


def emit_tridb_sql(
    cfg: Cfg,
    emb: np.ndarray,
    qids: list[int],
    grid: list[tuple[int, int, int]],
    *,
    k: int,
    runs: int,
) -> str:
    """Per (query, knob-combo): one warm-up (also the graded ids + SM-3 counters) then
    `runs` \\timing'd repeats. Knobs swept: (m_seeds, hops, term_cond). The query vector is
    the article's own embedding, inlined as a float8[] text literal (the operator's calling
    convention, matching tools/wiki_engine_load).

    WARNING (reviewer finding, line 269): `SET enable_seqscan=off` does NOT guarantee the
    HNSW leg. On the shipped gx10-v1 image the `float8[] <->` distance operator does not bind
    the `articles_hnsw` index, so tjs_open's vector leg seqscans 1M×384 and cancels at the
    statement timeout (examined=0) at N=1M. Any timing harvested from this SQL is trustworthy
    ONLY if the EXAMINED counter is > 0 — the report gate below refuses a TriDB point whose
    median examined is 0 (a silent seqscan / timeout). Fix the opclass binding before quoting
    a fixed-recall latency point."""
    out: list[str] = []
    w = out.append
    w("\\set ON_ERROR_STOP on")
    w("\\pset pager off")
    w("SET enable_seqscan = off;  -- NB: does NOT force HNSW here; gate on EXAMINED>0 (see docstring)")
    # TRIDB DEV-1354 — TR-1 SAFETY bound, disclosed and NOT swept (reviewer finding: the cap is not a
    # validated recall/latency tunable). A point whose examined reaches this ceiling is a truncated
    # (censored) drain — the report gate flags median examined >= cap CENSORED and excludes it.
    w(
        f"SET vectordb.tjs_open_max_examined = {cfg.tjs_max_examined};"
        "  -- TR-1 work cap; examined>=cap => CENSORED point, gated out of the matched comparison"
    )
    w("\\timing off")
    for qid in qids:
        qv = _vec_lit(emb[qid])
        expr = f"'embedding <-> ''{qv}'''"
        for (ms, hops, tc) in grid:
            tag = combo_tag(ms, hops, tc)
            call = (
                f"SELECT t.id FROM tjs_open('{cfg.engine_table}', {k}, {tc}, {ms}, {hops}, "
                f"'id', '', {expr}) AS t(id bigint)"
            )
            w(f"\\echo #WH TRIDB qid={qid} combo={tag}")
            w(f"\\echo #WH IDS qid={qid} combo={tag}")
            w(call + ";")  # warm-up + graded id set
            w(f"\\echo #WH ENDIDS qid={qid} combo={tag}")
            w(
                f"SELECT '#WH EXAMINED qid={qid} combo={tag} examined=' || "
                f"tjs_open_candidates_examined() || ' bridges=' || "
                f"tjs_open_bridges_injected() AS line;"
            )
            w("\\timing on")
            for _ in range(runs):
                w(call + ";")
            w("\\timing off")
    w("\\echo #WH DONE")
    return "\n".join(out) + "\n"


def parse_tridb(raw: str) -> dict[tuple[int, str], dict]:
    if "#WH DONE" not in raw:
        raise SystemExit("TriDB transcript did not reach '#WH DONE' — incomplete")
    res: dict[tuple[int, str], dict] = {}
    cur: tuple[int, str] | None = None
    in_ids = False
    for line in raw.splitlines():
        mi = _IDS.search(line)
        if mi:
            cur = (int(mi[1]), mi[2])
            res.setdefault(cur, {"ids": [], "times": [], "examined": None, "bridges": None})
            in_ids = True
            continue
        if line.startswith("\\echo") or "#WH ENDIDS" in line:
            in_ids = False
        me = _EX.search(line)
        if me:
            key = (int(me[1]), me[2])
            d = res.setdefault(key, {"ids": [], "times": [], "examined": None, "bridges": None})
            d["examined"] = int(me[3])
            d["bridges"] = int(me[4])
            in_ids = False
            continue
        if cur is not None and in_ids:
            m = _INT.match(line)
            if m:
                res[cur]["ids"].append(int(m[1]))
            continue
        mt = _TIME.search(line)
        if mt and cur is not None:
            res[cur]["times"].append(float(mt[1]))
    return res


# ======================================================================================
# BASELINE SIDE — live Milvus ANN -> Neo4j hop -> pgvector rerank, fused app-side, warm.
# ======================================================================================


def _connect_baseline(cfg: Cfg):
    from pymilvus import Collection, connections
    from neo4j import GraphDatabase
    import psycopg

    connections.connect(alias="wh", host=cfg.milvus_host, port=cfg.milvus_port)
    col = Collection(cfg.milvus_collection, using="wh")
    col.load()
    driver = GraphDatabase.driver(
        cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password)
    )
    pg = psycopg.connect(
        host=cfg.pg_host,
        port=cfg.pg_port,
        dbname=cfg.pg_db,
        user=cfg.pg_user,
        password=cfg.pg_password,
    )
    return col, driver, pg


def run_baseline(
    cfg: Cfg,
    emb: np.ndarray,
    qids: list[int],
    grid: list[tuple[int, int]],
    *,
    k: int,
    runs: int,
    use_pg_rerank: bool = True,
) -> dict[str, dict]:
    """Sweep (ef, seeds, hops) live across the three stores. grid is [(seeds, hops), ...];
    Milvus ef swept via WH_BASELINE_EFS. Returns {"m{seeds}h{hops}e{ef}": {qid: {...}}}."""
    col, driver, pg = _connect_baseline(cfg)
    efs = [int(x) for x in os.environ.get("WH_BASELINE_EFS", "32,64,128,256").split(",")]
    cur = pg.cursor()

    def milvus_seed(qv, seeds, ef):
        res = col.search(
            [qv.tolist()],
            "embedding",
            {"metric_type": cfg.milvus_metric, "params": {"ef": ef}},
            limit=seeds,
            output_fields=["id"],
        )
        return [int(h.id) for h in res[0]]

    def neo4j_hop(seed_ids, hops):
        cy = (
            f"MATCH (a:{cfg.neo4j_node_label})-[:{cfg.neo4j_rel}*1..{hops}]->"
            f"(b:{cfg.neo4j_node_label}) WHERE a.id IN $ids RETURN DISTINCT b.id AS id"
        )
        with driver.session() as s:
            rows = s.run(cy, ids=seed_ids)
            return {int(r["id"]) for r in rows}

    def pg_rerank(qv, cand, k):
        # pgvector filter+rerank over the reached candidate set (exact — no ANN index on
        # wiki_article; the reached set is small). '<=>' is cosine distance.
        lit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
        cur.execute(
            f"SELECT id FROM {cfg.pg_table} WHERE id = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector LIMIT %s",
            (list(cand), lit, k),
        )
        return [int(r[0]) for r in cur.fetchall()]

    out: dict[str, dict] = {}
    for (seeds, hops) in grid:
        for ef in efs:
            tag = f"m{seeds}h{hops}e{ef}"
            per: dict[int, dict] = {}
            for qid in qids:
                qv = emb[qid]

                def one():
                    t0 = time.perf_counter()
                    seed_ids = milvus_seed(qv, seeds, ef)
                    t1 = time.perf_counter()
                    reach = neo4j_hop(seed_ids, hops) | set(seed_ids)
                    t2 = time.perf_counter()
                    if use_pg_rerank:
                        top = pg_rerank(qv, reach, k)
                    else:
                        cand = np.fromiter(reach, dtype=np.int64, count=len(reach))
                        top = [
                            int(x)
                            for x in cand[np.argsort(-(emb[cand] @ qv))][:k]
                        ]
                    t3 = time.perf_counter()
                    return top, (
                        (t1 - t0) * 1e3,
                        (t2 - t1) * 1e3,
                        (t3 - t2) * 1e3,
                    )

                top, _ = one()  # warm-up (excluded)
                times, legs_last = [], None
                for _ in range(runs):
                    top, legs = one()
                    times.append(sum(legs))
                    legs_last = legs
                per[qid] = {
                    "ids": top,
                    "median_ms": float(statistics.median(times)),
                    "milvus_ms": legs_last[0],
                    "neo4j_ms": legs_last[1],
                    "rerank_ms": legs_last[2],
                }
            out[tag] = per
    cur.close()
    pg.close()
    driver.close()
    return out


# ======================================================================================
# GRADING + TUNING + REPORT
# ======================================================================================


def grade_tridb(parsed: dict, oracle: dict, k: int) -> dict[str, dict]:
    by_combo: dict[str, dict] = {}
    for (qid, tag), d in parsed.items():
        g = oracle.get(qid) or oracle.get(str(qid))
        if g is None:
            continue
        r = recall_at_k(d["ids"], g, k)
        c = by_combo.setdefault(tag, {"recall": [], "latency": [], "examined": []})
        if r == r:
            c["recall"].append(r)
        if d["times"]:
            c["latency"].append(float(statistics.median(d["times"])))
        if d["examined"] is not None:
            c["examined"].append(d["examined"])
    return {
        tag: {
            "recall_at_k": float(np.mean(c["recall"])) if c["recall"] else float("nan"),
            "median_latency_ms": float(np.median(c["latency"])) if c["latency"] else float("nan"),
            "median_examined": float(np.median(c["examined"])) if c["examined"] else float("nan"),
            "n_queries": len(c["recall"]),
        }
        for tag, c in by_combo.items()
    }


def grade_baseline(baseline: dict, oracle: dict, k: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tag, per in baseline.items():
        recs, lats = [], []
        for qid, d in per.items():
            g = oracle.get(qid) or oracle.get(str(qid))
            if g is None:
                continue
            r = recall_at_k(d["ids"], g, k)
            if r == r:
                recs.append(r)
            lats.append(d["median_ms"])
        out[tag] = {
            "recall_at_k": float(np.mean(recs)) if recs else float("nan"),
            "median_latency_ms": float(np.median(lats)) if lats else float("nan"),
            "n_queries": len(recs),
        }
    return out


def operating_point(curve: dict[str, dict], target: float) -> tuple[str, dict] | None:
    """Lowest-latency combo whose recall >= target (the fixed-accuracy point)."""
    ok = [
        (tag, c)
        for tag, c in curve.items()
        if c["recall_at_k"] == c["recall_at_k"] and c["recall_at_k"] >= target
    ]
    if not ok:
        return None
    return min(ok, key=lambda kv: kv[1]["median_latency_ms"])


def _int(s) -> int | None:
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# max |recall_tridb - recall_baseline| still called "equal recall" (reviewer finding, line 502).
RECALL_EPS = float(os.environ.get("WH_RECALL_EPS", "0.02"))


def publication_gate(
    tp: tuple[str, dict] | None,
    bp: tuple[str, dict] | None,
    oracle_meta: dict,
) -> list[str]:
    """Blockers that must ALL clear before a latency ratio / 'Yx' headline may be printed.

    A public GTM number cannot ride on a caveat footnote. This encodes the reviewer's
    blocker + majors as hard gates: the harness refuses the headline until they reconcile.
    Returns [] when publishable, else a list of human-readable blocker strings."""
    blockers: list[str] = []

    # Finding 1 / critical: the two systems must benchmark the SAME graph. The oracle and
    # Neo4j hold the manifest-induced edge set; the engine's native graph must match it
    # byte-for-byte on the same slice, else recall/latency/pages-touched are cross-mismatched.
    e_edges = _int(oracle_meta.get("engine_edges"))
    n_edges = _int(oracle_meta.get("neo4j_edges"))
    if e_edges is None or n_edges is None:
        blockers.append(
            "graph-set: engine/oracle edge counts unknown — cannot confirm both systems "
            "traverse the same topology (set WH_ENGINE_EDGES / WH_NEO4J_EDGES)."
        )
    elif e_edges != n_edges:
        blockers.append(
            f"graph-set MISMATCH: engine native graph = {e_edges:,} edges vs oracle/Neo4j "
            f"induced = {n_edges:,} edges ({n_edges / max(e_edges, 1):.2f}x) on the same "
            "slice. TriDB is graded against a graph it does not physically contain. "
            "Root-cause the edge-load path (dedup / identity-mode / directedness) or rebuild "
            "the oracle + Neo4j from the engine's ACTUAL loaded adjacency before comparing."
        )

    # Finding 2 / major: timer-boundary parity. TriDB latency is server-side psql \\timing
    # over a local unix socket; the baseline is Python wall-clock over three TCP round-trips.
    # Refuse the ratio until someone equalizes the boundary and asserts it via the env flag.
    if os.environ.get("WH_BOUNDARY_PARITY", "").lower() not in ("1", "true", "yes"):
        blockers.append(
            "timer boundary asymmetry: TriDB = server-side \\timing (local socket), baseline "
            "= client-side wall-clock over 3 TCP round-trips + driver serialization. The ratio "
            "flatters TriDB by construction. Measure both at the same boundary, then set "
            "WH_BOUNDARY_PARITY=1 to acknowledge it."
        )

    # Finding 3 / HIGH (cherry-pick hazard): the HNSW index build is RANDOMIZED. The one flattering
    # TriDB point (examined=90, ~3ms, recall 1.0) came from a single healthy build while FOUR fresh
    # builds hung the vector leg (examined=0, statement-timeout). Reporting the lucky build and
    # discarding the hung ones manufactures a win. Refuse any headline until the vector leg is
    # reproducibly healthy across N>=3 fresh builds (or the failure rate is disclosed).
    min_healthy = int(os.environ.get("WH_MIN_HEALTHY_BUILDS", "3"))
    healthy = _int(oracle_meta.get("hnsw_healthy_builds"))
    total = _int(oracle_meta.get("hnsw_total_builds"))
    if healthy is None or total is None:
        blockers.append(
            "HNSW build health UNDECLARED: the vector-leg index build is randomized and was "
            "observed to hang (examined=0, statement-timeout) on 4 of 5 fresh builds. Declare "
            f"WH_HNSW_HEALTHY_BUILDS / WH_HNSW_TOTAL_BUILDS (need all of >= {min_healthy} healthy, "
            "0 hung) before a latency/recall headline — a number from one lucky build is a cherry-pick."
        )
    elif total < min_healthy or healthy < total:
        blockers.append(
            f"HNSW build NON-REPRODUCIBLE: {healthy}/{total} fresh builds produced a healthy vector "
            f"leg (need all of >= {min_healthy}); the rest hung before the first candidate "
            "(examined=0). The examined-cap does NOT fix this — the hang is upstream of the first "
            "examined++. Root-cause the HNSW relaxed-monotonicity / opclass binding in the fork's "
            "vector iterator before quoting any TriDB latency/recall."
        )

    if tp is None or bp is None:
        blockers.append(
            "no matched operating point: "
            + ("TriDB " if tp is None else "")
            + ("baseline " if bp is None else "")
            + "has no combo clearing the recall target in the swept grid."
        )
        return blockers

    # Finding 3 / major: 'fixed EQUAL recall' means MATCHED, not merely both-above-threshold.
    tr = tp[1]["recall_at_k"]
    br = bp[1]["recall_at_k"]
    if abs(tr - br) > RECALL_EPS:
        blockers.append(
            f"recall NOT matched: TriDB operates at recall {tr:.3f}, baseline at {br:.3f} "
            f"(|Δ|={abs(tr - br):.3f} > eps {RECALL_EPS:.3f}). A latency ratio at unequal "
            "recall is apples-to-oranges — interpolate both sides to a common recall first."
        )

    # Finding 6 / minor promoted: a TriDB point with median examined == 0 is a silent seqscan
    # or statement-timeout (the float8[] <-> leg not binding articles_hnsw), not a real point.
    ex = tp[1].get("median_examined")
    if ex is not None and ex == ex and ex <= 0:
        blockers.append(
            "TriDB vector leg did NOT use the HNSW index: median candidates examined = 0 "
            "(seqscan / statement-timeout). No real fixed-recall latency point exists — fix "
            "the float8[] <-> opclass binding before quoting a number."
        )

    # Finding 5 / medium: a point whose examined hit the TR-1 cap is a TRUNCATED (censored) drain.
    # Its recall is a censored FLOOR, not the operator's true recall, yet the pull-site returns NULL
    # identically to a natural finish. examined >= cap IS the cap-engaged signal (a natural finish
    # stops strictly below the cap; the C returns at examined>=cap). Never quote a latency win at a
    # censored point, and never interpolate the recall curve through one.
    cap = _int(oracle_meta.get("tjs_max_examined"))
    if cap and ex is not None and ex == ex and ex >= cap:
        blockers.append(
            f"TriDB operating point is CENSORED: median examined = {ex:.0f} reached the TR-1 work "
            f"cap (vectordb.tjs_open_max_examined = {cap}), so its recall is a truncated FLOOR, not "
            "the true operator recall. Excluded from the matched comparison — raise the cap until "
            "the drain finishes naturally (examined < cap), or interpolate around it, before "
            "quoting a fixed-recall latency point."
        )
    return blockers


def render_md(
    cfg: Cfg,
    tridb: dict,
    baseline: dict,
    *,
    k: int,
    target: float,
    q: int,
    oracle_meta: dict,
) -> str:
    L: list[str] = []
    w = L.append
    w("# TriDB Benchmark — MATCHED wiki head-to-head: fused `tjs_open` vs multi-store")
    w("")
    w(
        "> **Regime caveat (read first).** This is the COMPUTE-BOUND, RAM-RESIDENT regime: "
        f"N={cfg.n:,} × dim-{cfg.dim} float32 embeddings fit in the Spark's 128 GB. It is "
        "NOT the spec's I/O-bound thesis (dim-768 float8 / chunk-scale > 128 GB = Milestone "
        "B). A latency number here neither vindicates nor kills the speed thesis. ADR-0017 "
        "stands: TriDB's value is one-WAL consistency + source-anchored fused retrieval."
    )
    w("")
    tp = operating_point(tridb, target)
    bp = operating_point(baseline, target)
    blockers = publication_gate(tp, bp, oracle_meta)
    if blockers:
        w(
            "> **COMPARISON INVALID — no headline latency ratio is emitted.** The following "
            "must reconcile before a matched `tjs_open`-vs-multi-store number is publishable "
            "(reviewer blocker + majors; a caveat footnote is not sufficient for a GTM claim):"
        )
        w("")
        for b in blockers:
            w(f"> - {b}")
        w("")
        w(
            "> Recall curves for each side are printed below for diagnosis, but they are "
            "graded against the manifest-induced oracle and are NOT a matched head-to-head."
        )
    elif tp and bp:
        tt, tc = tp
        bt, bc = bp
        ratio = (
            bc["median_latency_ms"] / tc["median_latency_ms"]
            if tc["median_latency_ms"]
            else float("nan")
        )
        w(
            f"**At a fixed recall@{k} >= {target:.2f} vs the exact fused oracle, over {q} "
            f"article-anchored queries at N={cfg.n:,}:** TriDB's fused `tjs_open` "
            f"(`{tt}`, recall {tc['recall_at_k']:.3f}) runs **{tc['median_latency_ms']:.2f} ms**; "
            f"the multi-store baseline (`{bt}`, recall {bc['recall_at_k']:.3f}) runs "
            f"**{bc['median_latency_ms']:.2f} ms** end-to-end across three systems "
            f"(**{ratio:.2f}x**). Both timers measured at the SAME boundary "
            "(WH_BOUNDARY_PARITY acknowledged)."
        )
    w("")
    w(f"## TriDB fused `tjs_open` — recall curve (warm, median of runs), N={cfg.n:,}")
    w("")
    w("| combo (m_seeds/hops/term_cond) | recall@k | latency (ms) | candidates examined (SM-3) |")
    w("|---|---:|---:|---:|")
    for tag, c in sorted(tridb.items(), key=lambda kv: kv[1]["recall_at_k"]):
        w(
            f"| {tag} | {c['recall_at_k']:.3f} | {c['median_latency_ms']:.2f} | "
            f"{c['median_examined']:.0f} |"
        )
    w("")
    w("## Multi-store baseline — recall curve (Milvus ef / seeds / hops), warm")
    w("")
    w("| combo (seeds/hops/ef) | recall@k | end-to-end latency (ms) |")
    w("|---|---:|---:|")
    for tag, c in sorted(baseline.items(), key=lambda kv: kv[1]["recall_at_k"]):
        w(f"| {tag} | {c['recall_at_k']:.3f} | {c['median_latency_ms']:.2f} |")
    w("")
    w("## Ground truth + honesty notes")
    w("")
    w(
        f"- **Oracle = exact fused blocking realization A**: exact top-{oracle_meta['m_seeds']} "
        f"by cosine UNION their {oracle_meta['hops']}-hop induced-graph neighbours, reranked "
        f"exactly, top-{k}. Both sides are approximations of THIS; recall = overlap with it. "
        "Latency is compared ONLY at the matched operating point above."
    )
    w(
        f"- **Same queries both sides:** {q} article-anchored ids sampled with a fixed seed "
        "from the loaded slice; each query vector is the article's own dim-384 embedding "
        "(source-anchored, `tjs_open`'s home regime)."
    )
    w(
        "- **Pages-touched (SM-3) is engine-internal and NON-COMPARABLE (reviewer finding).** "
        "`tjs_open_candidates_examined()` is shown in the TriDB curve table only as an "
        "early-termination diagnostic — NOT as a head-to-head win. The baseline has no "
        "comparable counter, AND a low examined count partly reflects the engine holding "
        "fewer edges than the oracle/Neo4j (see graph-set blocker), not superior locality. "
        "It is deliberately kept out of the headline until the graph legs reconcile."
    )
    w(
        "- **Warm** on both sides (engine buffers hot, Milvus/Neo4j collections loaded, PG "
        "cached). Reported consistently; no cold-cache number is mixed in."
    )
    w(
        f"- **HNSW build reproducibility (reviewer BLOCKER).** The vector-leg index build is "
        f"RANDOMIZED; healthy fresh builds declared: {oracle_meta.get('hnsw_healthy_builds', '?')}/"
        f"{oracle_meta.get('hnsw_total_builds', '?')}. A latency/recall headline is gated "
        "(comparison invalid) until the vector leg is healthy across ALL of >= 3 fresh builds — one "
        "lucky build (the origin of the flattering examined=90 / ~3ms / recall 1.0 point) is a "
        "cherry-pick, not a result. The four hung builds (examined=0) are disclosed, not discarded."
    )
    w(
        f"- **TR-1 work cap = SAFETY bound, not a tunable (reviewer finding).** "
        f"`vectordb.tjs_open_max_examined` (default {cfg.tjs_max_examined}) guarantees bounded "
        "termination at any N; it is NOT a validated recall/latency knob and no cap->recall SM-4 "
        "curve has been produced through it. In the observed healthy regime it is an UNEXERCISED "
        "no-op (drains finish at examined~=90, ~44x under the cap). Any point that DOES reach the "
        "cap is reported CENSORED (truncated recall floor) and excluded from the matched comparison."
    )
    w(
        f"- **Graph-set BLOCKER (not a caveat):** the engine's native graph reports "
        f"{oracle_meta.get('engine_edges', '?')} edges while Neo4j / the oracle hold "
        f"{oracle_meta.get('neo4j_edges', '?')} induced relationships for the same slice. "
        "The two graph legs are not byte-identical, so TriDB is graded against a graph it "
        "does not physically contain. This gates the headline ratio above (comparison "
        "invalid) until the edge counts reconcile — it is NOT demoted to a footnote."
    )
    w("")
    w("_Generated by `bench/wiki_h2h.py`. Numbers observed; no result fabricated._")
    return "\n".join(L) + "\n"


# ======================================================================================
# CLI
# ======================================================================================

DEFAULT_TRIDB_GRID = [(4, 1, 32), (8, 1, 64), (8, 2, 128), (16, 2, 256)]
DEFAULT_BASELINE_GRID = [(4, 1), (8, 1), (8, 2), (16, 2)]


def _grid_env(name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    out = []
    for part in raw.split(";"):
        out.append(tuple(int(x) for x in part.split(",")))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Matched wiki tjs_open vs multi-store h2h.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("oracle", "tridb-emit", "baseline", "report"):
        p = sub.add_parser(name)
        p.add_argument("--k", type=int, default=10)
        p.add_argument("--queries", type=int, default=int(os.environ.get("WH_Q", "50")))
        p.add_argument("--seed", type=int, default=1354)
        p.add_argument("--oracle-mseeds", type=int, default=16)
        p.add_argument("--oracle-hops", type=int, default=2)
        p.add_argument("--runs", type=int, default=3)
        p.add_argument("--target", type=float, default=0.90)
        p.add_argument("--oracle", type=Path, default=Path("bench/results/wiki_h2h_oracle.json"))
        p.add_argument("--tridb-raw", type=Path)
        p.add_argument("--baseline", type=Path, default=Path("bench/results/wiki_h2h_baseline.json"))
        p.add_argument("--out", type=Path)
        p.add_argument("--md-out", type=Path, default=Path("bench/results/wiki_h2h_report.md"))
        p.add_argument("--no-pg-rerank", action="store_true")
    args = ap.parse_args(argv)
    cfg = Cfg()

    if args.cmd == "oracle":
        emb = load_emb(cfg)
        adj = load_induced_adj(cfg)
        qids = sample_queries(cfg, args.queries, args.seed, emb)
        t0 = time.time()
        oracle = compute_oracle(
            emb, adj, qids, k=args.k, m_seeds=args.oracle_mseeds, hops=args.oracle_hops
        )
        out = args.out or args.oracle
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "n": cfg.n,
                    "dim": cfg.dim,
                    "k": args.k,
                    "queries": qids,
                    "oracle_mseeds": args.oracle_mseeds,
                    "oracle_hops": args.oracle_hops,
                    "induced_edges": sum(len(v) for v in adj.values()),
                    "oracle": {str(q): ids for q, ids in oracle.items()},
                },
                indent=2,
            )
        )
        print(
            f"[wiki_h2h oracle] {len(qids)} queries, k={args.k}, "
            f"induced_edges={sum(len(v) for v in adj.values())}, "
            f"{time.time() - t0:.1f}s -> {out}"
        )
        return 0

    if args.cmd == "tridb-emit":
        emb = load_emb(cfg)
        meta = json.loads(args.oracle.read_text())
        qids = meta["queries"]
        grid = _grid_env("WH_TRIDB_GRID", DEFAULT_TRIDB_GRID)
        sql = emit_tridb_sql(cfg, emb, qids, grid, k=meta["k"], runs=args.runs)
        out = args.out or Path("/tmp/wiki_h2h_tridb.sql")
        out.write_text(sql)
        print(
            f"[wiki_h2h tridb-emit] {len(qids)} queries x {len(grid)} combos x {args.runs} "
            f"runs -> {out}\n  run: docker exec -i {cfg.engine_container} psql -U {cfg.pg_user} "
            f"-d {cfg.engine_db} -f - < {out} > /tmp/wiki_h2h_tridb_raw.txt 2>&1"
        )
        return 0

    if args.cmd == "baseline":
        emb = load_emb(cfg)
        meta = json.loads(args.oracle.read_text())
        qids = meta["queries"]
        grid = _grid_env("WH_BASELINE_GRID", DEFAULT_BASELINE_GRID)
        res = run_baseline(
            cfg, emb, qids, grid, k=meta["k"], runs=args.runs,
            use_pg_rerank=not args.no_pg_rerank,
        )
        out = args.out or args.baseline
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({str(t): {str(q): d for q, d in per.items()} for t, per in res.items()}, indent=2))
        print(f"[wiki_h2h baseline] {len(qids)} queries x {len(res)} combos -> {out}")
        return 0

    if args.cmd == "report":
        meta = json.loads(args.oracle.read_text())
        oracle = {int(q): ids for q, ids in meta["oracle"].items()}
        k = meta["k"]
        tridb = grade_tridb(parse_tridb(args.tridb_raw.read_text()), oracle, k) if args.tridb_raw else {}
        braw = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
        baseline = grade_baseline(
            {t: {int(q): d for q, d in per.items()} for t, per in braw.items()}, oracle, k
        )
        oracle_meta = {
            "m_seeds": meta["oracle_mseeds"],
            "hops": meta["oracle_hops"],
            "engine_edges": os.environ.get("WH_ENGINE_EDGES", "21,945,976"),
            "neo4j_edges": os.environ.get("WH_NEO4J_EDGES", "38,991,320"),
            "tjs_max_examined": cfg.tjs_max_examined,
            "hnsw_healthy_builds": os.environ.get("WH_HNSW_HEALTHY_BUILDS"),
            "hnsw_total_builds": os.environ.get("WH_HNSW_TOTAL_BUILDS"),
        }
        md = render_md(
            cfg, tridb, baseline, k=k, target=args.target,
            q=len(meta["queries"]), oracle_meta=oracle_meta,
        )
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(md)
        jout = args.out or Path("bench/results/wiki_h2h_metrics.json")
        jout.write_text(json.dumps({"tridb": tridb, "baseline": baseline, "target": args.target}, indent=2))
        print(f"[wiki_h2h report] -> {args.md_out} / {jout}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

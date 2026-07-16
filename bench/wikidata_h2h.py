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

FIVE COMMANDS (mirror wiki_h2h): `oracle` (exact fused ground truth, runs anywhere on a slice's
assets), `tridb-emit` (the filter-first tjs_open sweep SQL — GX10), `baseline` (the live
multi-store leg, Neo4j traversal + pg type-filter + pg rerank — GX10), `grade` (raw psql
transcript + baseline JSON -> the graded curves JSON `report` consumes; curves only, no headline
math), `report` (render + gate the headline). Only `oracle`/`grade`/`report` + the pure helpers
run on the x86 standin; the live legs are GX10/Spark-gated, same boundary as wiki_h2h.

HONESTY (inherited): COMPUTE-BOUND at 1M (RAM-resident dim-D floats); the I/O-locality thesis is
dead (wiki-scale memory). Value = fusion speed + one-WAL consistency (Harness A). Latency /
pages-touched are reported ONLY at matched recall; the seedless mode is labeled blocked-on-043.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Reuse the generic scoring + the honesty gate VERBATIM (plan 060: "Reuse publication_gate()
# unchanged"). operating_point / _vec_lit / publication_gate are query-shape-agnostic, and so
# are the grading reducers (grade_tridb / grade_baseline: they consume the parsed per-(qid,tag)
# dict / the per-combo baseline JSON, both of which this file produces in the same shape) and
# the transcript micro-parsers (_TIME / _INT) + the grid env parser (_grid_env).
from bench.wiki_h2h import (
    _INT,
    _TIME,
    _grid_env,
    _vec_lit,
    grade_baseline,
    grade_tridb,
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
    # fork = MSVBASE vectordb (float8[] embedding, {..} literals); stock = pgvector on
    # stock PG 17 (vector(dim) embedding, [..] literals) — D2 un-fork / Gate B spike.
    engine_dialect: str = os.environ.get("WD_ENGINE_DIALECT", "fork")
    # baseline multi-store (isolated ports; mirror wiki_h2h layout)
    milvus_host: str = os.environ.get("WD_MILVUS_HOST", "localhost")
    milvus_port: str = os.environ.get("WD_MILVUS_PORT", "19531")
    milvus_collection: str = os.environ.get("WD_MILVUS_COLLECTION", "wikidata_entities")
    neo4j_uri: str = os.environ.get("WD_NEO4J_URI", "bolt://localhost:7688")
    neo4j_user: str = os.environ.get("WD_NEO4J_USER", "neo4j")
    neo4j_password: str = os.environ.get("WD_NEO4J_PASSWORD", "wikipassword")
    neo4j_node_label: str = os.environ.get("WD_NEO4J_LABEL", "Entity")
    pg_host: str = os.environ.get("WD_PGHOST", "localhost")
    pg_port: str = os.environ.get("WD_PGPORT", "5434")
    pg_db: str = os.environ.get("WD_PGDB", "tridb_wikidata")
    pg_user: str = os.environ.get("WD_PGUSER", "postgres")
    pg_password: str = os.environ.get("WD_PGPASSWORD", "postgres")
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
            # A cycle back to src (x --P47--> a --P47--> x on symmetric properties) must NOT
            # put the anchor in its own reach — the documented contract ("excludes src"), the
            # engine BFS's structural behavior (gph_traverse_bfs excludes the seed), and sane
            # KBQA semantics all agree. Caught live at 1M: 33/50 queries missed only [x].
            if dst != src and dst not in seen:
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
        # lexsort: last key is primary. Primary -sims (nearest first), secondary cand_arr
        # (id ascending) pins ties the same way the engine's `ORDER BY <->, e.id` does.
        order = np.lexsort((cand_arr, -sims))
        top = cand_arr[order][:k].tolist()
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
# TriDB SIDE — emit the fused filter-first statement (GX10-gated; mirrors wiki_h2h).
#
# SURFACE HONESTY (2026-07-14, first live engine run): tjs_open has NO typed-traversal slot —
# plan 038 landed typed traversal as native AM SRFs (gph_traverse_typed / gph_traverse_bfs),
# not as an operator argument (the switch ADR-0018's consequences anticipate). The KBQA
# filter-first query is therefore realized as ONE fused SQL statement over the native
# surfaces: C multi-hop typed BFS -> relational P31 filter -> exact vector rank, one
# round-trip, one system, one snapshot — semantically identical to the oracle (both sides
# exact => recall matched at 1.0, measured and tie-break-pinned; the h2h measures single-system fusion vs
# 3-system app-side assembly at identical semantics). The gate's examined>0 seqscan guard is
# fed by the backend-local graph visit counter delta (gph_visits) — >0 proves the native AM
# actually traversed; the operator's SM-3 counter does not exist on this shape.
# ======================================================================================
def emit_tridb_sql(
    cfg: WCfg,
    emb: np.ndarray,
    queries: list[dict],
    grid: list[tuple[int, int, int]],
    *,
    k: int,
    runs: int,
    hops: int = 2,
    edge_type_map: dict[str, int] | None = None,
) -> str:
    """Per query: warm-up (graded ids + gph_visits delta) then `runs` timed repeats.

    `edge_type_map` maps 'P<m>' -> the engine's dictionary edge-type id (from the engine
    load manifest's #WDL ETYPE lines); a query property missing from it is a slice/engine
    mismatch and fails loudly. `grid` is accepted for CLI-shape compatibility but the fused
    statement's only knob is `hops` (one combo: 'fusedh<hops>')."""
    if edge_type_map is None:
        raise SystemExit(
            "emit_tridb_sql: edge_type_map required (engine_load_manifest.json edge_type_map)"
        )
    out: list[str] = []
    w = out.append
    w("\\set ON_ERROR_STOP on")
    w("\\pset pager off")
    w("SET enable_seqscan = off;  -- wiki_h2h convention: force index paths")
    w(
        "SET graph_store.assume_dense_open = on;  -- advisor 048 O(1) vertex locate; the"
        " loader's dense-in-order load satisfies its precondition and every lookup is"
        " hard-verified (ERROR on violation, never silent). Disclosed in the report."
    )
    w("\\timing off")
    tag = f"fusedh{hops}"
    for qi, qy in enumerate(queries):
        pkey = f"P{qy['p']}"
        if pkey not in edge_type_map:
            raise SystemExit(
                f"emit_tridb_sql: property {pkey} not in engine edge_type_map — "
                "slice/engine mismatch (reload or re-sample)"
            )
        type_id = int(edge_type_map[pkey])
        if cfg.engine_dialect == "stock":
            # pgvector literal (square brackets); the unknown-typed quoted literal
            # coerces to vector against the vector <-> operator.
            qv = "[" + ",".join(repr(float(x)) for x in emb[qy["x"]]) + "]"
        else:
            qv = _vec_lit(emb[qy["x"]])
        # Fused filter-first statement: native typed BFS (C) seeds the small candidate set,
        # the relational P31 filter prunes it, the exact vector distance ranks the survivors.
        # `e.id <> x` mirrors the oracle's typed_reach, which never emits the anchor itself.
        # NB signature is gph_traverse_bfs(seed, max_depth, type_id) — graph_am.c:1705.
        call = (
            f"SELECT e.id FROM graph_store.gph_traverse_bfs({qy['x']}, {hops}, {type_id}) "
            f"AS t(dst) JOIN {cfg.engine_table} e ON e.id = t.dst "
            f"WHERE e.P31 @> ARRAY[{qy['t']}] AND e.id <> {qy['x']} "
            f"ORDER BY e.embedding <-> '{qv}', e.id LIMIT {k}"
        )
        w(f"\\echo #WD TRIDB qid={qi} combo={tag}")
        w("SELECT graph_store.gph_visits() AS wdv0 \\gset")
        w(f"\\echo #WD IDS qid={qi} combo={tag}")
        w(call + ";")
        w(f"\\echo #WD ENDIDS qid={qi} combo={tag}")
        w(
            f"SELECT '#WD EXAMINED qid={qi} combo={tag} examined=' || "
            f"(graph_store.gph_visits() - :wdv0) || ' bridges=0' AS line;"
        )
        w("\\timing on")
        for _ in range(runs):
            w(call + ";")
        w("\\timing off")
    w("\\echo #WD DONE")
    return "\n".join(out) + "\n"


# ======================================================================================
# BASELINE SIDE — live Neo4j typed traversal -> pg type-filter -> pg rerank, app-side, warm.
# ======================================================================================
def _connect_baseline(cfg: WCfg):
    """Neo4j + Postgres only — Milvus is NOT in the KBQA loop, and that is a fairness
    choice, not an omission: the anchor X is GIVEN (no ANN seeding step exists in this
    query shape), and the rank leg mirrors wiki_h2h's default use_pg_rerank convention
    (exact pgvector rerank over the small surviving candidate set — no ANN index needed).
    Charging the baseline a gratuitous Milvus round-trip would fabricate latency; omitting
    it FAVORS the baseline. Live-store imports are lazy so the module stays host-importable."""
    from neo4j import GraphDatabase
    import psycopg

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
    return driver, pg


def run_baseline(
    cfg: WCfg,
    emb: np.ndarray,
    queries: list[dict],
    grid: list[tuple[int, int]],
    *,
    k: int,
    runs: int,
    use_pg_rerank: bool = True,
) -> dict[str, dict]:
    """Sweep (hops, frontier) live across the stores the KBQA shape needs. grid is
    [(hops, frontier), ...] (WD_BASELINE_GRID overrides); returns {"h{hops}f{frontier}":
    {qi: {"ids", "median_ms", ...legs}}} — the same per-(combo,query) shape wiki_h2h's
    baseline emits, so grade_baseline consumes it unchanged.

    KNOBS. There is no Milvus ef/seeds here; the recall/latency curve comes from (a) hops
    swept UNDER/AT the oracle's hops (cheap low-recall points) and (b) a frontier cap
    (Cypher LIMIT on the DISTINCT typed reach — the traversal-work analogue of ef).

    TIMING. The full app-side assembly is timed per query — the per-store round-trips ARE
    the point: (i) Neo4j P-typed out-traversal from X, (ii) pg filter of the candidates to
    P31 ∋ T, (iii) rank of the survivors by cosine to X's embedding (pg `<=>` rerank by
    default; --no-pg-rerank = host numpy rerank, wiki_h2h's exact convention). Warm-up run
    excluded; `runs` timed repeats, median-of-runs (the graded ids come from the sweep's
    deterministic result, mirroring wiki_h2h). The query vector emb[x] is host-side on BOTH
    sides (wiki_h2h's fairness choice — the anchor vector is free for TriDB too, inlined in
    the emitted SQL)."""
    driver, pg = _connect_baseline(cfg)
    cur = pg.cursor()

    def neo4j_traverse(x: int, p: int, hops: int, frontier: int) -> list[int]:
        # Loader contract (mirror wiki_h2h): Entity.id is a STRING property (an int
        # `a.id IN $ids` silently matches NOTHING), edges are property-typed relationships
        # [:P<n>] in the natural subject->object direction (ADR-0016/ADR-0018 out-only).
        # `b <> a` mirrors typed_reach's exclude-src contract (and the engine BFS): a cycle
        # back to the anchor on a symmetric property must not return the anchor itself.
        cy = (
            f"MATCH (a:{cfg.neo4j_node_label})-[:P{p}*1..{hops}]->"
            f"(b:{cfg.neo4j_node_label}) WHERE a.id IN $ids AND b <> a "
            f"RETURN DISTINCT b.id AS id ORDER BY b.id LIMIT {frontier}"
        )
        with driver.session() as s:
            rows = s.run(cy, ids=[str(x)])
            return [int(r["id"]) for r in rows]

    def pg_type_filter(cand: list[int], t: int) -> list[int]:
        cur.execute(
            f"SELECT id FROM {cfg.pg_table} WHERE id = ANY(%s) "
            f"AND p31 @> ARRAY[%s]::bigint[]",
            (cand, t),
        )
        return [int(r[0]) for r in cur.fetchall()]

    def pg_rerank(qv, cand: list[int], k: int) -> list[int]:
        # exact rerank over the small surviving set ('<=>' cosine distance), as wiki_h2h.
        lit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
        cur.execute(
            f"SELECT id FROM {cfg.pg_table} WHERE id = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector LIMIT %s",
            (cand, lit, k),
        )
        return [int(r[0]) for r in cur.fetchall()]

    out: dict[str, dict] = {}
    for hops, frontier in grid:
        tag = f"h{hops}f{frontier}"
        per: dict[int, dict] = {}
        for qi, qy in enumerate(queries):
            qv = emb[qy["x"]]

            def one():
                t0 = time.perf_counter()
                cand = neo4j_traverse(qy["x"], qy["p"], hops, frontier)
                t1 = time.perf_counter()
                surv = pg_type_filter(cand, qy["t"]) if cand else []
                t2 = time.perf_counter()
                if not surv:
                    top: list[int] = []
                elif use_pg_rerank:
                    top = pg_rerank(qv, surv, k)
                else:
                    arr = np.fromiter(surv, dtype=np.int64, count=len(surv))
                    top = [int(x) for x in arr[np.argsort(-(emb[arr] @ qv))][:k]]
                t3 = time.perf_counter()
                return top, (
                    (t1 - t0) * 1e3,
                    (t2 - t1) * 1e3,
                    (t3 - t2) * 1e3,
                )

            # warm-up call grades the ids (excluded from timing), mirroring the TriDB side;
            # the timed repeats below capture ONLY latency and must not overwrite graded_top.
            graded_top, _ = one()
            times, legs_last = [], None
            for _ in range(runs):
                _, legs = one()
                times.append(sum(legs))
                legs_last = legs
            per[qi] = {
                "ids": graded_top,
                "median_ms": float(statistics.median(times)),
                "neo4j_ms": legs_last[0],
                "pg_filter_ms": legs_last[1],
                "rank_ms": legs_last[2],
            }
        out[tag] = per
    cur.close()
    pg.close()
    driver.close()
    return out


# ======================================================================================
# GRADE — raw transcript + baseline JSON -> the graded curves JSON `report` consumes.
# Curves ONLY: recall vs the oracle, median-of-runs latency, median examined. No headline
# math here — `report` (via the reused publication_gate) is the only place a ratio exists.
# ======================================================================================
_WD_IDS = re.compile(r"#WD IDS qid=(\d+) combo=(\S+)")
_WD_EX = re.compile(r"#WD EXAMINED qid=(\d+) combo=(\S+) examined=(\d+) bridges=(\d+)")


def parse_tridb(raw: str) -> dict[tuple[int, str], dict]:
    """Mirror of wiki_h2h.parse_tridb for the #WD marker format emit_tridb_sql emits:
    #WD IDS / #WD ENDIDS bracket the graded id rows of the warm-up call, #WD EXAMINED
    carries the SM-3 counters, and the psql `Time:` lines after it are the timed repeats
    (median-of-runs downstream). Refuses an incomplete transcript (no '#WD DONE')."""
    if "#WD DONE" not in raw:
        raise SystemExit("TriDB transcript did not reach '#WD DONE' — incomplete")
    res: dict[tuple[int, str], dict] = {}
    cur: tuple[int, str] | None = None
    in_ids = False
    for line in raw.splitlines():
        mi = _WD_IDS.search(line)
        if mi:
            cur = (int(mi[1]), mi[2])
            res.setdefault(
                cur, {"ids": [], "times": [], "examined": None, "bridges": None}
            )
            in_ids = True
            continue
        if line.startswith("\\echo") or "#WD ENDIDS" in line:
            in_ids = False
        me = _WD_EX.search(line)
        if me:
            key = (int(me[1]), me[2])
            d = res.setdefault(
                key, {"ids": [], "times": [], "examined": None, "bridges": None}
            )
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


def oracle_meta_from_env(meta: dict, cfg: WCfg) -> dict:
    """The oracle_meta block publication_gate requires (same WH_* env names as wiki_h2h
    and the spike report's reproducibility pins, so one declaration serves both harnesses).

    Honesty defaults: engine_edges / HNSW build health have NO default — undeclared keeps
    the graph-set / build-health blockers up until someone measures and declares them.
    neo4j_edges defaults to the oracle's induced edge count (the count Neo4j MUST hold if
    the loader staged the same slice). tjs_max_examined is the disclosed TR-1 cap: a combo
    whose median examined reaches it is a CENSORED point the gate excludes (mechanism
    reused verbatim from wiki_h2h — grade only carries the numbers through)."""
    induced = meta.get("induced_edges")
    return {
        "k": meta.get("k"),
        "hops": meta.get("hops"),
        "engine_edges": os.environ.get("WH_ENGINE_EDGES"),
        "neo4j_edges": os.environ.get(
            "WH_NEO4J_EDGES", str(induced) if induced is not None else None
        ),
        "tjs_max_examined": cfg.tjs_max_examined,
        "hnsw_healthy_builds": os.environ.get("WH_HNSW_HEALTHY_BUILDS"),
        "hnsw_total_builds": os.environ.get("WH_HNSW_TOTAL_BUILDS"),
    }


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
        "# TriDB Wikidata h2h — fused filter-first vs multi-store (matched)",
        "",
    ]
    L.append(
        "> COMPUTE-BOUND regime (RAM-resident); value = fusion speed + one-WAL consistency. "
        "Latency/pages reported ONLY at matched recall. Seedless/vector-first mode blocked on 043. "
        "TriDB side = ONE fused statement (native typed BFS -> relational filter -> exact vector "
        "rank; `graph_store.assume_dense_open=on`, disclosed) — tjs_open's typed-traversal "
        "integration is the plan 038 residual, not part of this claim."
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
    L.append(
        f"**Matched recall** (target {target:.2f}): TriDB {tp[0]} at "
        f"recall {tp[1]['recall_at_k']:.3f}, baseline {bp[0]} at "
        f"recall {bp[1]['recall_at_k']:.3f}:"
    )
    L.append("")
    L.append(f"- TriDB fused filter-first statement: {t_lat:.2f} ms")
    L.append(f"- multi-store (Milvus+Neo4j+pg): {b_lat:.2f} ms")
    L.append(f"- **speedup: {b_lat / t_lat:.2f}×**")
    return "\n".join(L) + "\n", blockers


# ======================================================================================
# CLI
# ======================================================================================
DEFAULT_GRID = [(8, 1, 64), (16, 2, 128), (32, 2, 256)]
# baseline knobs (hops, frontier): hops swept under/at the oracle's default hops=2 (the cheap
# low-recall end of the curve), frontier = Cypher LIMIT on the DISTINCT typed reach (the
# traversal-work analogue of Milvus ef). Same 4-point curve size as wiki_h2h's baseline grid.
DEFAULT_BASELINE_GRID = [(1, 64), (1, 256), (2, 1024), (2, 4096)]


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
    for name in ("oracle", "tridb-emit", "baseline", "grade", "report"):
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
        p.add_argument(
            "--baseline",
            type=Path,
            default=Path("bench/results/wikidata_h2h_baseline.json"),
        )
        p.add_argument("--no-pg-rerank", action="store_true")
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
        eng_manifest_path = Path(
            os.environ.get(
                "WD_ENGINE_MANIFEST", cfg.slice_dir / "engine_load_manifest.json"
            )
        )
        etype_map = json.loads(eng_manifest_path.read_text())["engine"]["edge_type_map"]
        sql = emit_tridb_sql(
            cfg,
            emb,
            queries,
            DEFAULT_GRID,
            k=meta["k"],
            runs=args.runs,
            hops=int(meta.get("hops", args.hops)),
            edge_type_map=etype_map,
        )
        out = args.out or Path("/tmp/wikidata_h2h_tridb.sql")
        out.write_text(sql)
        print(
            f"[wikidata_h2h tridb-emit] {len(queries)} queries x {len(DEFAULT_GRID)} combos "
            f"-> {out} (GX10: docker exec -i {cfg.engine_container} psql -f - < {out})"
        )
        return 0

    if args.cmd == "baseline":
        meta = json.loads(args.oracle.read_text())
        queries = meta["queries"]
        emb = load_emb(cfg, meta["n"])
        grid = _grid_env("WD_BASELINE_GRID", DEFAULT_BASELINE_GRID)
        res = run_baseline(
            cfg,
            emb,
            queries,
            grid,
            k=meta["k"],
            runs=args.runs,
            use_pg_rerank=not args.no_pg_rerank,
        )
        out = args.out or args.baseline
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {str(t): {str(q): d for q, d in per.items()} for t, per in res.items()},
                indent=2,
            )
        )
        print(
            f"[wikidata_h2h baseline] {len(queries)} queries x {len(res)} combos -> {out}"
        )
        return 0

    if args.cmd == "grade":
        # raw -> curves ONLY (recall vs the oracle, median-of-runs latency, median examined);
        # the honesty gate and any ratio live in `report`, unchanged.
        meta = json.loads(args.oracle.read_text())
        oracle = meta["oracle"]
        k = meta["k"]
        tridb = (
            grade_tridb(parse_tridb(args.tridb_raw.read_text()), oracle, k)
            if args.tridb_raw
            else {}
        )
        baseline: dict[str, dict] = {}
        if args.baseline and args.baseline.exists():
            braw = json.loads(args.baseline.read_text())
            baseline = grade_baseline(
                {t: {int(q): d for q, d in per.items()} for t, per in braw.items()},
                oracle,
                k,
            )
        graded = {
            "tridb": tridb,
            "baseline": baseline,
            "oracle_meta": oracle_meta_from_env(meta, cfg),
        }
        out = args.out or Path("bench/results/wikidata_h2h_graded.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(graded, indent=2))
        print(
            f"[wikidata_h2h grade] tridb {len(tridb)} combos, baseline {len(baseline)} "
            f"combos -> {out} (feed to: report --oracle {out})"
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

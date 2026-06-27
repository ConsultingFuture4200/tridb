"""Drive the canonical-query benchmark over a REAL embedding dataset (DEV-1284).

WHY THIS EXISTS
---------------
tools/bench_corpus.py builds a SYNTHETIC corpus: gaussian unit vectors. That is
enough to exercise the live engine's plumbing (insert -> HNSW -> tjs -> oracle),
but a recall / SM-4 number measured on random gaussians is not a credible
correctness claim — random vectors have no topical structure, so "is the early
-terminating tjs scan finding the true nearest reachable dst?" is being asked of
a degenerate distribution. This module loads a REAL embedding dataset from disk
(the kind ann-benchmarks / SIFT / a real corpus of sentence embeddings ships)
and produces the SAME manifest + the SAME `#BENCH ...` SQL that bench_corpus.py
emits, so it is a TRUE DROP-IN for the existing downstream consumer
(scripts/bench_live.sh -> bench/live_report.py). A live GX10 run would consume a
real-dataset corpus identically to a synthetic one.

WHAT IS MEASURABLE TODAY vs GATED
---------------------------------
The defining-feature number — recall@k / SM-4, "does the engine return the true
top-k reachable+filtered dst?" — is computable RIGHT NOW, on this x86 box, with
NO engine: it is an exact numpy top-k oracle over the loaded vectors (see
:func:`exact_oracle`). So real-dataset correctness is measurable here today.

LATENCY (SM-2) and the live tjs_candidates_examined() (SM-3) are LIVE-ENGINE
measurements and stay GX10/engine-gated. This module never produces a latency
number and never claims one; the SQL it emits carries the EXPLAIN ANALYZE
scaffolding for the on-target run exactly as bench_corpus.py does, marked the
same way.

DROP-IN CONTRACT
----------------
The manifest dict and the `#BENCH`-style SQL are produced by reusing
tools/bench_corpus.py's own emitter (:func:`tools.bench_corpus.build_sql`), so
the format cannot drift between the two paths. The ONE difference
from the synthetic manifest: real embeddings cannot be regenerated from a seed,
so the manifest carries the entity rows in a private "_entities" field (the same
convention tools/bench_corpus_shared.py already uses) plus a precomputed
"oracle" map, so a recall report needs only the manifest + a results file — no
RNG replay of vectors that do not come from an RNG.

Deterministic: a single --seed drives the graph synthesis (hub centroid choice,
fanout sampling, query jitter, ts assignment); the real vectors themselves are
fixed by the input file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# Reuse the canonical SQL emitter so the #BENCH format CANNOT drift from the
# synthetic path (golden rule: one canonical query, one surface).
from tools.bench_corpus import build_sql

# Default ts domain — matches tools/bench_corpus.py so a real-dataset run plugs
# into the same window/selectivity assumptions the live SQL + report expect.
DEFAULT_TIME_MIN = 19000
DEFAULT_TIME_MAX = 20000


# --------------------------------------------------------------------------- #
# Loaders — real embedding vectors from disk -> float64 ndarray (n, dim)
# --------------------------------------------------------------------------- #


def load_npy(path: Path) -> np.ndarray:
    """Load a numpy `.npy` array of shape (n, dim). Cast to float64."""
    arr = np.load(path)
    if arr.ndim != 2:
        raise ValueError(
            f"{path}: expected a 2-D (n, dim) array, got shape {arr.shape}"
        )
    return np.ascontiguousarray(arr, dtype=np.float64)


def load_fvecs(path: Path) -> np.ndarray:
    """Load a SIFT-style `.fvecs` file -> float64 (n, dim).

    `.fvecs` layout: each vector is a little-endian int32 `dim` followed by `dim`
    little-endian float32 components. All rows share one dim (the standard SIFT/
    GIST/Deep1B export format).
    """
    raw = np.fromfile(path, dtype=np.int32)
    if raw.size == 0:
        raise ValueError(f"{path}: empty .fvecs file")
    dim = int(raw[0])
    if dim <= 0:
        raise ValueError(f"{path}: invalid leading dim {dim}")
    row_int32 = dim + 1  # the dim header + dim float32 components
    if raw.size % row_int32 != 0:
        raise ValueError(
            f"{path}: size {raw.size} not a multiple of row stride {row_int32} "
            f"(dim={dim}) — not a well-formed .fvecs file"
        )
    rows = raw.reshape(-1, row_int32)
    # the leading int32 dim must be identical on every row
    if not np.all(rows[:, 0] == dim):
        raise ValueError(f"{path}: rows have inconsistent dim headers")
    floats = rows[:, 1:].view(np.float32)
    return np.ascontiguousarray(floats, dtype=np.float64)


def load_ivecs(path: Path) -> np.ndarray:
    """Load a SIFT-style `.ivecs` file -> float64 (n, dim).

    Same framing as `.fvecs` but the payload is int32 (used for ground-truth
    neighbour-id files). Returned as float64 for a uniform loader signature; cast
    back to int if you need ids.
    """
    raw = np.fromfile(path, dtype=np.int32)
    if raw.size == 0:
        raise ValueError(f"{path}: empty .ivecs file")
    dim = int(raw[0])
    row_int32 = dim + 1
    if raw.size % row_int32 != 0:
        raise ValueError(f"{path}: size not a multiple of row stride (dim={dim})")
    rows = raw.reshape(-1, row_int32)
    return np.ascontiguousarray(rows[:, 1:], dtype=np.float64)


def load_hdf5(path: Path, dataset: str = "train") -> np.ndarray:
    """Load an ann-benchmarks `.hdf5` dataset (the `train` matrix by default).

    h5py is imported LAZILY and is NOT a hard dependency (it is intentionally not
    in requirements.txt — most dev boxes never touch hdf5). If it is missing we
    degrade gracefully with an actionable message rather than crashing on import.
    """
    try:
        import h5py  # noqa: PLC0415 (deliberate lazy import — soft dep)
    except ImportError as exc:  # pragma: no cover - exercised only without h5py
        raise RuntimeError(
            f"reading {path} needs h5py, which is not installed. It is an "
            f"OPTIONAL dependency (ann-benchmarks .hdf5 only). Install it just "
            f"for this run with `pip install h5py`, or convert the dataset to "
            f".npy / .fvecs and use those loaders."
        ) from exc
    with h5py.File(path, "r") as f:
        if dataset not in f:
            avail = ", ".join(f.keys())
            raise ValueError(
                f"{path}: dataset '{dataset}' not found (available: {avail})"
            )
        arr = np.asarray(f[dataset])
    if arr.ndim != 2:
        raise ValueError(f"{path}: dataset '{dataset}' is not 2-D ({arr.shape})")
    return np.ascontiguousarray(arr, dtype=np.float64)


def load_vectors(path: Path, hdf5_dataset: str = "train") -> np.ndarray:
    """Dispatch on file extension -> float64 (n, dim).

    Supported: `.npy`, `.fvecs`, `.ivecs`, `.hdf5`/`.h5` (lazy h5py).
    """
    suffix = path.suffix.lower()
    if suffix == ".npy":
        return load_npy(path)
    if suffix == ".fvecs":
        return load_fvecs(path)
    if suffix == ".ivecs":
        return load_ivecs(path)
    if suffix in (".hdf5", ".h5"):
        return load_hdf5(path, dataset=hdf5_dataset)
    raise ValueError(
        f"{path}: unsupported extension '{suffix}' "
        f"(expected .npy, .fvecs, .ivecs, or .hdf5)"
    )


# --------------------------------------------------------------------------- #
# Topical graph synthesis over the REAL vectors
# --------------------------------------------------------------------------- #


def _l2_sq_rows(emb: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Squared L2 of every row of `emb` to vector `q` (monotone with L2, no sqrt
    — matches the canonical `<->` ordering used by tjs / the oracle SQL)."""
    diff = emb - q
    return np.einsum("ij,ij->i", diff, diff)


def synthesize_corpus(
    emb: np.ndarray,
    *,
    hubs: int,
    fanout: int,
    queries: int,
    k: int,
    window: int,
    time_min: int = DEFAULT_TIME_MIN,
    time_max: int = DEFAULT_TIME_MAX,
    query_jitter: float = 0.35,
    seed: int = 42,
) -> dict:
    """Build a topical graph + query set over the REAL embeddings.

    MIRRORS tools/bench_corpus.py:build() so the recall measurement is meaningful
    rather than a sparse-graph artifact. Each hub is an actual REAL entity; its
    centroid is that entity's own embedding (the synthetic path draws a random
    centroid — here the real vector IS the topical anchor). Its `fanout` graph
    neighbours are sampled from the entities NEAREST that centroid, so the
    qualifying (reachable + time-filtered) dst are DENSE in the similarity
    stream. That is exactly the locality the live tjs early-termination
    (consecutive_drops bound, ADR-0007) needs to reach the answers before firing
    — so SM-4 measures real recall on a realistic corpus, not a pathological case
    where answers are scattered uniformly through the dataset.

    The query vector for a hub is the hub centroid + small gaussian jitter (a user
    asking about the hub's topic), with a contiguous ts window selective enough to
    drop a real fraction of the neighbourhood yet leave >= k qualifying answers.

    Returns a manifest dict whose PUBLIC fields are identical in name + meaning to
    tools/bench_corpus.py's manifest, plus the private "_entities"/"oracle"
    carriers a real-dataset run needs (see module docstring).
    """
    rng = np.random.default_rng(seed)
    n, dim = emb.shape
    if hubs > n:
        raise ValueError(f"hubs ({hubs}) cannot exceed entities ({n})")

    # Timestamps: same uniform draw as the synthetic path (entity ts in [min,max]).
    ts = rng.integers(time_min, time_max + 1, size=n)

    # Hubs are the first `hubs` entity ids (0..hubs-1), matching bench_corpus.py's
    # "hub vertex ids are also entity ids" convention. The hub's centroid is its
    # OWN real embedding — the topical anchor for its neighbourhood.
    hub_ids = list(range(hubs))
    edges: list[tuple[int, int]] = []
    hub_dsts: dict[int, list[int]] = {}
    hub_centroids: dict[int, np.ndarray] = {}
    for h in hub_ids:
        centroid = emb[h]
        hub_centroids[h] = centroid
        # rank all entities by closeness to this hub's centroid; take the nearest
        # pool, then sample `fanout` from it (pool > fanout so neighbourhoods
        # overlap but differ) — identical strategy to bench_corpus.py.
        d2 = _l2_sq_rows(emb, centroid)
        pool = np.argsort(d2)[: max(fanout * 3, fanout + 1)]
        dsts = rng.choice(pool, size=min(fanout, len(pool)), replace=False)
        dsts = [int(d) for d in dsts if int(d) != h]
        hub_dsts[h] = dsts
        for d in dsts:
            edges.append((h, d))

    # Queries: pin a hub, draw the query vector near its centroid + jitter, choose
    # a contiguous ts window. Jitter is scaled by the per-dataset vector norm scale
    # so the same `query_jitter` is comparable across datasets of different
    # magnitudes (real embeddings are not unit-normalized like the synthetic ones).
    norm_scale = float(np.mean(np.linalg.norm(emb, axis=1))) or 1.0
    query_list = []
    for qid in range(queries):
        h = hub_ids[qid % len(hub_ids)]
        centroid = hub_centroids[h]
        jitter = rng.standard_normal(dim) * (query_jitter * norm_scale)
        qv = centroid + jitter
        start = int(rng.integers(time_min, time_max - window + 2))
        win = list(range(start, start + window))
        query_list.append(
            {"qid": qid, "src": h, "embedding": qv.tolist(), "window": win}
        )

    # Exact ground-truth oracle per query (numpy, no engine).
    oracle = {
        str(q["qid"]): exact_oracle(
            emb=emb,
            ts=ts,
            hub_dsts=hub_dsts,
            src=int(q["src"]),
            query_vec=np.asarray(q["embedding"], dtype=np.float64),
            window=q["window"],
            k=k,
        )
        for q in query_list
    }

    manifest = {
        # --- PUBLIC fields: same names/meaning as tools/bench_corpus.py -------
        "entities": n,
        "dim": dim,
        "hubs": hubs,
        "fanout": fanout,
        "num_queries": len(query_list),
        "k": k,
        "edges": len(edges),
        "seed": seed,
        "time_min": time_min,
        "time_max": time_max,
        "window": window,
        "queries": query_list,
        "hub_dsts": {str(h): hub_dsts[h] for h in hub_ids},
        # --- REAL-DATASET carriers (private "_" / oracle) --------------------
        # Real embeddings cannot be RNG-regenerated from a seed (unlike the
        # synthetic path), so we carry the entity rows the SQL inserts AND the
        # exact oracle the recall report grades against. Same "_entities"
        # convention as tools/bench_corpus_shared.py.
        "source": "real-dataset",
        "_entities": [(i, int(ts[i]), emb[i].tolist()) for i in range(n)],
        "_edges": edges,
        "oracle": oracle,
    }
    return manifest


# --------------------------------------------------------------------------- #
# Exact ground-truth oracle (numpy) — the SM-4 / recall reference, no engine
# --------------------------------------------------------------------------- #


def exact_oracle(
    *,
    emb: np.ndarray,
    ts: np.ndarray,
    hub_dsts: dict[int, list[int]],
    src: int,
    query_vec: np.ndarray,
    window: list[int],
    k: int,
) -> list[int]:
    """Exact top-k canonical-query answer for one query, computed in numpy.

    Mirrors the in-DB oracle (tools/bench_corpus.py PHASE A) WITHOUT the engine:
    the dst reachable from `src`, passing the timestamp filter, ordered by TRUE L2
    to the query vector, top-k. Ties are broken by ascending id — identical to the
    SQL oracle's `ORDER BY d2, id`, so the two agree id-for-id.

    This is the SM-4 parity / recall reference that makes real-dataset
    correctness measurable TODAY on a non-GX10 box.
    """
    window_set = set(window)
    reachable = hub_dsts.get(src, [])
    # reachable dst that pass the ts filter
    candidates = [d for d in reachable if int(ts[d]) in window_set]
    if not candidates:
        return []
    cand = np.asarray(candidates, dtype=np.int64)
    d2 = _l2_sq_rows(emb[cand], query_vec)
    # stable sort by (d2, id): lexsort with id as the primary tiebreak key second
    order = np.lexsort((cand, d2))
    return [int(cand[i]) for i in order[:k]]


# --------------------------------------------------------------------------- #
# Recall@k utility (oracle ids vs returned ids) — usable by the live report
# --------------------------------------------------------------------------- #


def recall_at_k(returned: list[int], oracle: list[int], k: int | None = None) -> float:
    """recall@k = |returned ∩ oracle_top_k| / |oracle_top_k|.

    Set-based (order-insensitive), matching how bench.metrics scores SM-4 parity
    (Jaccard there; recall here is the asymmetric "did we find the truth" view the
    live report wants per query). An empty oracle (no qualifying dst) is recall 1.0
    ONLY if nothing was returned — a false positive against an empty truth scores
    0.0 (mirrors tools/sweep_corpus._recall, the shared semantics).
    """
    truth = oracle if k is None else oracle[:k]
    got = set(returned if k is None else returned[:k])
    if not truth:
        return 1.0 if not got else 0.0  # empty truth: perfect only if nothing returned
    return len(got & set(truth)) / len(truth)


def report_recall(manifest: dict, results: dict[int, list[int]] | None) -> dict:
    """Aggregate recall@k over all queries from a manifest's oracle.

    `results` maps qid -> returned ids (e.g. parsed from a live #BENCH
    TRIDB_RESULT transcript). In PURE-ORACLE mode (`results is None`) every query
    is graded against ITSELF (results == oracle), which yields recall 1.0 and is
    the sanity baseline proving the oracle is internally consistent / the dataset
    plumbing is wired — NOT an engine claim.

    Returns {"k", "num_queries", "mean_recall", "per_query": {qid: recall}}.
    """
    k = manifest["k"]
    oracle = manifest.get("oracle")
    if oracle is None:
        raise ValueError(
            "manifest has no 'oracle' field — regenerate it with tools.real_corpus "
            "(the synthetic tools/bench_corpus.py manifest omits the oracle)"
        )
    per_query: dict[int, float] = {}
    for q in manifest["queries"]:
        qid = int(q["qid"])
        truth = [int(x) for x in oracle[str(qid)]]
        if results is None:
            returned = truth  # pure-oracle self-check
        else:
            returned = [int(x) for x in results.get(qid, [])]
        per_query[qid] = recall_at_k(returned, truth, k)
    mean_recall = sum(per_query.values()) / len(per_query) if per_query else 0.0
    return {
        "k": k,
        "num_queries": len(per_query),
        "mean_recall": mean_recall,
        "per_query": per_query,
    }


# --------------------------------------------------------------------------- #
# SQL + manifest emission (drop-in for the synthetic path)
# --------------------------------------------------------------------------- #


def emit(manifest: dict) -> str:
    """Emit the SAME `#BENCH`-style SQL tools/bench_corpus.py emits, for this
    real-dataset manifest.

    Delegates to tools.bench_corpus.build_sql so the format is, by construction,
    identical to the synthetic path — a live GX10 run consumes it the same way.
    The entity rows come from the manifest's "_entities" carrier (real vectors,
    not RNG-regenerated).
    """
    entities = [(i, ts, emb) for (i, ts, emb) in manifest["_entities"]]
    edges = [(int(s), int(d)) for (s, d) in manifest["_edges"]]
    return build_sql(
        manifest=manifest,
        entities=entities,
        edges=edges,
        source="tools/real_corpus.py",
    )


def public_manifest(manifest: dict) -> dict:
    """The manifest as written to disk: keep the public fields + the real-dataset
    carriers (_entities / _edges / oracle) the recall report and SQL emitter need.

    Unlike bench_corpus_shared (which DROPS the private arrays because the live
    side RNG-regenerates them), the real-dataset manifest MUST keep them — there
    is no seed that reproduces real embeddings. We do drop nothing here; the
    function exists as the single documented place that decides what ships.
    """
    return manifest


# --------------------------------------------------------------------------- #
# Results-file parsing for --report-recall
# --------------------------------------------------------------------------- #


def parse_results_file(path: Path) -> dict[int, list[int]]:
    """Parse a results file into qid -> returned ids.

    Accepts either:
      * a JSON object {"<qid>": [id, id, ...], ...}, or
      * a live #BENCH transcript containing `#BENCH TRIDB_RESULT qid=N ids=a,b,c`
        lines (the same format bench/live_report.py scrapes).
    """
    text = path.read_text()
    stripped = text.lstrip()
    if stripped.startswith("{"):
        raw = json.loads(text)
        return {int(qid): [int(x) for x in ids] for qid, ids in raw.items()}
    # fall back to the #BENCH transcript format
    import re  # noqa: PLC0415 (local: only this branch needs it)

    pat = re.compile(r"#BENCH TRIDB_RESULT qid=(\d+) ids=([\d,]*)")
    out: dict[int, list[int]] = {}
    for line in text.splitlines():
        m = pat.search(line)
        if m:
            ids = [int(x) for x in m.group(2).split(",") if x != ""]
            out[int(m.group(1))] = ids
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _write_manifest(path: Path, manifest: dict) -> None:
    # tuples in _entities/_edges serialize to JSON lists; round-trip is fine.
    path.write_text(json.dumps(public_manifest(manifest)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--vectors",
        type=Path,
        help="real embedding file: .npy / .fvecs / .ivecs / .hdf5",
    )
    p.add_argument(
        "--hdf5-dataset",
        default="train",
        help="dataset name inside a .hdf5 file (ann-benchmarks: 'train')",
    )
    p.add_argument("--queries", type=int, default=12)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--hubs", type=int, default=12)
    p.add_argument("--fanout", type=int, default=150)
    p.add_argument("--window", type=int, default=600)
    p.add_argument("--time-min", type=int, default=DEFAULT_TIME_MIN)
    p.add_argument("--time-max", type=int, default=DEFAULT_TIME_MAX)
    p.add_argument(
        "--query-jitter",
        type=float,
        default=0.35,
        help="stddev (x dataset norm scale) of the noise added to a hub centroid "
        "to form its query vector",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sql-out", type=Path)
    p.add_argument("--manifest-out", type=Path)
    p.add_argument(
        "--report-recall",
        action="store_true",
        help="report recall@k from a manifest's oracle (today, no engine). With "
        "--results FILE grades returned ids; without it, pure-oracle self-check.",
    )
    p.add_argument(
        "--manifest",
        type=Path,
        help="existing manifest (for --report-recall without regenerating)",
    )
    p.add_argument(
        "--results",
        type=Path,
        help="results file (JSON qid->ids, or a #BENCH transcript) for --report-recall",
    )
    args = p.parse_args(argv)

    # --- recall report path (measurable TODAY, no engine) -------------------- #
    if args.report_recall:
        if args.manifest is not None:
            manifest = json.loads(args.manifest.read_text())
        elif args.manifest_out is not None and args.manifest_out.exists():
            manifest = json.loads(args.manifest_out.read_text())
        else:
            raise SystemExit(
                "--report-recall needs --manifest PATH (or a prior --manifest-out)"
            )
        results = parse_results_file(args.results) if args.results else None
        rep = report_recall(manifest, results)
        mode = "graded vs results" if results else "pure-oracle self-check"
        print(
            f"[real_corpus] recall@{rep['k']} ({mode}): mean "
            f"{rep['mean_recall']:.3f} over {rep['num_queries']} queries"
        )
        for qid in sorted(rep["per_query"]):
            print(f"[real_corpus]   qid={qid} recall={rep['per_query'][qid]:.3f}")
        if results is None:
            print(
                "[real_corpus] NOTE: pure-oracle mode grades the oracle against "
                "itself (== 1.0); it proves the dataset/oracle plumbing, NOT the "
                "engine. A real engine recall needs the live #BENCH transcript "
                "(GX10/engine-gated) passed via --results."
            )
        return 0

    # --- generate path ------------------------------------------------------- #
    if args.vectors is None:
        raise SystemExit("--vectors is required (unless using --report-recall)")
    if args.sql_out is None or args.manifest_out is None:
        raise SystemExit("--sql-out and --manifest-out are required when generating")

    emb = load_vectors(args.vectors, hdf5_dataset=args.hdf5_dataset)
    manifest = synthesize_corpus(
        emb,
        hubs=args.hubs,
        fanout=args.fanout,
        queries=args.queries,
        k=args.k,
        window=args.window,
        time_min=args.time_min,
        time_max=args.time_max,
        query_jitter=args.query_jitter,
        seed=args.seed,
    )
    sql = emit(manifest)
    args.sql_out.parent.mkdir(parents=True, exist_ok=True)
    args.sql_out.write_text(sql)
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    _write_manifest(args.manifest_out, manifest)

    print(
        f"[real_corpus] loaded {emb.shape[0]} real vectors (dim={emb.shape[1]}) "
        f"from {args.vectors}"
    )
    print(
        f"[real_corpus] wrote {args.sql_out} (hubs={args.hubs} fanout={args.fanout} "
        f"queries={args.queries} k={args.k}) + manifest {args.manifest_out} "
        f"(with exact oracle for recall@k today; live latency is GX10-gated)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

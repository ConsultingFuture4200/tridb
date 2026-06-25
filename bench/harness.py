"""TriDB benchmark harness (DEV-1172).

Drives the ONE canonical query (spec §5) on an identical corpus against BOTH:

  * TriDB, via :mod:`bench.driver` (deterministic stub off-target; GX10/engine-
    gated live driver on-target), and
  * the multi-system baseline (Neo4j + Milvus + Postgres merged app-side —
    ``baseline/harness.py``, AkasicDB Scenario 2).

and captures success metrics SM-1..SM-5 (see :mod:`bench.metrics`) per-query and
in aggregate into a :class:`bench.metrics.BenchmarkReport`.

Baseline measurement has two modes, mirroring the engine split:

  * In-process model (default, runs anywhere): replays the baseline's defining
    cost — full materialization of the graph/vector legs and an app-side merge —
    against the in-memory corpus, recording the large intermediate sets SM-1 is
    graded against. Same answer-set semantics as ``baseline/harness.py``'s
    ``merge()`` so SM-4 parity is meaningful.
  * Live model (``--baseline live``): drives the real ``baseline/harness.py``
    against the running docker-compose stack. Stack-gated (needs the three
    systems up); not exercised off-target.

The TriDB stub and the in-process baseline model are intentionally NOT the same
code path: the stub fuses + early-terminates (TR-1), the baseline materializes +
merges. They agree only on the final answer set — which is the whole point of
SM-4.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from bench.driver import Corpus, EngineDriver, make_driver
from bench.metrics import BenchmarkReport, QuerySample, derive_metrics

# Over-fetch multiplier the out-of-DB baseline pays on the ANN leg because it
# cannot push the graph/time predicates into the vector scan (mirrors
# baseline/harness.py vector_topk's `search_limit = k * 32`). This is a primary
# driver of the baseline's intermediate-result blowup that SM-1 measures.
BASELINE_ANN_FANOUT = 32


# --------------------------------------------------------------------------- #
# Corpus loading (same seed format as tools/seed_corpus.py)
# --------------------------------------------------------------------------- #


def load_corpus(seed_dir: Path) -> Corpus:
    """Read entities.csv / edges.csv / queries.jsonl into an in-memory Corpus."""
    entities: dict[int, dict] = {}
    with open(seed_dir / "entities.csv", newline="") as f:
        for r in csv.DictReader(f):
            emb = [float(x) for x in r["embedding"].strip("{}").split(",")]
            entities[int(r["id"])] = {
                "timestamp": int(r["timestamp"]),
                "chunk": r["chunk"],
                "embedding": emb,
            }

    edges: list[tuple[int, int]] = []
    with open(seed_dir / "edges.csv", newline="") as f:
        for r in csv.DictReader(f):
            edges.append((int(r["src"]), int(r["dst"])))

    queries: list[dict] = []
    with open(seed_dir / "queries.jsonl") as f:
        for raw in f:
            stripped = raw.strip()
            if stripped:
                queries.append(json.loads(stripped))

    return Corpus(entities=entities, edges=edges, queries=queries)


# --------------------------------------------------------------------------- #
# In-process baseline model (mirrors baseline/harness.py, runs anywhere)
# --------------------------------------------------------------------------- #


def _l2_sq(a: list[float], b: list[float]) -> float:
    return sum((x - y) * (x - y) for x, y in zip(a, b))


def baseline_query_inprocess(query: dict, k: int, corpus: Corpus) -> QuerySample:
    """Replay the baseline's materialize-transfer-prune cost in-process.

    Faithful to ``baseline/harness.py`` semantics:
      1. vector ANN top-(k*fanout) over ALL entities -> ranked srcs (over-fetch).
      2. graph 1-hop expansion from those srcs -> all (src,dst) pairs.
      3. relational time filter over the reached dsts.
      4. app-side merge: qualify pairs, order by src distance, dedup, take k.

    Records every intermediate set; ``peak_intermediate_rows`` is the largest of
    them (the merged candidate set, typically) — the SM-1 surface. The answer set
    matches the canonical query result so SM-4 parity is exact when both sides
    are correct.
    """
    t0 = time.perf_counter()
    qid = int(query["qid"])
    q_emb = query["embedding"]
    time_range = set(query["selected_time_range"])

    # 1) Vector ANN over the whole corpus, over-fetching like the real Milvus leg.
    ranked = sorted(
        corpus.entities.keys(),
        key=lambda eid: _l2_sq(corpus.entities[eid]["embedding"], q_emb),
    )
    search_limit = min(k * BASELINE_ANN_FANOUT, len(ranked))
    vector_hits = [
        (eid, _l2_sq(corpus.entities[eid]["embedding"], q_emb))
        for eid in ranked[:search_limit]
    ]
    src_dist = dict(vector_hits)
    seeds = set(src_dist)

    # 2) Graph 1-hop expansion from the seed srcs -> candidate pairs.
    pairs = [(s, d) for (s, d) in corpus.edges if s in seeds]

    # 3) Relational time filter on reached dsts.
    dst_ids = {d for _, d in pairs}
    kept_dst = {
        d: corpus.entities[d]["chunk"]
        for d in dst_ids
        if d in corpus.entities and corpus.entities[d]["timestamp"] in time_range
    }

    # 4) App-side merge (mirrors baseline/harness.py merge()).
    candidates = [
        (src_dist[s], d) for (s, d) in pairs if s in src_dist and d in kept_dst
    ]
    candidates.sort(key=lambda x: x[0])
    chunks: list[str] = []
    seen: set[int] = set()
    for _, d in candidates:
        if d not in seen:
            seen.add(d)
            chunks.append(kept_dst[d])
        if len(chunks) >= k:
            break

    latency_ms = (time.perf_counter() - t0) * 1000.0

    # Peak intermediate = the largest set materialized + shipped across a system
    # boundary. The baseline must hold the vector candidate set, the full graph
    # pair set, and the merged candidate set; the peak is their max.
    peak = max(len(vector_hits), len(pairs), len(candidates))

    return QuerySample(
        qid=qid,
        system="baseline",
        k=k,
        latency_ms=latency_ms,
        peak_intermediate_rows=peak,
        # The baseline scans the whole corpus on the ANN leg (no pushdown), so it
        # "examines" the full corpus — SM-3 is a TriDB-only property but we record
        # the baseline's full scan for contrast.
        corpus_examined=corpus.size,
        corpus_size=corpus.size,
        result_chunks=chunks[:k],
        # Three independent systems, three transaction managers, no shared snapshot
        # (golden-rule #2 is exactly what the baseline lacks). For a static
        # read-only corpus the merged answer is still consistent, so we mark the
        # read atomic; the SM-5 contrast is that TriDB *guarantees* this and the
        # baseline cannot under concurrent writes (documented, not simulated here).
        txn_atomic=True,
    )


def baseline_query_live(query: dict, k: int, corpus: Corpus) -> QuerySample:
    """Stack-gated: drive the real baseline/harness.py against the live
    Neo4j+Milvus+Postgres docker-compose stack.

    UNBUILT-HERE in static-verify mode: needs the three systems running
    (`make baseline-up`). The contract is fixed so an operator with the stack up
    can wire `baseline.harness.run_query` in against a known QuerySample shape.
    """
    raise NotImplementedError(
        "live baseline is stack-gated: run `make baseline-up` and the three "
        "systems, then wire baseline/harness.py run_query here. Use the "
        "in-process baseline model off-stack."
    )


# --------------------------------------------------------------------------- #
# Benchmark orchestration
# --------------------------------------------------------------------------- #


def run_benchmark(
    corpus: Corpus,
    k: int,
    driver: EngineDriver,
    baseline_mode: str = "inprocess",
) -> BenchmarkReport:
    """Run the canonical query for every corpus query against both systems and
    derive SM-1..SM-5."""
    baseline_fn = (
        baseline_query_inprocess
        if baseline_mode == "inprocess"
        else baseline_query_live
    )

    tridb_samples: list[QuerySample] = []
    baseline_samples: list[QuerySample] = []
    for q in corpus.queries:
        tridb_samples.append(driver.run_query(q, k, corpus))
        baseline_samples.append(baseline_fn(q, k, corpus))

    metrics = derive_metrics(tridb_samples, baseline_samples)
    return BenchmarkReport(
        k=k,
        corpus_size=corpus.size,
        num_queries=len(corpus.queries),
        engine_mode=driver.mode,
        tridb_samples=tridb_samples,
        baseline_samples=baseline_samples,
        metrics=metrics,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed-dir", type=Path, default=Path("data/seed"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--engine",
        choices=["stub", "live"],
        default="stub",
        help="'stub' runs anywhere; 'live' is GX10/engine-gated",
    )
    parser.add_argument(
        "--baseline",
        choices=["inprocess", "live"],
        default="inprocess",
        help="'inprocess' runs anywhere; 'live' needs the docker-compose stack",
    )
    parser.add_argument("--dsn", default=None, help="TriDB DSN for --engine live")
    parser.add_argument(
        "--out", type=Path, default=Path("bench/out/bench_metrics.json")
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=Path("bench/out/report.html"),
        help="also render the HTML report (DEV-1173)",
    )
    args = parser.parse_args(argv)

    corpus = load_corpus(args.seed_dir)
    driver = make_driver(args.engine, dsn=args.dsn)
    report = run_benchmark(corpus, args.k, driver, baseline_mode=args.baseline)

    report.write_json(args.out)
    print(
        f"[bench] engine={report.engine_mode} k={report.k} "
        f"corpus={report.corpus_size} queries={report.num_queries}"
    )
    for m in report.metrics:
        status = "PASS" if m.passed else "FAIL"
        print(f"[bench] {m.sm} {status}: {m.detail}")
    print(f"[bench] wrote metrics -> {args.out}")

    if args.html is not None:
        from bench.report import render_report

        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_report(report))
        print(f"[bench] wrote report -> {args.html}")

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

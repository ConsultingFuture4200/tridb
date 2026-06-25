"""Engine driver interface + deterministic stub for the TriDB benchmark.

The live TriDB engine run is GX10/engine-gated (it needs the MSVBASE fork built
and the native graph access method loaded — see CLAUDE.md "Hardware reality").
To keep the harness, metric capture, and report fully runnable and unit-tested
off-target, the engine is abstracted behind :class:`EngineDriver` with two
implementations:

  * :class:`StubDriver` — deterministic, no engine. Computes the canonical query
    answer set directly from the in-memory corpus (the ground truth a correct
    TJS plan must return) and reports the bounded, early-terminating
    intermediate sizes / corpus-examined counts the TR-1 plan would produce.
    Used everywhere off the GX10.

  * :class:`LiveDriver` — engine-gated. Connects to a running TriDB (forked
    MSVBASE) instance and executes the ONE canonical SQL/PGQ query (spec §5)
    against the loaded graph store. Marked UNBUILT-HERE: the connection +
    EXPLAIN-instrumentation path needs the live engine and is not exercised on a
    non-GX10 box.

Both return a :class:`bench.metrics.QuerySample` per query so the harness treats
them identically.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass

from bench.metrics import QuerySample


@dataclass
class Corpus:
    """In-memory corpus the drivers query.

    Mirrors the seed format produced by ``tools/seed_corpus.py`` and consumed by
    ``baseline/harness.py``:

      * ``entities``: id -> {"timestamp": int, "chunk": str, "embedding": list}
      * ``edges``: list of (src, dst)
      * ``queries``: list of {"qid", "embedding", "selected_time_range"}
    """

    entities: dict[int, dict]
    edges: list[tuple[int, int]]
    queries: list[dict]

    @property
    def size(self) -> int:
        return len(self.entities)


class EngineDriver(abc.ABC):
    """Abstract TriDB engine. The harness only ever sees this surface."""

    #: "stub" | "live" — surfaced into the report so a reader can tell whether
    #: the numbers came from the real engine or the deterministic model.
    mode: str = "abstract"

    @abc.abstractmethod
    def run_query(self, query: dict, k: int, corpus: Corpus) -> QuerySample:
        """Execute the ONE canonical query (spec §5) for a single query row and
        return its per-query sample (latency + intermediate sizes + answer set +
        atomicity)."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Deterministic stub
# --------------------------------------------------------------------------- #


def _l2_sq(a: list[float], b: list[float]) -> float:
    """Squared L2 distance. Squared is monotone with L2, so ordering is
    identical and we skip the sqrt — matches the canonical ``<->`` ordering."""
    return sum((x - y) * (x - y) for x, y in zip(a, b))


class StubDriver(EngineDriver):
    """Deterministic, engine-free model of the canonical TJS plan.

    Computes exactly what spec §5 says:

        MATCH (src:entity)-[:related_to]->(dst:entity)
        COLUMNS (src.embedding, dst.chunk, dst.timestamp)
        WHERE dst.timestamp IN :selected_time_range
        ORDER BY src.embedding <-> :question_embedding
        LIMIT k

    so its answer set is the ground truth the baseline is graded against (SM-4).

    It also reports the *fused, early-terminating* execution cost (TR-1): rather
    than materializing the full (src,dst) cross product, the plan walks sources
    in similarity order (the HNSW relaxed-monotonicity iterator) and, per source,
    expands its adjacency list and applies the time filter inline, stopping as
    soon as k qualifying chunks are produced. ``peak_intermediate_rows`` is the
    largest in-flight working set under that walk (bounded by k plus the current
    source's fan-out), and ``corpus_examined`` is the number of source entities
    the walk had to touch before early termination — both far below the
    baseline's full-materialization cost.
    """

    mode = "stub"

    def run_query(self, query: dict, k: int, corpus: Corpus) -> QuerySample:
        t0 = time.perf_counter()

        qid = int(query["qid"])
        q_emb = query["embedding"]
        time_range = set(query["selected_time_range"])

        # Adjacency list: src -> [dst, ...] (the native graph access method).
        adj: dict[int, list[int]] = {}
        for src, dst in corpus.edges:
            adj.setdefault(src, []).append(dst)

        # Sources ranked by similarity to the question embedding. The live HNSW
        # iterator yields these incrementally; the stub computes the exact order
        # so the answer set is ground truth, then models the walk's early
        # termination for the cost counters.
        ranked_src = sorted(
            corpus.entities.keys(),
            key=lambda eid: _l2_sq(corpus.entities[eid]["embedding"], q_emb),
        )

        chunks: list[str] = []
        seen_dst: set[int] = set()
        sources_examined = 0
        peak_working_set = 0

        for src in ranked_src:
            sources_examined += 1
            # Inline 1-hop expansion + time filter for this source. The working
            # set is the current source's qualifying neighbours plus the answer
            # accumulator — never the full cross product.
            working: list[int] = []
            for dst in adj.get(src, []):
                if dst in seen_dst:
                    continue
                ent = corpus.entities.get(dst)
                if ent is not None and ent["timestamp"] in time_range:
                    working.append(dst)
            peak_working_set = max(peak_working_set, len(working) + len(chunks))

            for dst in working:
                seen_dst.add(dst)
                chunks.append(corpus.entities[dst]["chunk"])
                if len(chunks) >= k:
                    break
            if len(chunks) >= k:
                break

        latency_ms = (time.perf_counter() - t0) * 1000.0
        return QuerySample(
            qid=qid,
            system="tridb",
            k=k,
            latency_ms=latency_ms,
            peak_intermediate_rows=peak_working_set,
            corpus_examined=sources_examined,
            corpus_size=corpus.size,
            result_chunks=chunks[:k],
            # One transaction manager / one WAL (golden rule #2): a single
            # canonical query is always one atomic snapshot.
            txn_atomic=True,
        )


# --------------------------------------------------------------------------- #
# Live engine driver (GX10 / engine-gated — UNBUILT-HERE)
# --------------------------------------------------------------------------- #


class LiveDriver(EngineDriver):
    """Engine-gated driver against a running forked-MSVBASE TriDB.

    UNBUILT-HERE: requires the GX10 MSVBASE build + native graph access method.
    The query text and instrumentation contract are fixed here so the on-target
    implementer drops in the psycopg connection + EXPLAIN parsing against a known
    surface; the body raises off-target rather than pretending to run.
    """

    mode = "live"

    #: The ONE canonical query (spec §5). Parameters bound at execution.
    CANONICAL_SQL = """
        SELECT chunk
        FROM GRAPH_TABLE ( MATCH (src:entity)-[:related_to]->(dst:entity)
          COLUMNS ( src.embedding AS src_embedding,
                    dst.chunk     AS chunk,
                    dst.timestamp AS timestamp ) )
        WHERE timestamp IN %(selected_time_range)s
        ORDER BY src_embedding <-> %(question_embedding)s
        LIMIT %(k)s;
    """

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn

    def run_query(self, query: dict, k: int, corpus: Corpus) -> QuerySample:
        # GX10/engine-gated. The live path must:
        #   1. psycopg.connect(self.dsn) to the forked-MSVBASE instance.
        #   2. EXPLAIN (ANALYZE, FORMAT JSON) the CANONICAL_SQL to read the real
        #      peak intermediate rows + rows-examined from the TJS custom-scan
        #      node (the in-DB equivalents of the stub's counters).
        #   3. Execute CANONICAL_SQL to capture result_chunks + latency.
        #   4. Probe txn atomicity via the single shared transaction manager.
        raise NotImplementedError(
            "LiveDriver is GX10/engine-gated (UNBUILT-HERE): needs the MSVBASE "
            "fork + native graph access method. Use StubDriver off-target."
        )


def make_driver(mode: str, dsn: str | None = None) -> EngineDriver:
    """Factory: 'stub' (default, runs anywhere) or 'live' (GX10/engine-gated)."""
    if mode == "stub":
        return StubDriver()
    if mode == "live":
        return LiveDriver(dsn=dsn)
    raise ValueError(f"unknown engine mode: {mode!r} (expected 'stub' or 'live')")

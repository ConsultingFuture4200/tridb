"""Typed metric schema for the TriDB benchmark (SM-1..SM-5 vs targets).

Spec §7 success metrics:

    SM-1: >=5x intermediate-result reduction vs. baseline (selective queries)
    SM-2: lower latency on >=80% of queries
    SM-3: <25% of corpus examined for k=5
    SM-4: >=99% answer-set parity with baseline
    SM-5: 100% transaction atomicity

This module is the single source of truth for what gets measured. It is pure
data + arithmetic: no DB clients, no engine, no I/O beyond JSON (de)serialization
so it imports cleanly anywhere (harness, report, tests).

Two layers:

  * Per-query raw observations (:class:`QuerySample`) — what the harness records
    for one query against one system (TriDB or baseline).
  * Derived SM verdicts (:class:`MetricResult`, :class:`BenchmarkReport`) —
    each SM compared against its spec target, with pass/fail + the supporting
    numbers a reviewer needs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# SM targets (spec §7) — the single place the numbers live.
# --------------------------------------------------------------------------- #

SM1_MIN_REDUCTION = 5.0  # >= 5x intermediate-result reduction
SM2_MIN_WIN_FRACTION = 0.80  # lower latency on >= 80% of queries
SM3_MAX_CORPUS_FRACTION = 0.25  # < 25% of corpus examined for k=5
SM4_MIN_PARITY = 0.99  # >= 99% answer-set parity
SM5_REQUIRED_ATOMICITY = 1.0  # 100% transaction atomicity


# --------------------------------------------------------------------------- #
# Per-query raw observations
# --------------------------------------------------------------------------- #


@dataclass
class QuerySample:
    """Per-query observations for ONE system (TriDB or baseline).

    The harness fills one of these per query per system. The SM derivations
    below consume pairs of these (TriDB sample vs baseline sample for the same
    qid).

    ``peak_intermediate_rows`` is the SM-1 surface: the largest intermediate
    result set the system materialized for this query. For the baseline this is
    the app-side merged candidate set (plus the graph/vector/relational legs it
    had to ship across system boundaries); for TriDB it is whatever the fused,
    early-terminating plan held in flight — which TR-1 keeps bounded.

    ``corpus_examined`` / ``corpus_size`` are the SM-3 surface: how many corpus
    entities the system actually touched to answer at k=5.

    ``result_chunks`` is the ordered answer set, used for SM-4 parity.

    ``txn_atomic`` is the SM-5 surface: did the query observe a consistent,
    atomic snapshot (single transaction manager for TriDB; for the baseline,
    whether the three independent systems agreed — they have no shared txn).
    """

    qid: int
    system: str  # "tridb" | "baseline"
    k: int

    latency_ms: float = 0.0
    peak_intermediate_rows: int = 0
    corpus_examined: int = 0
    corpus_size: int = 0

    result_chunks: list[str] = field(default_factory=list)
    txn_atomic: bool = True

    def corpus_fraction(self) -> float:
        """Fraction of the corpus examined (SM-3 surface)."""
        if self.corpus_size <= 0:
            return 1.0
        return self.corpus_examined / self.corpus_size


# --------------------------------------------------------------------------- #
# Derived per-SM verdicts
# --------------------------------------------------------------------------- #


@dataclass
class MetricResult:
    """One success metric (SM-N) compared against its target.

    ``value`` is the achieved measurement, ``target`` the spec threshold,
    ``passed`` the verdict, and ``detail`` a one-line human explanation with the
    supporting numbers.
    """

    sm: str  # "SM-1" .. "SM-5"
    name: str
    value: float
    target: float
    unit: str
    passed: bool
    detail: str


def _safe_ratio(numerator: float, denominator: float) -> float:
    """numerator/denominator, treating a zero denominator as 0 intermediate work
    on the baseline -> infinite reduction is meaningless, so report 0.0 and let
    the caller's detail string explain. A zero baseline never happens on a real
    selective corpus; the guard only protects degenerate/empty runs."""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def sm1_intermediate_reduction(
    tridb: list[QuerySample], baseline: list[QuerySample]
) -> MetricResult:
    """SM-1: >=5x intermediate-result reduction vs. baseline.

    Reduction = (baseline peak intermediate rows) / (TriDB peak intermediate
    rows), summed across queries so a few cheap queries do not mask the win on
    the selective ones. Passes when the aggregate ratio >= 5x.
    """
    base_total = sum(s.peak_intermediate_rows for s in baseline)
    tri_total = sum(s.peak_intermediate_rows for s in tridb)
    ratio = _safe_ratio(base_total, tri_total)
    passed = ratio >= SM1_MIN_REDUCTION
    return MetricResult(
        sm="SM-1",
        name="Intermediate-result reduction",
        value=round(ratio, 3),
        target=SM1_MIN_REDUCTION,
        unit="x",
        passed=passed,
        detail=(
            f"baseline materialized {base_total} intermediate rows vs TriDB "
            f"{tri_total} ({ratio:.2f}x reduction; target >= "
            f"{SM1_MIN_REDUCTION:g}x)"
        ),
    )


def sm2_latency_win_fraction(
    tridb: list[QuerySample], baseline: list[QuerySample]
) -> MetricResult:
    """SM-2: lower latency on >=80% of queries.

    Pairs TriDB and baseline samples by qid; counts queries where TriDB latency
    is strictly lower. Passes when the win fraction >= 0.80.

    .. warning::
       **SM-2 is structurally unmeasurable in stub mode.** The StubDriver and the
       in-process baseline are both Python simulations; both pay the same
       O(N*D) ``sorted()`` over all entities, and their ``latency_ms`` ratio
       reflects how much *Python* work each does after that sort, NOT the
       in-DB-engine vs out-of-DB-overhead difference SM-2 is meant to capture. A
       stub SM-2 "pass" is a simulation artifact, not a latency claim. Only the
       LiveDriver (GX10) produces a real SM-2 number. Treat the stub verdict as
       "simulation only" — see bench/README.md.
    """
    base_by_qid = {s.qid: s for s in baseline}
    paired = [(t, base_by_qid[t.qid]) for t in tridb if t.qid in base_by_qid]
    wins = sum(1 for t, b in paired if t.latency_ms < b.latency_ms)
    fraction = wins / len(paired) if paired else 0.0
    passed = fraction >= SM2_MIN_WIN_FRACTION
    return MetricResult(
        sm="SM-2",
        name="Latency-win fraction",
        value=round(fraction, 3),
        target=SM2_MIN_WIN_FRACTION,
        unit="fraction",
        passed=passed,
        detail=(
            f"TriDB faster on {wins}/{len(paired)} queries "
            f"({fraction:.0%}; target >= {SM2_MIN_WIN_FRACTION:.0%})"
        ),
    )


def sm3_corpus_examined(tridb: list[QuerySample]) -> MetricResult:
    """SM-3: <25% of corpus examined for k=5.

    Measured on TriDB only (it is a property of TriDB's early-terminating plan).
    Uses the worst-case (max) corpus fraction across queries so a single
    blow-out query fails the metric. Passes when max fraction < 0.25.
    """
    fractions = [s.corpus_fraction() for s in tridb if s.k == 5]
    if not fractions:
        # No k=5 queries -> fall back to all queries so the metric still reports.
        fractions = [s.corpus_fraction() for s in tridb]
    worst = max(fractions) if fractions else 1.0
    passed = worst < SM3_MAX_CORPUS_FRACTION
    return MetricResult(
        sm="SM-3",
        name="Corpus examined (k=5, worst case)",
        value=round(worst, 4),
        target=SM3_MAX_CORPUS_FRACTION,
        unit="fraction",
        passed=passed,
        detail=(
            f"worst-case {worst:.1%} of corpus examined "
            f"(target < {SM3_MAX_CORPUS_FRACTION:.0%})"
        ),
    )


def _jaccard(a: list[str], b: list[str]) -> float:
    """Set parity of two answer sets (order-insensitive). Two empty sets are
    defined as full parity (1.0) — both systems agreed there is nothing to
    return."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def sm4_answer_parity(
    tridb: list[QuerySample], baseline: list[QuerySample]
) -> MetricResult:
    """SM-4: >=99% answer-set parity with baseline.

    Per-query Jaccard overlap of the answer sets, averaged across queries.
    Passes when the mean parity >= 0.99.
    """
    base_by_qid = {s.qid: s for s in baseline}
    paired = [(t, base_by_qid[t.qid]) for t in tridb if t.qid in base_by_qid]
    parities = [_jaccard(t.result_chunks, b.result_chunks) for t, b in paired]
    mean_parity = sum(parities) / len(parities) if parities else 0.0
    passed = mean_parity >= SM4_MIN_PARITY
    return MetricResult(
        sm="SM-4",
        name="Answer-set parity",
        value=round(mean_parity, 4),
        target=SM4_MIN_PARITY,
        unit="fraction",
        passed=passed,
        detail=(
            f"mean answer-set parity {mean_parity:.1%} across {len(paired)} "
            f"queries (target >= {SM4_MIN_PARITY:.0%})"
        ),
    )


def sm5_txn_atomicity(tridb: list[QuerySample]) -> MetricResult:
    """SM-5: 100% transaction atomicity.

    Fraction of TriDB queries that observed an atomic snapshot. Passes only at
    exactly 1.0 — a single non-atomic read fails the metric (one WAL, one txn
    manager is the whole point per golden rule #2).
    """
    if not tridb:
        return MetricResult(
            sm="SM-5",
            name="Transaction atomicity",
            value=0.0,
            target=SM5_REQUIRED_ATOMICITY,
            unit="fraction",
            passed=False,
            detail="no TriDB samples to evaluate",
        )
    atomic = sum(1 for s in tridb if s.txn_atomic)
    fraction = atomic / len(tridb)
    passed = fraction >= SM5_REQUIRED_ATOMICITY
    return MetricResult(
        sm="SM-5",
        name="Transaction atomicity",
        value=round(fraction, 4),
        target=SM5_REQUIRED_ATOMICITY,
        unit="fraction",
        passed=passed,
        detail=(
            f"{atomic}/{len(tridb)} queries atomic "
            f"({fraction:.0%}; target {SM5_REQUIRED_ATOMICITY:.0%})"
        ),
    )


# --------------------------------------------------------------------------- #
# Aggregate report container
# --------------------------------------------------------------------------- #


@dataclass
class BenchmarkReport:
    """Full benchmark result: raw per-query samples for both systems + the five
    derived SM verdicts. JSON-serializable for the report renderer and for
    archival.
    """

    k: int
    corpus_size: int
    num_queries: int
    engine_mode: str  # "stub" | "live"
    tridb_samples: list[QuerySample]
    baseline_samples: list[QuerySample]
    metrics: list[MetricResult]

    @property
    def all_passed(self) -> bool:
        return all(m.passed for m in self.metrics)

    def to_dict(self) -> dict:
        return {
            "k": self.k,
            "corpus_size": self.corpus_size,
            "num_queries": self.num_queries,
            "engine_mode": self.engine_mode,
            "all_passed": self.all_passed,
            "metrics": [asdict(m) for m in self.metrics],
            "tridb_samples": [asdict(s) for s in self.tridb_samples],
            "baseline_samples": [asdict(s) for s in self.baseline_samples],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())

    @classmethod
    def from_dict(cls, d: dict) -> BenchmarkReport:
        return cls(
            k=d["k"],
            corpus_size=d["corpus_size"],
            num_queries=d["num_queries"],
            engine_mode=d["engine_mode"],
            tridb_samples=[QuerySample(**s) for s in d["tridb_samples"]],
            baseline_samples=[QuerySample(**s) for s in d["baseline_samples"]],
            metrics=[MetricResult(**m) for m in d["metrics"]],
        )

    @classmethod
    def from_json(cls, text: str) -> BenchmarkReport:
        return cls.from_dict(json.loads(text))


def derive_metrics(
    tridb: list[QuerySample], baseline: list[QuerySample]
) -> list[MetricResult]:
    """Compute all five SM verdicts from paired per-query samples."""
    return [
        sm1_intermediate_reduction(tridb, baseline),
        sm2_latency_win_fraction(tridb, baseline),
        sm3_corpus_examined(tridb),
        sm4_answer_parity(tridb, baseline),
        sm5_txn_atomicity(tridb),
    ]

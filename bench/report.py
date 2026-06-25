"""Read-once HTML benchmark report (DEV-1173).

Renders a single self-contained HTML page from a :class:`bench.metrics.Benchmark
Report` comparing TriDB vs the multi-system baseline:

  * a SM-1..SM-5 scoreboard (achieved value vs spec §7 target, pass/fail),
  * a per-query table (latency, peak intermediate rows, corpus examined, parity),
  * a v2-recommendation section keyed off which SMs passed/failed.

"Read-once" = no JS, no external assets, no live queries: open the file and the
whole story is on the page. Pure string templating (no template-engine dep) so
it imports anywhere ``bench.metrics`` does.
"""

from __future__ import annotations

import argparse
import html
from datetime import datetime, timezone
from pathlib import Path

from bench.metrics import BenchmarkReport, MetricResult, QuerySample


def _esc(s: object) -> str:
    return html.escape(str(s))


def _badge(passed: bool) -> str:
    cls = "pass" if passed else "fail"
    label = "PASS" if passed else "FAIL"
    return f'<span class="badge {cls}">{label}</span>'


def _scoreboard_rows(metrics: list[MetricResult]) -> str:
    rows = []
    for m in metrics:
        rows.append(
            "<tr>"
            f"<td class='sm'>{_esc(m.sm)}</td>"
            f"<td>{_esc(m.name)}</td>"
            f"<td class='num'>{_esc(m.value)} {_esc(m.unit)}</td>"
            f"<td class='num'>{_esc(m.target)} {_esc(m.unit)}</td>"
            f"<td>{_badge(m.passed)}</td>"
            f"<td class='detail'>{_esc(m.detail)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _per_query_rows(tridb: list[QuerySample], baseline: list[QuerySample]) -> str:
    from bench.metrics import _jaccard  # local: parity helper

    base_by_qid = {s.qid: s for s in baseline}
    rows = []
    for t in tridb:
        b = base_by_qid.get(t.qid)
        if b is None:
            continue
        parity = _jaccard(t.result_chunks, b.result_chunks)
        reduction = (
            b.peak_intermediate_rows / t.peak_intermediate_rows
            if t.peak_intermediate_rows
            else float("inf")
        )
        faster = "win" if t.latency_ms < b.latency_ms else "loss"
        rows.append(
            "<tr>"
            f"<td class='num'>{_esc(t.qid)}</td>"
            f"<td class='num'>{t.latency_ms:.2f}</td>"
            f"<td class='num'>{b.latency_ms:.2f}</td>"
            f"<td class='num lat-{faster}'>{faster}</td>"
            f"<td class='num'>{_esc(t.peak_intermediate_rows)}</td>"
            f"<td class='num'>{_esc(b.peak_intermediate_rows)}</td>"
            f"<td class='num'>{reduction:.1f}x</td>"
            f"<td class='num'>{t.corpus_fraction():.1%}</td>"
            f"<td class='num'>{parity:.0%}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _v2_recommendations(report: BenchmarkReport) -> str:
    """v2-recommendation section keyed off which SMs passed/failed."""
    by_sm = {m.sm: m for m in report.metrics}
    items: list[str] = []

    if not by_sm["SM-1"].passed:
        items.append(
            "<b>SM-1 (intermediate reduction) under target.</b> The fused plan is "
            "still materializing too much in flight. v2: push the relational time "
            "predicate into the graph-expansion iterator (predicate pushdown into "
            "the adjacency walk) so non-qualifying neighbours are never emitted; "
            "revisit the TJS working-set bound."
        )
    else:
        items.append(
            "<b>SM-1 holds.</b> The early-terminating fused plan keeps the working "
            "set bounded. v2: extend the same pushdown discipline to the BM25 seam "
            "(currently architected-but-closed) without regressing the bound."
        )

    if not by_sm["SM-2"].passed:
        items.append(
            "<b>SM-2 (latency win) under target.</b> Per-query latency loses on too "
            "many queries. v2: add the deferred cost-based leg-ordering (beyond the "
            "v1 selectivity heuristic, FR-6) and cardinality estimation so the "
            "planner stops picking a bad first leg on the losing queries."
        )

    if not by_sm["SM-3"].passed:
        items.append(
            "<b>SM-3 (corpus examined) over budget.</b> Early termination is "
            "touching too many sources. v2: tighten the HNSW relaxed-monotonicity "
            "stopping condition and consider a graph-aware ANN entry point so the "
            "similarity walk reaches qualifying neighbourhoods sooner."
        )

    if not by_sm["SM-4"].passed:
        items.append(
            "<b>SM-4 (answer parity) below 99%.</b> TriDB and baseline disagree on "
            "results — a correctness regression, not a perf knob. v2 (and a v1 "
            "blocker): diff the divergent qids, confirm tie-breaking and "
            "relaxed-monotonicity ordering match the baseline's exact semantics."
        )

    if not by_sm["SM-5"].passed:
        items.append(
            "<b>SM-5 (atomicity) below 100%.</b> A read observed a non-atomic "
            "snapshot — this violates golden rule #2 (one txn manager, one WAL). "
            "v2 is moot until v1 closes this: trace the offending query to the "
            "shared transaction manager."
        )

    if report.all_passed:
        items.append(
            "<b>All five metrics pass on this corpus.</b> v2 priorities shift from "
            "the core thesis to breadth: the deferred cost-based optimizer, "
            "multi-hop / typed edges beyond a single <code>:related_to</code>, the "
            "BM25 fourth store, and the 128 GB headline benchmark on the GX10."
        )

    if report.engine_mode == "stub":
        items.append(
            "<b>Caveat — engine mode is <code>stub</code>.</b> These numbers come "
            "from the deterministic model, not the live forked-MSVBASE engine. "
            "The headline result is GX10/engine-gated; re-run with "
            "<code>--engine live</code> on-target before quoting SM-1/SM-2/SM-3 "
            "latencies as real."
        )

    return "\n".join(f"<li>{i}</li>" for i in items)


_CSS = """
:root { --pass:#1a7f37; --fail:#cf222e; --ink:#1f2328; --muted:#656d76;
        --line:#d0d7de; --bg:#ffffff; --head:#f6f8fa; }
* { box-sizing: border-box; }
body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       color: var(--ink); background: var(--bg); margin: 0; padding: 2rem; max-width: 1100px; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2rem 0 .5rem; border-bottom: 1px solid var(--line); padding-bottom: .25rem; }
.sub { color: var(--muted); margin: 0 0 1rem; }
.meta { display: flex; gap: 1.5rem; flex-wrap: wrap; color: var(--muted); font-size: .9rem; margin-bottom: 1rem; }
.meta b { color: var(--ink); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { border: 1px solid var(--line); padding: .4rem .6rem; text-align: left; }
th { background: var(--head); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.sm { font-weight: 600; }
td.detail { color: var(--muted); }
.badge { display: inline-block; padding: .05rem .5rem; border-radius: 1rem; font-size: .78rem; font-weight: 600; color: #fff; }
.badge.pass { background: var(--pass); }
.badge.fail { background: var(--fail); }
.lat-win { color: var(--pass); font-weight: 600; }
.lat-loss { color: var(--fail); font-weight: 600; }
.verdict { font-size: 1.1rem; font-weight: 600; margin: 1rem 0; }
.verdict.pass { color: var(--pass); }
.verdict.fail { color: var(--fail); }
ul.recs { padding-left: 1.2rem; }
ul.recs li { margin: .5rem 0; }
code { background: var(--head); padding: 0 .25rem; border-radius: 3px; }
footer { margin-top: 2rem; color: var(--muted); font-size: .8rem; border-top: 1px solid var(--line); padding-top: .75rem; }
"""

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TriDB Benchmark Report</title>
<style>{css}</style>
</head>
<body>
<h1>TriDB Benchmark Report</h1>
<p class="sub">TriDB (tri-modal, single-process) vs the multi-system baseline
(Neo4j + Milvus + Postgres, merged app-side) on the canonical query (spec §5).</p>

<div class="meta">
  <span><b>Generated</b> {generated}</span>
  <span><b>Engine mode</b> <code>{engine_mode}</code></span>
  <span><b>k</b> {k}</span>
  <span><b>Corpus</b> {corpus_size} entities</span>
  <span><b>Queries</b> {num_queries}</span>
</div>

<div class="verdict {verdict_cls}">Overall: {verdict}</div>

<h2>Success metrics (SM-1..SM-5 vs spec §7 targets)</h2>
<table>
<thead><tr>
  <th>SM</th><th>Metric</th><th class="num">Achieved</th>
  <th class="num">Target</th><th>Verdict</th><th>Detail</th>
</tr></thead>
<tbody>
{scoreboard}
</tbody>
</table>

<h2>Per-query comparison</h2>
<table>
<thead><tr>
  <th class="num">qid</th>
  <th class="num">TriDB ms</th><th class="num">Base ms</th><th class="num">latency</th>
  <th class="num">TriDB peak</th><th class="num">Base peak</th><th class="num">reduction</th>
  <th class="num">corpus seen</th><th class="num">parity</th>
</tr></thead>
<tbody>
{per_query}
</tbody>
</table>

<h2>v2 recommendations (DEV-1173)</h2>
<ul class="recs">
{recommendations}
</ul>

<footer>
TriDB benchmark harness (DEV-1172) + report (DEV-1173). Baseline = AkasicDB
Scenario 2 (out-of-DB integration). SM targets are spec §7. Engine mode
<code>{engine_mode}</code>: <code>stub</code> = deterministic model (no live
engine), <code>live</code> = forked-MSVBASE on the GX10.
</footer>
</body>
</html>
"""


def render_report(report: BenchmarkReport) -> str:
    """Render a self-contained HTML string for the given benchmark report."""
    verdict = "ALL METRICS PASS" if report.all_passed else "ONE OR MORE METRICS FAIL"
    return _TEMPLATE.format(
        css=_CSS,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        engine_mode=_esc(report.engine_mode),
        k=_esc(report.k),
        corpus_size=_esc(report.corpus_size),
        num_queries=_esc(report.num_queries),
        verdict=verdict,
        verdict_cls="pass" if report.all_passed else "fail",
        scoreboard=_scoreboard_rows(report.metrics),
        per_query=_per_query_rows(report.tridb_samples, report.baseline_samples),
        recommendations=_v2_recommendations(report),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--metrics",
        type=Path,
        default=Path("bench/out/bench_metrics.json"),
        help="benchmark metrics JSON produced by bench/harness.py",
    )
    parser.add_argument("--out", type=Path, default=Path("bench/out/report.html"))
    args = parser.parse_args(argv)

    report = BenchmarkReport.from_json(args.metrics.read_text())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(report))
    print(f"[report] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

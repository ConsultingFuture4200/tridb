# SM-4 on stock PG — the seedless recall surface of the re-homed operator (1M, measured)

**What this is.** The (term_cond × hnsw.max_scan_tuples) recall surface for `tjs_pg`'s
vector-first path (ADR-0019) at N=1,002,331 — the budget-shaped-recall measurement ADR-0015
E3.3 said must replace the fork's term_cond-only curve. Filtered ANN (typed P31 filter,
moderate selectivity), 50 seeded queries, exact oracle, client-clocked over TCP, stock PG 17 +
pgvector on the DGX Spark. Raw: `bench/results/wd_1m_sm4_seedless.json`.

| term_cond | budget 1k | 5k | 20k | 80k | examined (med) | latency @20k |
|---|---|---|---|---|---|---|
| 16  | 0.750 (14% capped) | 0.800 | 0.830 (4% capped) | 0.830 | 46 | 0.8 ms |
| 64  | 0.752 (18% capped) | 0.808 | 0.840 (6% capped) | 0.840 | 140 | 1.5 ms |
| 256 | 0.752 (42% capped) | 0.814 | 0.850 (18% capped) | 0.850 | 868–1014 | 11.2 ms |

**Honest findings.**

1. **Recall is budget-shaped at the low end, exactly as E3.3 predicted** — every term_cond row
   rises with budget and saturates at 20k tuples; the capped fraction (disclosed per point)
   explains the low-budget loss.
2. **Past saturation the ceiling is the relaxed-order stream itself, not the budget**: 80k
   buys nothing over 20k. The seedless plateau on this workload is **~0.83–0.85 recall@10** —
   the 20k-corpus ADR-0015 probe's 0.965 does not survive at 1M with typed filters.
3. **term_cond has steep diminishing returns**: 16→256 buys +0.02 recall for ~14× the examined
   work and ~14× the latency. The operating sweet spot is term_cond 16–64 at budget ≥20k:
   0.83–0.84 recall@10 at **0.8–1.5 ms** (vs the multi-store baseline's ~3.3 ms at its best).
4. **Positioning consequence:** filter-first remains the headline physical path (recall 0.992
   at 0.14 ms, Gate B); seedless on stock PG is *serviceable* (sub-ms to low-ms, ~0.85
   ceiling) and its limits are now measured and public, not hidden. Raising the ceiling means
   pgvector-side work (ef_search interplay, quantized rescoring) or the ADR-0012 stretch
   (PPR-graded bridges) — future work, listed not promised.

Fork phase/bridge parity note: the seedless semantics measured here include the fork's
guaranteed-bridge injection (ADR-0012 recipe B, commit 81b8023) with m_seeds=0 (pure filtered
ANN — the E3 probe shape). Bridge-mode benchmarks belong to the GraphRAG workload suite.

## Addendum 2026-07-17 (plan 093): the "capped" fraction above is not a proven budget cap

Plan 074 made `tjs_open_budget_capped()` unable to ever return true (false, or SQL NULL for an
unobservable ending) — pgvector's iterative scan API does not disclose whether
`hnsw.max_scan_tuples` or natural index exhaustion ended the stream. The `budget_capped_fraction`
values quoted in the table above (and in the raw `bench/results/wd_1m_sm4_seedless.json`, left
unmodified) were measured under the **pre-074** contract, where the boolean fired whenever the
stream ended before `term_cond`. Re-read them as: **"fraction of queries whose stream ended
before term_cond"** — right-censored (possibly budget-shaped, never proven) — not as a proven
budget cap. The historical numbers are not wrong as measurements; the label was wrong.

`bench/wikidata_sm4_seedless.py` now collects the honest successors instead:

- `stream_end_unknown_fraction` — fraction of queries whose
  `tjs_open_termination_reason()` was `stream_end_unknown` (the direct, correctly-named
  successor of the old capped fraction; same right-censored caveat — possibly budget-shaped
  on this harness's fixed `hnsw.max_scan_tuples` sweep, but pgvector never discloses which).
- `graph_censored_fraction` — fraction of queries whose `tjs_open_graph_censored()` was
  true (plan 077's independent graph-leg budget, `tjs.graph_work_budget`). This harness's
  own `tjs_open` calls use `m_seeds=0, hops=0` (pure filtered ANN, no graph leg touched), so
  this fraction reads 0.0 for every point in the committed 1M results — an honest "graph leg
  not exercised", not a vacuous metric.
- `mean_graph_examined` (new) — mean edge-steps the graph leg consumed per query (0 on this
  harness, for the same m_seeds=0/hops=0 reason above).

A live small-scale validation (5-query constructed points, stock `tridb/pg17-unfork:dev`,
30-row corpus + a 500-edge hub) confirmed both new fractions move: a huge-`term_cond`/
huge-budget point on the tiny corpus read `stream_end_unknown_fraction = 1.0` (natural
exhaustion, term_cond never fires); a `m_seeds=1, hops=1` point with
`tjs.graph_work_budget = 128` (« the hub's 500-edge reach) read `graph_censored_fraction =
1.0` with `tjs_open_graph_examined() = 128` exactly; a negative control (`term_cond=1`,
`m_seeds=0`) read both fractions back at 0. Transcript in the plan-093 commit.

**Any future re-measure of this curve (1M or otherwise) must report
`stream_end_unknown_fraction`/`graph_censored_fraction`, never `budget_capped_fraction`.**
The 1M corpus/engine were not available in this session (host-only worktree, no Spark/GX10
loaded corpus reachable) — the 1M re-run against the new counters is **pending**, not run.

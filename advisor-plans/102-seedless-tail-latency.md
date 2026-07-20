# Plan 102: Seedless `tjs_open` tail-latency defect (issue #30) — diagnose, budget-shaped termination, re-gate

> **Executor instructions**: DIAGNOSE BEFORE FIXING. Step 1's per-query evidence on the Spark
> decides Step 2; if BOTH hypotheses are disproven, STOP and report. Never modify or restart the
> existing Spark containers (`tridb-wikidata-pg17` is READ-ONLY diagnosis material); the gate runs
> in a NEW `tridb-issue30-*` container built from this branch. Skip the advisor index update.

## Status

- **Priority**: P1 (open defect on the released 0.2.0 seedless path; issue #30)
- **Effort**: M–L (C fix is small; the evidence work is the bulk)
- **Risk**: MED (touches the seedless stream loop; DEV-1169 regression risk is the named hazard)
- **Depends on**: ADR-0019/0020 (the stock seedless scan + graph-leg budget), ADR-0015 E3 (the
  named gaps), plan 074 (honest disclosure), plan 095/097 (GUC default-inertness discipline),
  v0.2.0 release (2026-07-20 — extension surface is RELEASED; versioning per CONTRIBUTING)
- **Planned at**: 2026-07-20
- **Branch**: `advisor/102-seedless-tails`

## The defect (issue #30, measured 2026-07-20)

Seedless `tjs_open` p95 is 70–220 ms vs pgvector iterative's stable ~26–30 ms at matched recall
(~0.82/0.83) on the 1M Wikidata slice (`docs/benchmark_allpg_baseline_v0.1.0.md`, seedless leg).
Filter-first executes in ~49 µs median on the same box — the engine is not slow; the seedless
stream drain specifically is. p95 grows with the sweep while pgvector's stays flat: a per-query
pathological case, consistent with the open ADR-0015 E3 gaps (no per-candidate distance exposure;
termination not shaped by the caller's budget).

## Step 1 — Diagnose on the Spark (evidence, not assumption)

Environment: ssh `spark`; EXISTING container `tridb-wikidata-pg17` (1M Gate B corpus, 0.1.0-era
extensions) — READ-ONLY queries only. Own scripts go in `~/issue30/` via scp.

Re-run the seedless sweep points with PER-QUERY traces (wall ms, `tjs_open_candidates_examined()`,
termination reason, filter selectivity of the query's P31 type) and identify the p95 outliers.
Test two hypotheses:

- **H1 (superlinear drain)**: pgvector's relaxed-order iterative scan re-walks the graph with
  growing effort per resumed round, so wall time grows superlinearly in tuples drained; the
  ADR-0007 term_cond rule (k passers + tc CONSECUTIVE losing passers; filter-failers exempt from
  the drop count — DEV-1169) forces ~(k+tc)/selectivity drained tuples vs plain SQL's
  ~k/selectivity. Evidence: per-query drained-tuple counts vs wall time vs selectivity; outliers
  vs medians.
- **H2 (per-candidate probe cost)**: the relational filter check is one SPI query per stream
  candidate (`tjs_pg.c` ~line 1206, plan prepared once per call at ~1154). Determine cached vs
  re-planned behavior and its per-tuple cost share (same query shape with trivially-true filter
  vs real filter at fixed drain depth, or standalone probe timing).

**Deliverable**: a finding table — per-outlier-query drained tuples, passers, wall ms, H1/H2
attribution. **STOP condition**: both hypotheses disproven → report, no fix.

## Step 2 — Fix (shaped by Step 1; smallest correct change)

Pre-authorized directions:

1. **E3 budget-shaped termination**: `tjs.vector_scan_budget` (PGC_USERSET int GUC, 0 = disabled
   = today's behavior, default 0 — default-inert per plan 095 discipline). When > 0, the seedless
   stream ends at the cap with HONEST disclosure: new `tjs_open_termination_reason()` value
   `'scan_budget'`; `tjs_open_budget_capped()` returns TRUE for that ending (the boolean finally
   has an observable signal — one the operator itself owns, same epistemics as
   `tjs_open_graph_censored()`). Never silently.
2. **If H2 confirmed and the probe is uncached**: cache it (SPI_prepare once per call +
   SPI_execute_plan per candidate — note: this is ALREADY the code's shape; if the residual
   per-candidate constant is the cost, cut it) — do NOT rewrite the probe into direct heap access
   unless the cached-plan fix is measured insufficient; flag instead.

Constraints: do NOT change the drop-rule semantics (DEV-1169: predicate rejections never count as
drops); do NOT touch filter-first; no default behavior changes (byte-identical with the GUC at 0).
Version discipline: 0.2.0 is RELEASED (v0.2.0 tag pushed 2026-07-20), so any SQL-surface change
needs 0.2.0→0.3.0 scaffolding per CONTRIBUTING — a GUC + a new return value from an EXISTING
function needs no SQL-surface change; prefer exactly that.

## Step 3 — Tests

New stock-suite SQL coverage: scan-budget termination fires + is disclosed
(`termination_reason() = 'scan_budget'`, `budget_capped() = true`, examined == budget); negative
control: with the budget at 0/unset, behavior byte-identical to today against an existing fixture;
DEV-1169 regression coverage stays green (filter-failers still exempt from drops under the new
budget). Gates:

- `make stock-graph-test PG_MAJOR=17` AND `PG_MAJOR=16` (fail-fast, full list)
- `make stock-crash-test PG_MAJOR=17`
- `bash scripts/tjs_parity_test.sh` — 11/11
- `make stock-upgrade-test` + `make stock-writer-lock-test` IF extension packaging touched
- `make test PY=...` + `make lint PY=...` + `git diff --check`

## Step 4 — The gate (issue #30 acceptance targets, on the Spark)

Build a NEW container `tridb-issue30-pg17` (own port; check `ss -ltn`) from this branch's
extensions on the same stock PG17+pgvector base (`scripts/pg17/`), load the SAME 1M slice with the
committed loader (`tools/wikidata_engine_load.py`; edge-parity hard gate **7,422,959**; docker
`--shm-size` ≥ maintenance_work_mem for the parallel HNSW build). Re-run the seedless leg
(`bench/wd_allpg_baseline.py seedless` / `bench/wikidata_sm4_seedless.py`) with the SAME 50 pinned
queries + oracle + single-backend core-pinning protocol, sweeping the new budget knob. Same-day
rows: (a) OLD build — existing `tridb-wikidata-pg17`, read-only; (b) NEW build; (c) plain pgvector
iterative in the NEW container.

**TARGETS (from #30)**: at matched recall (~0.82/0.83), median within ~1.1× of plain pgvector AND
p95 within 2× of pgvector's. Report the full table verbatim WHATEVER it shows — an honest miss is
reported as a miss; the benchmark is never tuned to pass.

Docs: append an addendum to `docs/benchmark_allpg_baseline_v0.1.0.md` (append-don't-rewrite) with
the post-fix table; fix evidence lands in this plan doc.

## STOP conditions

- Step 1 disproves BOTH H1 and H2 → report the finding table, make no fix.
- The fix cannot stay default-inert (any existing suite shows a byte diff with the GUC at 0).
- A DEV-1169 regression appears in any form (predicate rejections counted as drops / empty
  answers at tight selectivity).
- The fix requires a pgvector patch or a new SQL-surface function that forces 0.3.0 scaffolding
  and the GUC-only route is measured insufficient → report before scaffolding.

## Boundaries

Never push; never merge to master; existing Spark containers untouched; `tridb-issue30-pg17` is
LEFT RUNNING at the end (name/port reported) for advisor verification; other scratch containers
removed. Commits early and often: `fix(tjs): ...`, `test(tjs): ...`, `bench(tjs): ...`.

REPORT FORMAT: STATUS / STEP-1 FINDING TABLE verbatim / FIX (incl. version-discipline choice) /
GATES / STEP-4 BEFORE-AFTER TABLE verbatim vs targets / FILES CHANGED / NOTES / WORKTREE+commits.

## Step 1 evidence (executed 2026-07-20, Spark, container `tridb-wikidata-pg17` read-only)

Harness: `~/issue30/diag30.py` + `diag30_d.py` on the Spark (results `~/issue30/diag30.json`);
same 50 seeded queries (seed 1354) as the published leg, live exact oracle, median of 3
client-clocked reps. Note: this container's extensions are 0.1.0 (`tjs_open_termination_reason()`
absent — `tjs_open_budget_capped()` used instead); the stream/probe architecture under test is
unchanged through 0.2.0.

### Per-outlier trace (the finding table)

Reproduced sweep: tc=16/80k median 0.79 ms p95 ~68 ms; tc=64/20k median 1.53 ms p95 ~110 ms;
tc=256/20k median 10.96 ms p95 ~202 ms (matches the published defect signature). Slowest queries
at tc=256/20k ("m" = filter-type members of 1,002,331; est. passers = examined x m/N — a uniform
lower bound; passers actually cluster vector-near for typical queries, which is why medians
terminate fast):

| x | m (members) | examined | est. passers | wall ms | us/tuple | recall |
|---:|---:|---:|---:|---:|---:|---:|
| 506575 | 122 | 21312 | 2.6 | 208.8 | 9.8 | 0.60 |
| 379121 | 155 | 21072 | 3.3 | 206.3 | 9.8 | 1.00 |
| 858662 | 544 | 20490 | 11.1 | 201.7 | 9.9 | 1.00 |
| 373513 | 344 | 20612 | 7.1 | 201.7 | 9.8 | 1.00 |
| 875781 | 126 | 21160 | 2.7 | 199.7 | 9.4 | 1.00 |
| 454669 | 239 | 22595 | 5.4 | 195.5 | 8.7 | 1.00 |
| (median query) | — | ~90–900 | — | 0.7–4.1 | — | — |

Every p95 outlier is a very-selective-filter query that drains to pgvector's internal stream end
(~20–25k tuples — `hnsw.scan_mem_multiplier` memory bound and/or `max_scan_tuples`, with
per-round overshoot) at ~9–10 us/drained tuple. 25 of 50 queries examine < 1000 tuples and take
< ~4 ms; 9 of 50 drain >= 10k and take ~200 ms at tc=256.

### H1 — superlinear drain: CONFIRMED (pgvector-intrinsic, amplified by the passer-only rule)

Pure drain (tc=0, filter='', NO SPI probe), same box, per-query:

| max_scan_tuples | examined | wall ms | us/tuple |
|---:|---:|---:|---:|
| 1000 | ~1.3–1.9k | 0.9–1.5 | 0.65–0.81 |
| 5000 | ~5.4–6.6k | 5.5–5.9 | 0.83–1.08 |
| 20000 | ~20–25k | 85–97 | 3.8–4.5 |
| 80000 | ~21–26k (stream self-ends) | 89–98 | 3.8–4.5 |

Marginal cost per drained tuple rises ~6x between shallow and deep (0.7 -> 4.5 us/t; 3.7x more
tuples between the 5k and 20k points costs 15x more time). ef_search is NOT the driver: a
follow-up sweep (ef in {40,100,200,800}) leaves drain, tjs, and native-pgvector times flat.
Native pgvector pays the SAME deep-drain cost (its own starved-filter queries measured 82–120 ms
same-day, incl. one returning 0 rows at 120 ms) — the drain cost is pgvector-iterative-scan-
intrinsic, not tjs-added. The tjs-specific amplifier is termination: the ADR-0007 drop rule
counts only filter-PASSING candidates (DEV-1169, correct and non-negotiable), so when passers
stop appearing the operator has NO bound of its own and always rides to the stream end. That is
the E3.3 gap: termination is not shaped by any caller budget.

### H2 — per-candidate probe cost: CONFIRMED (~5 us/candidate; plan already cached)

Fixed-drain ablation (tc=0, mst=20000, ~20–25k candidates/query, 10 queries): filter=''
(no probe) 81–98 ms vs filter='true' (trivially-true probe) 184–231 ms vs the real filter
192–244 ms. The probe adds **4.7–5.5 us per candidate** — it roughly DOUBLES the deep-drain
wall (~105 ms of the ~200 ms outliers). The real filter's array-containment adds ~nothing over
'true': the cost is SPI per-execution machinery, NOT planning — the plan is already prepared
once per call (`SPI_prepare` at tjs_pg.c:1154, `SPI_execute_plan` per candidate), so the
pre-authorized "cache it" remedy is already in place and measured insufficient on its own.
Direct-heap/ExprState probe evaluation is NOT implemented per the plan's constraint — flagged
in Notes as the remaining per-candidate lever.

### Verdict

Both hypotheses CONFIRMED -> proceed to Step 2 as planned: `tjs.vector_scan_budget` caps the
drain (bounding BOTH the H1 superlinear region and the H2 probe volume), disclosed via
'scan_budget'/budget_capped()=true. Same-day note for Step 4: native pgvector's own tails on
the starved queries measured 82–120 ms today (the published 26–30 ms p95 sat below the 3rd
slowest query of 50); the gate table re-measures all rows same-day.

## Step 4 evidence (filled in during execution)

_Pending._

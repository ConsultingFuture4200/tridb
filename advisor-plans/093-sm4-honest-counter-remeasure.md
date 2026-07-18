# Plan 093: SM-4 seedless bench — replace the vacuous capped-fraction with the honest counters

> **Executor instructions**: Host-Python change + tests, with a live small-scale engine validation
> on the local stock image. Do NOT regenerate committed 1M results; the at-scale re-measure is
> gated on corpus availability and must be reported as run/not-run honestly. Skip the advisor
> index update.
>
> **Drift check (run first)**:
> `git diff --stat 6de2e30..HEAD -- bench/wikidata_sm4_seedless.py tests/ docs/sm4_seedless_stock_v0.1.0.md src/tjs_pg/tjs_pg--0.1.0.sql`

## Status

- **Priority**: P1 (measurement integrity; the current metric is actively wrong)
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 074, 077 (both merged in your base)
- **Category**: measurement integrity / tests
- **Planned at**: commit `6de2e30`, 2026-07-17

## Why this matters

Plan 074 made `tjs_open_budget_capped()` never return true (it is false or NULL), so the SM-4
seedless bench's `budget_capped_fraction` is now vacuously 0.0 — a disclosure metric that can no
longer disclose. Plan 077 additionally introduced a second, independent capping axis
(`tjs_open_graph_censored()` — the graph-leg budget). The bench must count what the operator
actually reports: stream endings via `termination_reason`, and graph censoring via the new boolean.

## Current state (verified)

- `bench/wikidata_sm4_seedless.py:94-98`:
  ```python
  cur.execute("SELECT tjs_open_candidates_examined(), tjs_open_budget_capped()")
  ...
  capped += 1   # truthy boolean — can never fire post-074 (False or None only)
  ```
  `:118` emits `"budget_capped_fraction"`; `:7-8` and `:155,171` carry the old wording.
- Post-074 operator API: `tjs_open_termination_reason()` returns
  `filter_first | term_cond | stream_end_unknown`; `stream_end_unknown` is the right-censored
  "stream ended: pgvector budget OR natural exhaustion, unobservable" state.
- Post-077 operator API: `tjs_open_graph_censored() -> boolean` (real boolean, never NULL) and
  `tjs_open_graph_examined() -> bigint` (edge-steps).
- Historical note (already recorded in the advisor index): fractions in the committed
  `bench/results/wd_1m_sm4_seedless.json` were measured under the pre-074 contract and actually
  mean "stream ended before term_cond", not proven budget caps.

## Target metrics per sweep point

- `stream_end_unknown_fraction` — fraction of queries whose reason was `stream_end_unknown`
  (the honest successor of the old fraction; on this harness's fixed `hnsw.max_scan_tuples`
  sweep, an unknown ending is *possibly budget-shaped* — say exactly that in the label).
- `graph_censored_fraction` — fraction with `tjs_open_graph_censored() = true`.
- Keep `examined` stats; additionally record mean `tjs_open_graph_examined()`.
- Report text/labels updated to the censored vocabulary; never claim a proven budget cap.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused | `.venv/bin/pytest tests/ -q -k 'sm4 or seedless'` | all pass |
| Host | `make test && make lint` | exit 0 |
| Live validation | small-corpus run against `tridb/pg17-unfork:dev` (see Step 3) | fractions behave as constructed |

## Scope

**In scope**: `bench/wikidata_sm4_seedless.py`, its test home in `tests/`,
`docs/sm4_seedless_stock_v0.1.0.md` (append a dated addendum re-interpreting the historical
fraction; do not rewrite results).

**Out of scope**: operator C/SQL; regenerating `bench/results/*`; the full 1M re-measure unless
the corpus + loaded engine ALREADY exist locally (see STOP).

## Git workflow

Branch `advisor/093-sm4-honest-counters`. Suggested commit: `fix(bench): sm4 counts censored endings`.

## Steps

### Step 1: Failing test first

Unit-test the per-query counter collection seam (monkeypatched cursor or extracted pure reducer):
reasons `['term_cond','stream_end_unknown','stream_end_unknown']` + censored `[False,True,False]`
must yield `stream_end_unknown_fraction=2/3`, `graph_censored_fraction=1/3`; assert the emitted
point dict no longer contains `budget_capped_fraction` (or contains it only as an explicitly
deprecated alias — pick one and be consistent with the JSON schema consumers, grep for readers of
that key first: `rg -l 'budget_capped_fraction' --type py bench tools tests docs`).

**Verify (negative control)**: fails pre-fix.

### Step 2: Switch the collection and labels

Query `tjs_open_termination_reason()`, `tjs_open_graph_censored()`, `tjs_open_graph_examined()`
alongside `candidates_examined`. Update the docstring (lines 7-8), point labels (`:155`) and the
honesty note (`:171`) to the censored vocabulary. Drop or alias `budget_capped_fraction` per the
Step-1 decision.

**Verify**: focused tests pass; `make test && make lint` green.

### Step 3: Live small-scale validation on the stock image

Build a tiny deterministic corpus in `tridb/pg17-unfork:dev` (reuse the loader/fixture patterns
from `test/tjs_pg_tr1_test.sql` or the bench's own setup path) and run the sweep at two constructed
points: one with a huge scan budget (expect `stream_end_unknown_fraction` high — tiny corpus
exhausts) and one with `tjs.graph_work_budget` set tiny via `SET` (expect
`graph_censored_fraction = 1.0`). This proves the new fractions move.

**Verify**: both constructed points report the expected fractions; record the transcript in the
commit message or a scratch note referenced by it.

### Step 4: Doc addendum

Append to `docs/sm4_seedless_stock_v0.1.0.md`: dated note that (a) the historical
`budget_capped_fraction` values mean "stream ended before term_cond" under the pre-074 contract,
(b) the metric is replaced by the two new fractions, (c) any future 1M re-measure must use them.
If the 1M corpus + loaded engine are ALREADY present locally, you may additionally re-run the
sweep and append the new curve clearly labeled; otherwise state the re-measure is pending and on
what.

**Verify**: `rg 'stream_end_unknown_fraction|graph_censored_fraction' bench docs tests` finds
code, docs, and tests; `git diff --check` clean.

## Done criteria

- [ ] The bench queries reason + graph-censor + graph-examined; the vacuous metric is gone/aliased.
- [ ] Negative-control test failed pre-fix; live constructed points move both fractions.
- [ ] Doc addendum re-interprets history without rewriting it.
- [ ] Host tests/lint green; only in-scope files changed.

## STOP conditions

- A consumer of `budget_capped_fraction` (grep in Step 1) would break and the fix is out of scope
  — report it, don't edit out-of-scope files.
- The 1M corpus is absent and a step seems to require it — it doesn't; the at-scale re-run is
  optional and gated.

## Maintenance notes

Any new operator disclosure (like 077's censor flag) must be threaded into this bench in the same
change that adds it; a disclosure metric that cannot fire is worse than none.

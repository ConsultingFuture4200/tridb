# Plan 027: CI + test-suite hardening — engine layer gated nightly, run-all mode, and the four missing high-value tests

> **Executor instructions**: Follow step by step; verify each step. On any STOP condition, stop and
> report. Update your row in `advisor-plans/README.md` when done (unless a reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat e345998..HEAD -- .github/workflows/ci.yml Makefile test/ tests/ baseline/sm2.py`
> Plans 024/025 add ENGINE_TESTS entries — additive Makefile drift is expected; excerpt mismatches are not.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none hard; the >256-batch test (Step 4) needs the CURRENT engine image with the
  filter-first body (present at e345998), and should be re-run after 024 lands.
- **Category**: tests / dx
- **Planned at**: commit `e345998`, 2026-07-03

## Why this matters

The engine layer — the C that IS the product — has no continuous gate: `.github/workflows/ci.yml`
runs the engine build + `make test-all` ONLY on manual `workflow_dispatch`, so a patch that applies
cleanly but breaks runtime merges green (this exact mechanism let three AM suites rot unnoticed
until 2026-07-03, fixed in `197f447`). `make graph-test` fail-fasts, so one early failure hides all
downstream suite results on an expensive run. And four specific high-value coverage gaps exist:
the filter-first multi-batch drain path (>256 qualifying rows — the at-scale story) has zero
coverage; `baseline/sm2.py`'s corpus rebuild — the SM-2 fairness linchpin — is the one of three
generators no test pins byte-identical; its load batching is untested at boundary sizes; CI also
double-fires on PR branches and re-downloads the heavy dependency tree every run.

## Current state

- `.github/workflows/ci.yml` — `on: push: {}` + `pull_request: {}` + `workflow_dispatch: {}` (no
  branch filters → double-fire); comment at ~line 9: engine job "gated behind workflow_dispatch —
  far too heavy for per-PR runners"; `engine:` job has `if: github.event_name == 'workflow_dispatch'`;
  `actions/setup-python` without `cache: pip`; actions pinned by SHA (keep that).
- `Makefile:57-63` — both ENGINE_TESTS and AM_TESTS loops end `|| exit 1` (fail-fast, no accumulator).
- `test/tjs_filter_first_test.sql` — corpus 2000 rows, edges `add_edge(1,{10,20,30,40})`; max
  examined asserted = 3. The C drain fetches `SPI_cursor_fetch(portal, true, 256)` in a loop — the
  second iteration + per-batch scratch reset are never exercised.
- `tests/test_bench_corpus_shared.py:48` — `test_shared_entities_match_live_rebuild` pins
  `bench/live_report.rebuild_corpus` against `tools/bench_corpus_shared.build_corpus`. Nothing
  imports `baseline.sm2` anywhere in `tests/`.
- `baseline/sm2.py` — `rebuild_corpus(manifest, seed)` (numpy regeneration; must be byte-identical
  to the other two); load batch sizes: Milvus 1000, Neo4j 10_000, Postgres 500. `baseline/harness.py`
  holds the connection helpers; heavy clients import lazily.
- Python conventions: pytest under `tests/`, plain functions + asserts, `make test` runs via the
  repo venv. SQL engine tests: DO-block ASSERT style, wired in `Makefile` ENGINE_TESTS.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Python | `make test && make lint` | all pass, lint clean |
| YAML sanity | `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` | exit 0 |
| One engine test | `bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_filter_first_test.sql` | ALL PASS |
| Full suite (run-all mode) | `KEEP_GOING=1 make graph-test` | prints per-suite results + summary |

## Scope

**In scope:** `.github/workflows/ci.yml`; `Makefile` (graph-test loops only);
`test/tjs_filter_first_test.sql` (add one assertion block); `tests/test_bench_corpus_shared.py`
(extend); new `tests/test_baseline_sm2_load.py`; README status row.

**Out of scope:** the v0/v1 parity test (owned by plan 025 Step 1 — do not duplicate);
`scripts/*_test.sh` internals; any engine C; adding new CI jobs beyond the schedule/gating changes.

## Git workflow
Branch `advisor/027-ci-test-hardening`; `test:`/`ci:` commit style; do NOT push.

## Steps

### Step 1: CI — nightly engine gate, caching, dedup
In `ci.yml`: (a) add `schedule: [{cron: "17 6 * * *"}]` to `on:` and widen the engine job condition
to `if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'`; (b) add
`cache: 'pip'` + `cache-dependency-path: requirements.lock` to every `setup-python`; (c) scope
`push:` to `branches: [master]` and add a top-level `concurrency: {group: ci-${{ github.ref }},
cancel-in-progress: true}`. Keep all action SHAs untouched.
**Verify**: YAML sanity command → exit 0; `git diff .github/workflows/ci.yml` shows only these three concerns.

### Step 2: Makefile run-all mode
Rework the two `graph-test` loops: default behavior UNCHANGED (fail fast); with `KEEP_GOING=1`,
record failures (`FAILED="$$FAILED $$t"`) and continue, print a `=== FAILED SUITES: ... ===` summary,
exit nonzero if any failed.
**Verify**: `KEEP_GOING=1 make graph-test IMAGE=tridb/msvbase:dev` completes past any single failure (temporarily `exit 1` in a scratch copy of one harness to prove accumulation if all suites are green; revert).

### Step 3: Baseline-corpus byte-identity pin
Extend `tests/test_bench_corpus_shared.py`: build a small manifest via `build_corpus`, call
`baseline.sm2.rebuild_corpus(public_manifest, seed)` (import via the same sys.path shim the module's
`__main__` uses — see `baseline/sm2.py` imports; heavy clients are lazy so import is safe), assert
entities (ids, timestamps, embeddings arrays exactly) and edges equal the `_entities`/`_edges` of
`build_corpus`. Model after `test_shared_entities_match_live_rebuild`.
**Verify**: `make test` → passes, new test included.

### Step 4: Filter-first multi-batch engine test
In `test/tjs_filter_first_test.sql` add an assertion block: create hub 2 with >600 edges
(`SELECT graph_store.add_edge(2, g) FROM generate_series(1000,1700) g` — note each dst must exist in
`entities`, corpus is 2000 rows so ids 1000-1700 are valid), run `tjs(...,'filter_first')` with an
always-true filter and k=5; assert (a) `tjs_candidates_examined()` = 701 (the full drain — proves >2
fetch batches), (b) the answer equals the same query under `vector_first` with a large term_cond,
(c) a second identical call returns identical rows (scratch-reset stability).
**Verify**: `bash scripts/graph_test.sh tridb/msvbase:dev test/tjs_filter_first_test.sql` → ALL PASS incl. the new block.

### Step 5: Baseline load-batch slicing test
New `tests/test_baseline_sm2_load.py`: monkeypatch/fake the client objects (a list-capturing fake
`Collection`/session/cursor) and drive `load_milvus`/`load_neo4j`/`load_postgres` with corpora of
sizes {0(skip if guarded), 1, batch, batch+1, 2*batch+3}; assert the captured inserts reassemble to
exactly the input ids with no overlap/omission. Import-time env parsing: also assert
`BASELINE_ANN_FANOUT=notanint` raises `ValueError` cleanly on module reload (documenting behavior).
**Verify**: `make test && make lint` green.

## Test plan
Steps 3-5 ARE the tests; Step 2 verified by forced-failure experiment; Step 1 by YAML parse + next
scheduled run (note in README row that first nightly is unobserved).

## Done criteria
- [ ] ci.yml: schedule trigger + widened engine condition + pip cache + branch filter/concurrency; YAML parses
- [ ] `KEEP_GOING=1` mode works; default fail-fast behavior byte-compatible for green runs
- [ ] New/extended tests pass: baseline byte-identity, load slicing, >256 drain block
- [ ] `make test && make lint` and targeted engine tests green
- [ ] README status row updated

## STOP conditions
- The >256 drain assertion (a) disagrees with the measured examined count — that is a REAL finding
  (batch-boundary bug in the drain); report with the observed number, do not adjust the assert.
- `baseline.sm2.rebuild_corpus` is NOT byte-identical to `build_corpus` — real finding; report.
- ci.yml uses a structure that makes the condition edit ambiguous (drifted) — report.

## Maintenance notes
When plan 025 flips drivers to v1, Step 4's `add_edge` seeding goes through the compat shim — the
assertion values must not change. The nightly engine job's cost is ~1 image build/day; if runner
minutes matter, add `paths` filters later. Follow-up deliberately deferred: refactoring the 16
harness scripts onto a shared `scripts/lib/pg_in_image.sh` (tracked in advisor-plans README backlog).

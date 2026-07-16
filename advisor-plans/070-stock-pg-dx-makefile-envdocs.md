# Plan 070: Make the stock-PG engine layer reachable — Makefile target + env-var reference

> **Executor instructions**: Follow step by step; run every verification. STOP conditions halt you.
> SKIP updating advisor-plans/README.md (reviewer maintains it).
>
> **Drift check (run first)**: `git diff --stat 4d54c11..HEAD -- Makefile docs/INSTALL_stock_pg.md`
> If changed, compare "Current state" to live code; mismatch = STOP.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx / docs
- **Planned at**: commit `4d54c11`, 2026-07-16

## Why this matters

D2's headline deliverable is the stock-PG-16/17 engine layer (graph AM + `tjs_pg`), but it is only
reachable by reading `.github/workflows/ci.yml` or invoking `scripts/pg17_graph_test.sh` by hand:
there is **no Makefile target** for it (the fork layer has `make graph-test`/`make smoke-test`), and
the new harness env vars (`WD_ENGINE_DIALECT`, `WD_SLICE`, `WD_EMB`, the `WH_`/`WD_` gate keys) are
**undocumented** — a runner who forgets `WD_ENGINE_DIALECT=stock` silently benchmarks the fork. Two
small, zero-risk additions close the "a new dev can build/test the stock layer on the first try" gap
(`rules/safety.md`'s test-bootstrap rule, currently unmet for the stock layer).

## Current state

- `Makefile:1` `.PHONY` lists `test lint graph-test smoke-test test-all ...` — no `stock-graph-test`.
  `graph-test:` (`Makefile:71`) is the fork-image target; mirror its shape for the stock image.
- The stock suite is run in `.github/workflows/ci.yml` job `stock-pg` via a shell loop over
  `test/graph_*.sql` + `test/tjs_pg_test.sql` calling `bash scripts/pg17_graph_test.sh <image> <sql>`
  after `docker build ... scripts/pg17/`.
- `docs/INSTALL_stock_pg.md` — `grep -c 'WD_ENGINE_DIALECT'` returns 0 (no env reference); there is
  no `.env.example` for the harness.
- Env vars the harnesses read (from `bench/wikidata_h2h.py` `WCfg` + `oracle_meta_from_env`, and the
  loaders): `WD_ENGINE_DIALECT` (fork|stock, default fork), `WD_SLICE`, `WD_EMB`, `WD_DIM`,
  `WD_ENGINE`/`WD_ENGINE_DB`/`WD_ENGINE_TABLE`, the baseline `WD_MILVUS_*`/`WD_NEO4J_*`/`WD_PG*`, and
  the gate keys `WH_ENGINE_EDGES`|`WD_ENGINE_EDGES`, `WH_NEO4J_EDGES`|`WD_NEO4J_EDGES`,
  `WH_HNSW_HEALTHY_BUILDS`|`WD_HNSW_HEALTHY_BUILDS`, `WH_HNSW_TOTAL_BUILDS`|`WD_HNSW_TOTAL_BUILDS`,
  `WH_BOUNDARY_PARITY`. (Read the actual files to confirm the list before documenting — do not
  invent vars.)

## Steps

1. **Makefile `stock-graph-test` target.** Add to `.PHONY` and define a target that builds the stock
   image and runs the pure-SQL graph suites + `test/tjs_pg_test.sql` on it — mirror the `stock-pg`
   CI job's suite list exactly (read `.github/workflows/ci.yml` for the canonical list so the
   Makefile and CI can't drift). Parametrize the PG major with a variable defaulting to 17, e.g.:
   ```makefile
   PG_MAJOR ?= 17
   STOCK_IMAGE ?= tridb/pg$(PG_MAJOR)-unfork:dev
   stock-graph-test:
   	docker build --build-arg PG_MAJOR=$(PG_MAJOR) -t $(STOCK_IMAGE) scripts/pg17/
   	@for t in <the exact suite list from ci.yml stock-pg>; do \
   	  echo "=== $$t (stock PG$(PG_MAJOR)) ==="; \
   	  bash scripts/pg17_graph_test.sh $(STOCK_IMAGE) $$t || exit 1; \
   	done
   ```
   Use the SAME suite list ci.yml uses (the `test/graph_*` set + `test/tjs_pg_test.sql`) — copy it,
   do not re-derive. Match the Makefile's existing tab/indentation and recipe style.

2. **Env-var reference in `docs/INSTALL_stock_pg.md`.** Add a short "Harness environment variables"
   section: a table of the vars above with one-line meanings and defaults, emphasizing that
   `WD_ENGINE_DIALECT=stock` selects the pgvector engine (default `fork` benchmarks the MSVBASE
   fork) and that the gate accepts either the `WH_` or `WD_` prefix (plan 065). Keep it factual and
   derived from the actual code — read `bench/wikidata_h2h.py` `WCfg` for the exact names/defaults.

## Verification

1. `make stock-graph-test` runs (builds the image if needed and runs the suites) → ends with each
   suite's `ALL PASS` / green and exits 0. (This does docker builds; it is the real gate.)
   - If the docker build/run is too slow or docker is unavailable in the executor environment,
     verify instead that the target PARSES and lists the right suites: `make -n stock-graph-test`
     prints the docker build + the per-suite `pg17_graph_test.sh` invocations for the ci.yml list,
     and `make stock-graph-test PG_MAJOR=17 2>&1 | head` starts building — then mark the full run
     "verify on a docker host" in NOTES. Do NOT claim ALL PASS without actually seeing it.
2. `grep -c 'stock-graph-test' Makefile` ≥ 2 (the .PHONY entry + the target).
3. `grep -c 'WD_ENGINE_DIALECT' docs/INSTALL_stock_pg.md` ≥ 1.
4. `make lint` and `make test` still green (you changed no Python — this just confirms nothing broke).

## Done criteria

- `make stock-graph-test` exists and, on a docker host, builds + runs the stock suite green (or
  `make -n` shows the correct invocations if docker-gated in your env — note which).
- `docs/INSTALL_stock_pg.md` documents the harness env vars incl. `WD_ENGINE_DIALECT`.

## Out of scope / do NOT touch

- `.github/workflows/ci.yml` (the target mirrors it; don't change CI).
- Any Python, C, or the pg17 scripts themselves.
- advisor-plans/, any file other than `Makefile` and `docs/INSTALL_stock_pg.md`.

## STOP conditions

- If the ci.yml `stock-pg` suite list can't be located, STOP and report (don't guess the list).
- If adding the target would require changing `scripts/pg17_graph_test.sh` (it shouldn't — the
  target only calls it), STOP and report.

## Maintenance note

The `stock-graph-test` suite list must stay in sync with `ci.yml`'s `stock-pg` job. A reviewer adding
a stock-PG SQL suite should add it to both. Consider a follow-up to source the list from one place.

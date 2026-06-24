# Plan 001: One command (and CI) verifies the whole engine, not just the Python layer

> **Executor instructions**: Follow this plan step by step. Run every verification command
> and confirm the expected result before moving on. If a "STOP conditions" item occurs, stop
> and report — do not improvise. When done, update this plan's row in `plans/README.md`.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- Makefile README.md CLAUDE.md scripts/graph_test.sh scripts/smoke_test.sh`
> If any of those changed since `cb097db`, compare the "Current state" excerpts below against
> the live files before proceeding; on a mismatch, treat it as a STOP condition.

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: dx / tests
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
`make test` runs only the two Python unit tests. The engine's real tests — the graph-store
extension, the tri-modal compositions, the smoke test — live in `test/*.sql` and run only by
hand via `scripts/graph_test.sh` against a Docker image. A contributor can change
`src/graph_store_ext/graph_store.c`, run `make test`, see green, and have broken the graph
store. There is no CI. After this plan, one command (`make test-all`) verifies the engine and
CI runs the cheap layer on every PR.

## Current state
- `Makefile` (the whole file today):
  ```make
  .PHONY: test lint baseline-up baseline-down seed clean
  test:
  	pytest tests/ -q
  lint:
  	ruff check . && ruff format --check .
  ...
  ```
- `scripts/graph_test.sh` — usage `graph_test.sh [image] [sqlfile]`; builds the extension via
  PGXS inside the `tridb/msvbase:dev` container and runs the given SQL file with
  `psql -v ON_ERROR_STOP=1`. It **exits non-zero if any assertion RAISEs** (the SQL tests use
  `DO $$ ... RAISE EXCEPTION ... $$`). Default sqlfile is `test/graph_store_test.sql`.
- `scripts/smoke_test.sh` — builds nothing; runs `test/smoke.sql` (vector + relational) in the
  image; prints `[smoke_test] PASS` on success.
- Engine SQL suites that must be covered: `test/graph_store_test.sql`,
  `test/trimodal_compose.sql`, `test/trimodal_early_term.sql`, `test/fork_distance_probe.sql`.
- The Docker image `tridb/msvbase:dev` is produced by `scripts/x86build.sh --docker` (heavy:
  ~9.5 GB, multi-minute build). It already exists on the dev machine but will NOT exist on a
  fresh CI runner.
- There is no `.github/` directory.
- Repo conventions: Bash scripts start with `set -euo pipefail`, use `log()`/`die()` helpers
  (see `scripts/x86build.sh:38-39`). Python env via `uv` (there is a `.venv`); tests are
  `pytest`, lint/format is `ruff`.

## Commands you will need
| Purpose | Command | Expected on success |
|---|---|---|
| Python tests | `cd /home/bob/code/tridb && pytest tests/ -q` | all pass |
| Lint | `ruff check . && ruff format --check .` | exit 0 |
| Engine test (one suite) | `bash scripts/graph_test.sh tridb/msvbase:dev test/graph_store_test.sql` | `ALL TESTS PASSED`, exit 0 |
| Image present? | `docker image inspect tridb/msvbase:dev` | exit 0 when built |
| Bash syntax | `bash -n scripts/<file>.sh` | exit 0 |

## Scope
**In scope**: `Makefile`, `.github/workflows/ci.yml` (create), `README.md`, `CLAUDE.md`.
**Out of scope**: the SQL test files (don't change assertions), `scripts/graph_test.sh` and
`scripts/smoke_test.sh` internals (only call them), `scripts/x86build.sh`.

## Git workflow
- Branch: `advisor/001-verification-baseline`.
- Conventional commits, matching `git log` style (e.g. `dx(test): add make test-all + CI`).
- Do NOT push or open a PR unless the operator asks.

## Steps

### Step 1: Add `graph-test`, `smoke-test`, and `test-all` Makefile targets
Add to `Makefile` (extend `.PHONY`). `graph-test` must guard on the image and run all four
engine suites, failing on the first failure:
```make
IMAGE ?= tridb/msvbase:dev
ENGINE_TESTS := test/graph_store_test.sql test/trimodal_compose.sql \
                test/trimodal_early_term.sql test/fork_distance_probe.sql

graph-test:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker"; exit 1; }
	@for t in $(ENGINE_TESTS); do \
	  echo "=== $$t ==="; bash scripts/graph_test.sh $(IMAGE) $$t || exit 1; done

smoke-test:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || \
	  { echo "image $(IMAGE) not built — run scripts/x86build.sh --docker"; exit 1; }
	bash scripts/smoke_test.sh

test-all: test lint smoke-test graph-test
```
**Verify**: `make graph-test` → each suite prints its PASS lines and `ALL TESTS PASSED` /
`STRUCTURE VERIFIED`, overall exit 0. `make test-all` → all of pytest, ruff, smoke, graph
green. (Both require the image; if it's missing, build it first with
`scripts/x86build.sh --docker`.)

### Step 2: Add CI workflow — fast layer on every PR, engine layer gated
Create `.github/workflows/ci.yml` with two jobs:
- `python` (every push/PR): set up Python, `pip install -r requirements.txt`, `make test`,
  `ruff check . && ruff format --check .`. Fast, no Docker.
- `engine` (manual `workflow_dispatch` + optional schedule, NOT every PR — the image build is
  too heavy for per-PR runners): build the image via `scripts/x86build.sh --docker`, then
  `make graph-test`.

Target shape:
```yaml
name: CI
on: { push: {}, pull_request: {}, workflow_dispatch: {} }
jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - run: pytest tests/ -q
      - run: ruff check . && ruff format --check .
  engine:
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch'
    steps:
      - uses: actions/checkout@v4
        with: { submodules: false }
      - run: scripts/x86build.sh --docker
      - run: make graph-test
```
**Verify**: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"`
exits 0 (valid YAML). (CI itself can only be verified once pushed — out of scope here.)

### Step 3: Document the test split
In `README.md` ("Build & test") and `CLAUDE.md` ("Build & test commands"), state plainly:
- `make test` / `make lint` — Python + lint only (fast, no Docker).
- `make graph-test` / `make smoke-test` / `make test-all` — the engine (needs the
  `tridb/msvbase:dev` image from `scripts/x86build.sh --docker`).
**Verify**: `grep -n "make test-all\|make graph-test" README.md CLAUDE.md` returns matches.

## Test plan
No new automated tests; this plan *wires existing* tests into one command + CI. Confirm the
existing suites still pass via `make test-all` (requires the image).

## Done criteria
- [ ] `make test` still passes (pytest, 2 suites).
- [ ] `make graph-test` runs all four engine suites green when the image exists, and exits
      non-zero with a clear message when it doesn't.
- [ ] `make test-all` exists and runs test + lint + smoke + graph.
- [ ] `.github/workflows/ci.yml` exists, is valid YAML, runs pytest + ruff on PRs and the
      engine build only on `workflow_dispatch`.
- [ ] README and CLAUDE.md document which command covers what.
- [ ] `plans/README.md` status row updated.

## STOP conditions
- `scripts/graph_test.sh` exit codes don't actually reflect pass/fail (e.g. a failing suite
  exits 0) — STOP; the CI gate would be meaningless. Report so graph_test.sh can be fixed first.
- The per-PR image build is unavoidable on the only available runners (no `workflow_dispatch`
  option fits the team's setup) — STOP and propose a self-hosted or scheduled approach rather
  than a 9.5 GB build on every PR.

## Maintenance notes
- When a new `test/*.sql` engine suite is added, append it to `ENGINE_TESTS` in the Makefile.
- A reviewer should confirm the `engine` CI job is NOT on the per-PR path (cost).
- Deferred: caching the built image in a registry so the `engine` job can pull instead of
  rebuild — worth doing once a registry is available.

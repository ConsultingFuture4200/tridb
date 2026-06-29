# Plan 014: Run the full engine verification target in manual CI

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report instead of improvising. When done,
> update this plan's status row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat fb3f08b..HEAD -- .github/workflows/ci.yml Makefile`
> If either in-scope file changed since this plan was written, compare the
> excerpts below with the live code before proceeding. A mismatch is a STOP
> condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx / tooling
- **Planned at**: commit `fb3f08b`, 2026-06-26

> **Prerequisite (RESOLVED — read before running).** An earlier `plans/README.md` status marked this
> plan BLOCKED on a `crash_recovery` suite-ordering flake (`make graph-test` could fail flakily under
> CI/host load when `crash_recovery_test.sh` scenario 2 ran last). **That flake was fixed 2026-06-26 in
> commit `b4513c1` (DEV-1234 P1b)** — the harness holds the doomed txn open with `pg_sleep(3600)` and
> uses a ~180s liveness-checked readiness budget (`scripts/crash_recovery_test.sh:104-128`; 🟢 in
> `docs/STATUS.md`). It is therefore SAFE to wire `make test-all` (which includes `graph-test`) into
> CI: you are not enabling a known-flaky suite. If, when you run it, `crash_recovery_test.sh` scenario 2
> still times out or self-commits the doomed txn, STOP and report — that would mean the fix regressed,
> which is out of this plan's scope to re-fix.

## Why this matters

The manual GitHub Actions engine job builds the MSVBASE image and runs
`make graph-test`, but the repo's documented full verification target is
`make test-all`, which includes Python tests, lint, smoke-test, and graph-test.
Because the engine workflow is already manual and expensive, it should run the
same fail-loud full gate that maintainers use for release confidence.

## Current state

- `.github/workflows/ci.yml` has a manual engine job:

  ```yaml
  .github/workflows/ci.yml:22
  engine:
  .github/workflows/ci.yml:29
  - run: scripts/x86build.sh --docker
  .github/workflows/ci.yml:30
  - run: make graph-test
  ```

- `Makefile` defines the full verification target:

  ```make
  Makefile:31
  smoke-test:
  Makefile:37
  test-all: test lint smoke-test graph-test
  ```

- Fast push/PR CI already runs Python tests and lint:

  ```yaml
  .github/workflows/ci.yml:18
  - run: pip install -r requirements.txt
  .github/workflows/ci.yml:19
  - run: pytest tests/ -q
  .github/workflows/ci.yml:20
  - run: ruff check . && ruff format --check .
  ```

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Fast local tests | `make test` | all tests pass |
| Lint | `make lint` | ruff check and format check pass |
| Engine target check | `make test-all` | exits 0 when `tridb/msvbase:dev` exists |

## Scope

**In scope**:
- `.github/workflows/ci.yml`
- Optional comments in `Makefile` if wording needs clarification.

**Out of scope**:
- Adding scheduled CI.
- Adding baseline stack or `make sm2` to CI.
- Changing Docker build scripts.

## Git workflow

- Branch: `advisor/014-engine-ci-test-all`
- Commit message style: `ci: run full engine verification on dispatch`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Change the manual engine job to run `make test-all`

In `.github/workflows/ci.yml`, replace the engine job's `make graph-test` step
with `make test-all`.

Keep `scripts/x86build.sh --docker` first; `test-all` needs the image for
`smoke-test` and `graph-test`.

**Verify**: `rg -n "make graph-test|make test-all" .github/workflows/ci.yml`
shows no `make graph-test` in the engine job and one `make test-all`.

### Step 2: Avoid duplicate work only if it is worth the complexity

It is acceptable for the manual engine job to rerun Python tests and lint even
though the fast `python` job also runs them. The engine job is dispatch-only and
the simpler `make test-all` command is less likely to drift from local practice.

Do not add job dependencies or conditionals unless the maintainer explicitly
wants a more complex workflow.

**Verify**: Re-read `.github/workflows/ci.yml` and confirm the `python` job is
unchanged.

### Step 3: Run local fast gates

Run the fast local gates:

- `make test`
- `make lint`

If the engine image `tridb/msvbase:dev` exists locally, also run `make test-all`.
If the image is absent, do not build it unless the operator asked for it; record
that `make test-all` was not run locally.

**Verify**: Commands exit 0, or the only skipped command is `make test-all`
because the image is absent.

## Test plan

- Search check for the workflow command.
- `make test` and `make lint`.
- Optional `make test-all` when image is available.

## Done criteria

- [ ] Manual engine CI runs `make test-all` after `scripts/x86build.sh --docker`.
- [ ] Fast `python` CI job remains intact.
- [ ] `make test` and `make lint` pass locally.
- [ ] `make test-all` was run locally if the image exists; otherwise the final note says it was skipped.
- [ ] `plans/README.md` row for plan 014 is updated.

## STOP conditions

- `make test-all` requires services or credentials that are not available in GitHub Actions.
- The workflow already changed to a matrix or reusable workflow; stop and adapt the plan before editing.
- The Docker image build step changes tag names away from `tridb/msvbase:dev`.

## Maintenance notes

Keep `make sm2` out of CI unless maintainers explicitly provision the baseline
stack. It requires Milvus, Neo4j, Postgres, and live client dependencies and is
better treated as a benchmark run, not a correctness gate.

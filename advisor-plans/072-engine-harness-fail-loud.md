# Plan 072: Make engine test harnesses preserve build failures

> **Executor instructions**: Follow this plan step by step. Run every verification command and
> confirm the expected result before moving on. Do not update `advisor-plans/README.md`; the advisor
> maintains the index. Do not run `scripts/gx10build.sh` off a GX10.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- scripts/graph_test.sh scripts/pg17_graph_test.sh scripts/tjs_test.sh scripts/graph_am_test.sh tests/`
> If an in-scope harness changed, re-read its container shell and compare it with the excerpts below.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / tests
- **Planned at**: commit `a780b46`, 2026-07-15

## Why this matters

Three engine harnesses pipe `make` through `tail` inside a shell that lacks `pipefail`. A failed
compile can therefore be replaced by `tail`'s zero status and reported as a passing test run. This
invalidates every later stock/fork engine verification that relies on those scripts.

## Current state

- `scripts/graph_test.sh:26,32-33`, `scripts/pg17_graph_test.sh:31,36-38`, and
  `scripts/tjs_test.sh:23,28-29` start their inner container shell with `set -e`, then use the shape:

  ```bash
  make ... 2>&1 | tail -20
  ```

  `set -e` does not inspect the non-final pipeline process without `pipefail`.
- `scripts/graph_am_test.sh:28-34` is the local fail-loud exemplar: it writes build output to a log,
  checks `make` directly, tails the log on failure, and exits nonzero.
- The host shells already use `set -euo pipefail`; the defect is specifically inside `bash -c` in
  the container.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Syntax | `bash -n scripts/{graph_test,pg17_graph_test,tjs_test,graph_am_test}.sh` | exit 0 |
| Focused test | `.venv/bin/pytest tests/test_engine_harness_fail_loud.py -q` | all pass |
| Host suite | `make test && make lint` | exit 0 |

## Scope

**In scope**:
- `scripts/graph_test.sh`
- `scripts/pg17_graph_test.sh`
- `scripts/tjs_test.sh`
- `tests/test_engine_harness_fail_loud.py` (create)

**Out of scope**:
- Operator, access-method, SQL, Dockerfile, or benchmark changes.
- Changing image names or test-suite membership.
- Claiming fork/GX10 build success without running on the appropriate hardware.

## Git workflow

Use the assigned Linear branch `dustin/dev-NNNN`; if no issue exists, do not invent an issue number.
Use a conventional commit such as `fix(test): preserve engine build failures`. Do not push unless
instructed.

## Steps

### Step 1: Add a static regression test

Create `tests/test_engine_harness_fail_loud.py`. Read each inner `bash -c` payload and assert that no
unguarded `make ... | tail` pipeline remains. Also assert each harness either enables `pipefail` in
that same inner shell or uses the established log-file/explicit-`|| exit` pattern. Test all three
files by name so deletion or omission cannot make the test vacuously pass.

**Verify**: run the focused test before the fix; it must fail on all three named harnesses. This is
the required negative control. If it passes against the current code, STOP because the assertion is
not detecting the defect.

### Step 2: Make each build command fail loudly

Mirror `scripts/graph_am_test.sh`: capture `make` output to a temporary/container-local log, tail it
for concise output, and explicitly exit nonzero when `make` fails. Preserve successful output and
all later SQL execution. A same-shell `set -o pipefail` solution is acceptable only if it is visibly
inside the container payload and the regression test proves it.

**Verify**: syntax and focused test commands above exit 0.

### Step 3: Prove failure propagation

For each harness, exercise its inner build shape with a deliberate failing command (`false` or a
nonexistent make target) without committing that perturbation. Confirm the shell exits nonzero and
shows the tail of the log. Restore the real command and run any engine harness whose image is
available; report unavailable images as unrun, not passed.

**Verify**: three deliberate failures return nonzero; `git diff --check` exits 0.

## Test plan

- Static test detects all three original pipelines and requires fail-loud behavior in each file.
- `bash -n` covers quoting in nested shell payloads.
- Manual negative control proves runtime exit propagation.
- Run `make test && make lint`; Docker engine tests are additional and environment-gated.

## Done criteria

- [ ] No unguarded `make ... | tail` remains in the three in-scope harnesses.
- [ ] A deliberate build failure makes each harness's inner shell return nonzero.
- [ ] Focused test, shell syntax, `make test`, `make lint`, and `git diff --check` pass.
- [ ] `git status --short` contains only the four in-scope paths.

## STOP conditions

- An in-scope script has been structurally replaced since `a780b46`.
- Preserving failure requires changing the image or test contract.
- The negative-control test cannot detect the current bug.
- Any step would require claiming a GX10-only build passed off-target.

## Maintenance notes

New container harnesses should use the same explicit log-and-status pattern. Reviewers should check
the *inner* shell, because host-level `pipefail` does not cross `bash -c`.

# Plan 050: Host unit tests for DEV-1354 value-claim harnesses

> **Executor instructions**: Python only; no Docker. Update index when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- bench/wiki_consistency.py bench/wiki_h2h.py bench/wiki_fusion.py tools/wiki_reader.py tests/`

## Status
- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

STATUS’s fusion **speed** and **consistency** claims are enforced by harness gates and pure helpers
that have **zero** `tests/` coverage. CI runs `pytest tests/` only. A silent gate regression only
shows up on the next Spark re-run. Contrast: extract/linkpredict already have host tests.

## Current state

- Missing: `tests/test_wiki_consistency.py`, `test_wiki_h2h.py`, `test_wiki_fusion.py`, `test_wiki_reader.py`
- Present pattern: `tests/test_wiki_linkpredict.py` — pure logic, no network
- Critical pure functions:
  - `bench/wiki_consistency.py`: `torn()`, `vec()`, scenario bookkeeping
  - `bench/wiki_h2h.py`: `publication_gate()` (~558–648)
  - `tools/wiki_reader.py`: RRF/`related_fused` helpers if importable without full Reader init

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| New tests | `pytest tests/test_wiki_h2h.py tests/test_wiki_consistency.py -q` | all pass |
| Full | `make test && make lint` | exit 0 |

## Scope

**In scope:** new test modules under `tests/`; tiny refactors **only if required** to import pure
helpers (extract function to module level without changing behavior).

**Out of scope:** live Docker h2h; inventing measured latency numbers as fixtures; full wiki_reader HTTP integration.

## Git workflow
- Branch: `advisor/050-dev1354-unit-tests`
- Commit: `test(bench): unit-test wiki consistency/h2h gates (advisor 050)`

## Steps

### Step 1: publication_gate matrix

Create `tests/test_wiki_h2h.py`:

- Import `publication_gate` (or the smallest pure function that encodes gates).
- Synthetic result dicts covering: graph set mismatch, timer boundary fail, examined==0, recall mismatch,
  healthy pass case.
- Assert raise/return codes match harness contract (read live function docstring first).

**Verify**: `pytest tests/test_wiki_h2h.py -q`

### Step 2: consistency helpers

Create `tests/test_wiki_consistency.py`:

- `torn()` true/false cases from fixed multi-store vs TriDB shape fixtures (no real DBs).
- `vec()` round-trip if pure.

**Verify**: pytest green.

### Step 3: wiki_reader pure helpers (optional but recommended)

If `related_fused` / RRF merge can be tested with toy score lists without loading 7M CSR, add
`tests/test_wiki_reader.py` with 2–3 cases. If import pulls heavy deps, extract a pure function first.

**Verify**: `make test && make lint`

## Test plan
- Characterization only — pin current gate semantics; if a gate is intentionally changed later, update tests in the same PR.

## Done criteria
- [ ] `publication_gate` (or equivalent) covered for blocker + pass cases
- [ ] At least one consistency pure helper covered
- [ ] `make test` / `make lint` green
- [ ] No live network/Docker in new tests
- [ ] Index DONE

## STOP conditions
- Helpers are nested and unimportable without massive refactor — extract minimal pure functions only; do not rewrite harness architecture.
- Gate logic is in a shell script only — test the Python function that shell calls.

## Maintenance notes
- When 1M fusion unblocks (plan 043), add a fixture for “healthy build” gate if new fields appear.

# Plan 008: Harden the Python tooling — input validation, resource cleanup, harness tests

> **Executor instructions**: Follow step by step; run every verification command. On a STOP
> condition, stop and report. Update this plan's row in `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- tools/seed_corpus.py baseline/harness.py tests/`
> If changed, re-read the cited files before editing; mismatch with excerpts = STOP.

## Status
- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug / tests
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
Small robustness gaps in the Python tooling produce confusing crashes and leave the baseline
harness's merge logic (the SM-1 measurement surface) untested:
1. `tools/seed_corpus.py` crashes unhelpfully on degenerate CLI input — `--dim 0`
   (division by zero in normalization), `--entities 0` (`rng.choice` with a negative size), and
   a time window narrower than the query span (`rng.integers(low >= high)`).
2. `baseline/harness.py`'s cleanup runs three `close()`/`disconnect()` calls in one `finally`
   with no per-call guard — if the first raises, the other two leak connections.
3. The harness merge/expand/filter functions have zero unit tests; a regression there silently
   corrupts the baseline TriDB is measured against.

## Current state
- `tools/seed_corpus.py` (relevant lines):
  - `:18` `--dim` default 768, no lower bound. `:36-37`:
    ```python
    embedding = rng.standard_normal(args.dim).astype(np.float32)
    embedding /= np.linalg.norm(embedding)     # dim=0 -> norm 0 -> divide by zero -> '{}' embedding
    ```
  - `:53-54` `k = min(args.edges_per_node, args.entities - 1)`; with `--entities 0`, `k = -1`
    → `rng.choice(0, -1, replace=False)` raises.
  - `:72` `start_time = rng.integers(args.time_min, args.time_max - 28)` → `ValueError: low >= high`
    when `time_max - time_min < 28`.
  - `main()` has no argument validation after `parse_args()` (`:25`).
- `baseline/harness.py:441-446` (the `finally` block in `run()`):
  ```python
  finally:
      drivers["neo4j"].close()
      milvus_connections.disconnect(milvus_alias)
      drivers["postgres"].close()
  ```
- Tests today: `tests/test_seed_corpus.py`, `tests/test_join_order.py` (run by `make test` =
  `pytest tests/ -q`). No `tests/test_harness*.py`. The harness functions to cover:
  `merge` (`:339`), `run_query` (`:384`), `graph_expand`/`vector_topk`/`relational_filter`
  (locate via `grep -n 'def ' baseline/harness.py`).
- Conventions: `pytest`, `ruff` (`make lint`). Existing tests use `tmp_path` and subprocess /
  direct import (see `tests/test_seed_corpus.py` for the structural pattern).

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Python tests | `cd /home/bob/code/tridb && pytest tests/ -q` | all pass |
| Lint/format | `ruff check . && ruff format --check .` | exit 0 |
| Repro a seed crash | `python tools/seed_corpus.py --entities 0 --out /tmp/s` | (currently) a traceback |
| List harness defs | `grep -n '^def \|^    def ' baseline/harness.py` | the function list |

## Scope
**In scope**: `tools/seed_corpus.py`, `baseline/harness.py` (the `finally` block only),
`tests/test_seed_corpus.py` (extend), `tests/test_harness.py` (create).
**Out of scope**: the harness's live-system TODOs (DEV-1171 skeleton work — leave them), the SQL
tests, any C code.

## Git workflow
- Branch `advisor/008-python-hardening`; commits per concern (validation / cleanup / tests).
  Conventional commit style.

## Steps

### Step 1: Validate seed_corpus.py CLI inputs
In `main()` right after `args = parser.parse_args()`, validate and exit with a clear message
(`parser.error(...)`) when: `args.dim < 1`, `args.entities < 1`, `args.edges_per_node < 0`, or
`args.time_max - args.time_min < 30` (the query span is 30; needs strict headroom for `:72`).
**Verify**: `python tools/seed_corpus.py --entities 0 --out /tmp/s` exits non-zero with a clear
message (no traceback); `--dim 0`, and `--time-min 1000 --time-max 1010` likewise. A normal run
(`python tools/seed_corpus.py --entities 40 --dim 16 --out /tmp/s`) still succeeds.

### Step 2: Make harness cleanup resilient
Wrap each cleanup call in `baseline/harness.py:441-446` so one failure does not skip the others
(individual `try/except` that logs and continues, or a small helper). Preserve behavior on the
happy path.
**Verify**: `ruff check baseline/harness.py` clean; re-read confirms all three resources are
closed even if the first raises.

### Step 3: Extend seed_corpus tests for the new validation + boundaries
In `tests/test_seed_corpus.py`, add tests asserting `seed_corpus` exits non-zero (subprocess,
following the file's existing pattern) for `--dim 0`, `--entities 0`, and a too-narrow time
window; and a positive boundary test for `--entities 1` (no self-edges possible). Optionally
assert parsed embeddings are L2-normalized (norm ≈ 1.0).
**Verify**: `pytest tests/test_seed_corpus.py -q` → all pass, including the new cases.

### Step 4: Add unit tests for the harness merge logic
Create `tests/test_harness.py` with mock-based unit tests for the app-side merge:
`merge()` (dedup + top-k ordering + the intermediate-size metrics it records), and the
`graph_expand` / `vector_topk` / `relational_filter` shaping functions. Mock the DB clients —
do NOT require live Neo4j/Milvus/Postgres. Cover: all-empty inputs, all-full inputs, duplicate
src/dst handling, and the final top-k size. Model the structure on `tests/test_join_order.py`
(pure-import unit tests).
**Verify**: `pytest tests/test_harness.py -q` → all pass; `pytest tests/ -q` → all pass.

## Test plan
- New `tests/test_seed_corpus.py` cases: `--dim 0`, `--entities 0`, narrow time window (all
  expected to exit non-zero), `--entities 1` happy boundary, embedding-normalization check.
- New `tests/test_harness.py`: `merge` happy path, empty inputs, duplicate handling, top-k size;
  `graph_expand`/`vector_topk`/`relational_filter` shaping. All with mocked clients.
- Pattern to follow: `tests/test_seed_corpus.py` (subprocess + tmp_path) and
  `tests/test_join_order.py` (direct import).
- Verification: `pytest tests/ -q` → all pass including the new tests; `ruff check .` clean.

## Done criteria
- [ ] `seed_corpus.py` rejects `--dim 0`, `--entities 0`, `--edges-per-node < 0`, and a time
      window narrower than 30, each with a clear non-traceback message.
- [ ] `harness.py` closes all three resources even if one cleanup call raises.
- [ ] `tests/test_seed_corpus.py` covers the new validation + the `--entities 1` boundary.
- [ ] `tests/test_harness.py` exists and unit-tests `merge` + the shaping functions with mocks.
- [ ] `pytest tests/ -q` all pass; `ruff check . && ruff format --check .` exit 0.
- [ ] `plans/README.md` status row updated.

## STOP conditions
- The harness functions are too entangled with live clients to unit-test without a real
  refactor (e.g. `merge` reaches into a live driver). STOP and report — extracting a pure merge
  function is a larger change than this plan; note it as a follow-up rather than refactoring
  blind.

## Maintenance notes
- The harness is a DEV-1171 skeleton; when its live-system TODOs are implemented, the merge unit
  tests here become the regression guard for the SM-1 intermediate-size measurement.
- Reviewer: confirm the new validation thresholds match the actual query span (30) used at
  `seed_corpus.py:72-73`.

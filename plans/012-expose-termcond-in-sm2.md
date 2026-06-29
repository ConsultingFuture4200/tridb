# Plan 012: Expose `term_cond` in the SM-2 benchmark harness

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report instead of improvising. When done,
> update this plan's status row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat fb3f08b..HEAD -- tools/bench_sm2_corpus.py scripts/bench_sm2.sh bench/sm2_compare.py tests/test_sm2_compare.py tests/test_bench_corpus.py docs/benchmark_sm2_v0.1.0.md`
> If any in-scope file changed since this plan was written, compare the excerpts
> below with the live code before proceeding. A mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: correctness / benchmark
- **Planned at**: commit `fb3f08b`, 2026-06-26

## Why this matters

The at-scale TJS fix made `term_cond` the recall/effort knob. `make bench-live`
already has `BENCH_TERMCOND`, but the fair SM-2 head-to-head path hardcodes
`0`, which maps to the engine default of `50`. That means latency and answer
parity from `make sm2` cannot be tied to the same operating point as the
documented 5000 or 10000 runs. The harness should make the chosen `term_cond`
explicit in generated SQL, JSON, and markdown.

## Current state

- `tools/bench_corpus.py` already exposes the knob for the live benchmark:

  ```python
  tools/bench_corpus.py:235
  # tjs() early-termination depth ...
  tools/bench_corpus.py:239
  termcond = int(os.environ.get("BENCH_TERMCOND", "0") or "0")
  tools/bench_corpus.py:257
  SELECT t.id FROM tjs('entities', {k}, {termcond}, ...
  ```

- `tools/bench_sm2_corpus.py` hardcodes `0` in warmup, measured runs, and result capture:

  ```python
  tools/bench_sm2_corpus.py:100
  SELECT count(*) FROM tjs('entities', {k}, 0, ...
  tools/bench_sm2_corpus.py:115
  SELECT t.id FROM tjs('entities', {k}, 0, ...
  tools/bench_sm2_corpus.py:125
  SELECT t.id FROM tjs('entities', {k}, 0, ...
  ```

- `docs/STATUS.md` records the operating-point curve:

  ```text
  docs/STATUS.md:14
  | `term_cond` | SM-4 exact-parity | SM-3 examined |
  docs/STATUS.md:16
  | 50 (default) | 58.5% | 3.6% |
  docs/STATUS.md:18
  | 10000 | 100% | 20.1% |
  ```

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `pytest tests/test_sm2_compare.py tests/test_bench_corpus.py -q` | all pass |
| Fast tests | `make test` | all tests pass |
| Lint | `make lint` | ruff check and format check pass |
| SM-2 regeneration | `BENCH_TERMCOND=10000 make sm2` | exits 0 when engine image and baseline stack are available |

## Scope

**In scope**:
- `tools/bench_sm2_corpus.py`
- `scripts/bench_sm2.sh`
- `bench/sm2_compare.py`
- `tests/test_sm2_compare.py`
- Optional focused tests for `tools/bench_sm2_corpus.py` if needed.
- Regenerated `docs/benchmark_sm2_v0.1.0.md` and `bench/results/sm2_metrics.json` when live dependencies are available.

**Out of scope**:
- Changing the default engine behavior for `term_cond=0`.
- Changing the TJS operator implementation.
- Changing `bench/live_report.py`; plan 011 handles SM-1 accounting.

## Git workflow

- Branch: `advisor/012-sm2-termcond`
- Commit message style: `fix(bench): expose term_cond in sm2 harness`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Thread `term_cond` into SM-2 SQL generation

In `tools/bench_sm2_corpus.py`, add a `--term-cond` integer argument with default
`0`. Pass it into `build_sql` and replace all three hardcoded `0` TJS arguments
with `{term_cond}`.

Update the status print to include `term_cond=<value>`.

**Verify**: Run the generator directly with a temp output and inspect the SQL:

`python tools/bench_sm2_corpus.py --entities 60 --dim 4 --hubs 3 --fanout 12 --queries 2 --k 5 --runs 2 --term-cond 10000 --sql-out /tmp/sm2.sql --manifest-out /tmp/sm2.json`

Expected: exit 0, `/tmp/sm2.sql` contains `tjs('entities', 5, 10000,`.

### Step 2: Pass `BENCH_TERMCOND` from `scripts/bench_sm2.sh`

In `scripts/bench_sm2.sh`, define:

```bash
TERMCOND="${BENCH_TERMCOND:-0}"
```

Include it in the log line and pass `--term-cond "$TERMCOND"` to
`tools/bench_sm2_corpus.py`.

**Verify**: `BENCH_TERMCOND=10000 bash scripts/bench_sm2.sh missing-image` should
still fail early on the Docker image check before generation. Then verify the
generator command itself using Step 1.

### Step 3: Record `term_cond` in SM-2 JSON and markdown

Choose the least invasive path:

- Add `term_cond` to the manifest written by `tools/bench_sm2_corpus.py`, or
- Add it to the result payload in `bench/sm2_compare.py` from the manifest.

Then update `bench/sm2_compare.py:render_md` so the generated markdown includes
the selected `term_cond` in the headline metadata near corpus size, seed, and
runs/query.

**Verify**: Extend `tests/test_sm2_compare.py` so `_manifest()` includes
`"term_cond": 10000`, `compare()` preserves it in the result, and `render_md()`
contains `term_cond=10000`.

### Step 4: Update tests that assume canonical `0`

If any tests assert hardcoded `0`, update them to assert the default remains `0`
and add one explicit non-default case. `tests/test_bench_corpus.py` currently
checks `bench_corpus.py`, not `bench_sm2_corpus.py`; do not weaken that test
unless it directly fails from this change.

**Verify**: `pytest tests/test_sm2_compare.py tests/test_bench_corpus.py -q` exits 0.

### Step 5: Regenerate SM-2 artifacts if live dependencies are available

If the engine image exists and the baseline stack is running, run:

`BENCH_TERMCOND=10000 make sm2`

This should regenerate the SM-2 markdown and JSON with `term_cond=10000` recorded.
If the live dependencies are unavailable, do not fake the artifacts; note that
regeneration is pending.

**Verify**: `rg -n "term_cond" docs/benchmark_sm2_v0.1.0.md bench/results/sm2_metrics.json` shows the selected value after regeneration.

## Test plan

- Unit tests for `bench/sm2_compare.py` preserving and rendering `term_cond`.
- Direct generator smoke test proving `--term-cond 10000` reaches every TJS call.
- Full `make test` and `make lint`.
- Live `BENCH_TERMCOND=10000 make sm2` when dependencies are present.

## Done criteria

- [ ] SM-2 SQL generation accepts `--term-cond` and defaults to `0`.
- [ ] `scripts/bench_sm2.sh` passes `BENCH_TERMCOND` through.
- [ ] SM-2 JSON and markdown record the chosen `term_cond`.
- [ ] Tests cover default and non-default behavior.
- [ ] `make test` and `make lint` pass.
- [ ] If live dependencies are available, `BENCH_TERMCOND=10000 make sm2` has refreshed committed artifacts.
- [ ] `plans/README.md` row for plan 012 is updated.

## STOP conditions

- The TJS SQL signature changes and no longer accepts `term_cond` as the third argument.
- The baseline comparison format cannot represent the setting without a larger schema migration.
- A live `BENCH_TERMCOND=10000 make sm2` run produces answer parity divergence; stop and report the numbers instead of editing docs around it.

## Maintenance notes

Every future benchmark result should name the operating point. Do not mix
latency from `term_cond=50` with recall from `term_cond=10000` in the same claim.

# Plan 013: Refresh TJS termination and GX10 status documentation

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report instead of improvising. When done,
> update this plan's status row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat fb3f08b..HEAD -- README.md docs/decisions/0007-tjs-operator.md scripts/patches/tridb_tjs_operator.patch docs/benchmark_results_v0.1.0.md tests/test_bench_corpus.py`
> If any in-scope file changed since this plan was written, compare the excerpts
> below with the live code before proceeding. A mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 011, 012
- **Category**: docs
- **Planned at**: commit `fb3f08b`, 2026-06-26

## Why this matters

The docs still contain two stale messages after the recent TJS scale fix and
GX10 sign-off. First, ADR-0007 and the original TJS SQL comments say
graph-rejected candidates count as `term_cond` drops, which is exactly the bug
that was later fixed. Second, the README says the GX10 ARM64 build sign-off is
still remaining, while `docs/STATUS.md` says it passed on June 25, 2026. These
contradictions make it easy for an executor or reader to repeat the wrong
operating semantics.

## Current state

- `docs/STATUS.md` is the current source for the TJS scale fix:

  ```text
  docs/STATUS.md:10
  Fixed in `tridb_tjs_predicate_termination.patch`
  docs/STATUS.md:11
  a "drop" now means past-frontier only: PQ full AND distance >= k-th
  ```

- `docs/decisions/0007-tjs-operator.md` still says the old behavior is correct:

  ```text
  docs/decisions/0007-tjs-operator.md:126
  `term_cond` counts graph-rejected candidates
  docs/decisions/0007-tjs-operator.md:129
  fires early termination sooner
  ```

- `scripts/patches/tridb_tjs_operator.patch` contains SQL-comment text from the
  original patch that says graph-rejected candidates count as drops. The active
  patch chain later applies `tridb_tjs_predicate_termination.patch`, so this is
  stale explanatory text, not the final intended semantics.

- README status is stale:

  ```text
  README.md:162
  Remaining on-target work: the GX10 ARM64 build sign-off and the 128 GB headline benchmark.
  ```

- `docs/STATUS.md` says the sign-off happened:

  ```text
  docs/STATUS.md:27
  ON-TARGET SIGN-OFF 2026-06-25
  docs/STATUS.md:30
  DEV-1160/1161 signed off.
  ```

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Stale text search | `rg -n "term_cond.*graph-rejected|graph-rejected.*term_cond|Remaining on-target work: the GX10 ARM64 build sign-off" README.md docs scripts/patches tests` | no stale matches after edits |
| Fast tests | `make test` | all tests pass |
| Lint | `make lint` | ruff check and format check pass |

## Scope

**In scope**:
- `README.md`
- `docs/decisions/0007-tjs-operator.md`
- `scripts/patches/tridb_tjs_operator.patch` comments/SQL comments only
- `docs/benchmark_results_v0.1.0.md` if plan 011 has not already corrected the SM-1 wording
- Tests only if they assert stale text.

**Out of scope**:
- Changing TJS implementation.
- Changing benchmark numbers without rerunning the benchmark.
- Rewriting the whole README.

## Git workflow

- Branch: `advisor/013-refresh-tjs-docs`
- Commit message style: `docs(tjs): refresh termination and gx10 status`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Update ADR-0007 termination semantics

In `docs/decisions/0007-tjs-operator.md`, replace the bullet beginning
"`term_cond` counts graph-rejected candidates" with the fixed semantics:

- Predicate rejections do not advance `consecutive_drops`.
- A drop means past-frontier only: the priority queue is full and the candidate
  distance is at or beyond the current kth score.
- Termination cannot fire before the priority queue fills.
- `term_cond` is still a recall/effort knob; cite the curve in `docs/STATUS.md`.

Keep the note about SQL-fragment injection; it is still relevant.

**Verify**: `rg -n "term_cond.*graph-rejected|fires early termination sooner" docs/decisions/0007-tjs-operator.md` returns no matches.

### Step 2: Update stale TJS patch comments

In `scripts/patches/tridb_tjs_operator.patch`, adjust only comments and SQL
COMMENT text that describe the old drop accounting. Do not change code hunks
unless the active patch no longer applies, because the final semantics are
implemented by the later `tridb_tjs_predicate_termination.patch`.

The comment should say this initial patch is superseded by the predicate-correct
termination patch in the active chain, or directly describe the final behavior
if that comment is part of user-facing SQL.

**Verify**: `rg -n "graph-rejected included|restrictive graph predicate.*fires|counts EVERY candidate" scripts/patches/tridb_tjs_operator.patch` returns no stale matches.

### Step 3: Update README status

Change README status to say the GX10 ARM64 build and engine suite have been
signed off, and the remaining on-target work is the 128 GB headline benchmark
and any explicitly documented post-scale benchmark work. Keep the x86 standin
caveat for current committed benchmark artifacts.

Do not claim the 128 GB benchmark is done.

**Verify**: `rg -n "GX10 ARM64 build sign-off" README.md` returns no stale
"remaining" claim, and README still mentions the 128 GB headline benchmark as
remaining.

### Step 4: Align benchmark result wording if needed

If plan 011 has not already done this, update `docs/benchmark_results_v0.1.0.md`
so SM-1 wording matches the current TJS design: top-k heap plus precomputed
reachable-id set, not "never materializing the reachable set."

**Verify**: `rg -n "never materializing the reachable|only.*top-k heap" docs/benchmark_results_v0.1.0.md` returns no stale match.

## Test plan

- Search-based checks for stale docs strings.
- `make test` to ensure no generated-doc assumptions in tests broke.
- `make lint` for Python formatting/lint health.

## Done criteria

- [ ] ADR-0007 documents predicate-correct drop accounting.
- [ ] TJS patch comments no longer contradict the active final patch.
- [ ] README no longer says GX10 ARM64 build sign-off remains.
- [ ] Benchmark docs do not claim TJS avoids all reachable-set materialization.
- [ ] `make test` and `make lint` pass.
- [ ] `plans/README.md` row for plan 013 is updated.

## STOP conditions

- The active patch chain no longer applies in the documented order.
- Updating patch comments changes `git apply` behavior or patch context.
- You find a newer status doc that contradicts `docs/STATUS.md`; stop and ask which status is authoritative.

## Maintenance notes

Docs that describe benchmark numbers should name the date, corpus scale, and
`term_cond`. Avoid replacing one stale absolute claim with another; use
`docs/STATUS.md` as the per-issue status source.

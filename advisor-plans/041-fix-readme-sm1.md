# Plan 041: Fix README SM-1 headline to honest 1.07× FAIL

> **Executor instructions**: Docs-only. Follow steps; honor STOP. Update `advisor-plans/README.md` when done.
>
> **Drift check**: `git diff --stat c216750..HEAD -- README.md docs/benchmark_results_v0.1.0.md docs/STATUS.md`

## Status
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `c216750`, 2026-07-09

## Why this matters

The public README benchmarks table still claims **SM-1 = 32×**. The corrected live accounting is
**1.07× FAIL** under `max(k, reached)` (`docs/benchmark_results_v0.1.0.md`). SM-1 is a row-count ratio
(hardware-independent); GX10 does not restore 32×. A false front-page number undoes the honesty work
in STATUS/benchmark_results and is the worst class of doc bug.

## Current state

- `README.md:106`:
  ```
  | **SM-1** | Intermediate-result reduction vs. baseline | ≥ 5× | **32×** |
  ```
- Corrected source of truth (`docs/benchmark_results_v0.1.0.md:26`): SM-1 **1.07×** FAIL (baseline 1920 vs TriDB 1799).
- `docs/STATUS.md:123` already records the correction.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Grep stale claim | `rg -n 'SM-1.*32|32×' README.md` | no false 32× after fix (or only historical notes) |
| Fast layer | `make test && make lint` | exit 0 (no code change expected) |

## Scope

**In scope:** `README.md` benchmarks table (and any adjacent README sentence that still asserts SM-1 32× as current result).

**Out of scope:** Redesigning SM-1 operator (plan 056 / DIR-06 territory); rewriting full `docs/benchmark_sm2_*.md` history; badges that correctly cite SM-2.

## Git workflow
- Branch: `advisor/041-readme-sm1`
- Commit: `docs(readme): correct SM-1 to 1.07× FAIL (advisor 041)`

## Steps

### Step 1: Replace SM-1 result cell

Change the SM-1 **Result** cell to something equivalent to:

```markdown
| **SM-1** | Intermediate-result reduction vs. baseline | ≥ 5× | **1.07× FAIL** (standin; corrected `max(k, reached)` — see [`docs/benchmark_results_v0.1.0.md`](docs/benchmark_results_v0.1.0.md); not restored by GX10) |
```

Keep SM-2 / SM-3 / SM-4 / SM-5 as they are unless they also contradict `benchmark_results` (do not invent numbers).

**Verify**: `rg -n '32×|\*\*32' README.md` — no remaining SM-1 = 32× as current result. Manual read of the table.

### Step 2: Cross-check nearby README claims

If the About/Features section still says “32× intermediate reduction,” fix to match. Do not silently drop SM-1 from the table.

**Verify**: `rg -n 'intermediate|SM-1' README.md` looks honest; links to `docs/benchmark_results_v0.1.0.md`.

## Test plan
- No new tests. Optional: none.
- `make test && make lint` still green.

## Done criteria
- [ ] README SM-1 result is **1.07× FAIL** (or equivalent wording) with link to corrected report
- [ ] No current-result claim of SM-1 ≥ 5× / 32× on the front page
- [ ] `make test` / `make lint` green
- [ ] README only (or explicitly justified adjacent README lines)
- [ ] Index row DONE

## STOP conditions
- `docs/benchmark_results_v0.1.0.md` itself no longer says 1.07× — re-read STATUS and use the latest corrected figure; do not invent.
- Someone already fixed README on HEAD — mark plan DONE with note, no churn.

## Maintenance notes
- When streaming graph predicate lands and SM-1 is re-measured, update **both** README and `benchmark_results` together.
- Reviewer: ensure wording still distinguishes SM-2 “100% of queries faster” from SM-1 intermediate reduction.

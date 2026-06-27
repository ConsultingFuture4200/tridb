# Plan 005: Fix the stale @-scale parity row in the GTM doc

> **Executor instructions**: A small, careful docs edit. Read the WHOLE of
> `docs/gtm_opensource_v0.1.0.md` first so the fix is consistent with the rest of the doc. STOP and report
> if the "Current state" excerpt doesn't match. Update this plan's row in `advisor-plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 7bf3dca..HEAD -- docs/gtm_opensource_v0.1.0.md docs/STATUS.md docs/benchmark_results_v0.1.0.md`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `7bf3dca`, 2026-06-26

## Why this matters

`docs/gtm_opensource_v0.1.0.md` is the launch messaging plan, and its own thesis is "honesty is the
differentiator." But its "Where we actually stand" table has a stale @-scale answer-parity row that
**predates the DEV-1169 predicate-termination fix** and contradicts the same doc's R1 section (which
documents the post-fix recall curve) and the doc's own 2026-06-26 addendum. A reader sees "Open question"
where the doc later says "fixed." Fixing it keeps the credibility story self-consistent.

## Current state

- The stale row, `docs/gtm_opensource_v0.1.0.md:45` (verbatim):
  ```
  | Answer parity vs baseline @ scale | **7/12 exact, Jaccard 0.58** (was 12/12 at toy scale) | **Open question** — see Risk R1 |
  ```
- The SAME doc's **Risk R1** section already supersedes this: it documents that the 100k/dim-768 GX10 run's
  empty/partial parity was the predicate-blind early-termination BUG, now FIXED, and that the honest
  @-scale result is a **recall/effort curve** (term_cond 50 → 58.5%, 5000 → 97.2%, 10000 → 100% exact).
  The doc's top "Addendum 2026-06-26" also references this.
- **Do NOT** "fix" the number to 12/12: the `12/12 exact (Jaccard 1.0)` figure in `docs/STATUS.md:146`
  and `docs/benchmark_sm2_v0.1.0.md` is the **2000/dim-32 x86 standin** (and is an SM-2 latency-win count
  plus standin SM-4), NOT the @-scale parity. Conflating the standin with @-scale is exactly the error to
  avoid. The correct @-scale statement is the R1 curve.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Find the stale row | `grep -n "7/12\|Jaccard 0.58\|Open question" docs/gtm_opensource_v0.1.0.md` | the line(s) above |
| Confirm no stale claim remains | `grep -n "7/12" docs/gtm_opensource_v0.1.0.md` | no output after the fix |

## Scope

**In scope:** `docs/gtm_opensource_v0.1.0.md` (the one table row, and only if needed a one-line tweak to the
R1 lead so they agree).
**Out of scope:** the R1 curve table itself (already correct); STATUS / benchmark_sm2 / benchmark_results
(their numbers are correct for their scales); bumping the doc to v0.2.0 (the addendum convention is fine).

## Steps

### Step 1: Rewrite the @-scale parity row to match R1

Replace line 45 so the result column states the post-DEV-1169 @-scale recall **curve** (not "7/12", not
"12/12") and the status column points to R1 as resolved-with-a-curve rather than "Open question". Keep it
honest and terse, e.g. the result cell becomes a pointer to the curve ("recall/effort curve: 58.5% →
100% exact across term_cond — see R1") and the status cell becomes "Fixed (DEV-1169); curve, not a point".
Match the table's existing tone and the proof-value framing of the other rows.

**Verify**: `grep -n "7/12" docs/gtm_opensource_v0.1.0.md` → no output.

### Step 2: Read-back consistency check

Re-read the table, the Addendum, and Risk R1 together and confirm there is no remaining place that calls
@-scale parity an "open question" or quotes the pre-fix 7/12 number.

**Verify**: `grep -n "Open question" docs/gtm_opensource_v0.1.0.md` → returns nothing, OR only points that
are genuinely still open (e.g. the public-dataset value claim), not the answer-parity-@-scale one.

## Test plan

Docs only — no automated test. Acceptance is the two greps above plus a human read-back that the table,
addendum, and R1 now tell one consistent story: *mechanism + on-target latency real; @-scale parity is a
recall curve (fixed), not an open question; the remaining open item is the public-dataset value claim.*

## Done criteria

- [ ] `grep -n "7/12" docs/gtm_opensource_v0.1.0.md` → empty.
- [ ] The @-scale parity row references the R1 recall curve and is not marked "Open question".
- [ ] No other doc changed; the standin 12/12 figures elsewhere are untouched.
- [ ] `advisor-plans/README.md` row updated.

## STOP conditions

- The R1 section's curve numbers differ from (50→58.5%, 5000→97.2%, 10000→100%) — the doc may have been
  updated; reconcile to whatever R1 currently states rather than hardcoding these.
- You're tempted to write "12/12 @ scale" — STOP; that's the standin, not @-scale (see Current state).

## Maintenance notes

- When the real 100k/768 headline run lands (Linear DEV-1286), update R1's curve with the measured numbers
  and this row will already point at it.
- Reviewer: confirm the fix did not import the 2k/32 standin number as if it were the @-scale result.

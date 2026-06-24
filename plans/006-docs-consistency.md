# Plan 006: Fix docs drift — built vs. designed vs. gated, and the two graph-store dirs

> **Executor instructions**: Follow step by step; run every verification command. On a STOP
> condition, stop and report. Update this plan's row in `plans/README.md` when done.
>
> **Drift check (run first)**: `git -C /home/bob/code/tridb diff --stat cb097db..HEAD -- docs/ README.md CLAUDE.md`
> If changed, re-read the cited files before editing; mismatch with excerpts = STOP.

## Status
- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `cb097db`, 2026-06-24

## Why this matters
The docs contradict each other and the code, which misleads contributors about what is built,
what is only designed, and what is hardware-gated. Three concrete drifts:
1. `docs/STATUS.md`'s top blockquote says the native C work is "buildable & testable here now,
   **not** GX10-gated," but the same file's table marks DEV-1164–1170 🔴 GX10-gated and
   `README.md` says they "must run on the GX10."
2. `docs/graph_store_layout_v0.1.0.md` reads as a spec of the *current* store, but it describes
   the **future** custom 32KB-page access method (DEV-1164); v0 (`src/graph_store_ext/`) uses a
   heap relation. A reader can mistake the spec for what's built.
3. Two directories with near-identical names — `src/graph_store/` (a GX10-gated C interface
   *skeleton*, `graphstore.h`, never compiled) and `src/graph_store_ext/` (the working v0
   extension) — with nothing at a glance saying which is which.

## Current state
- `docs/STATUS.md:6-11` — the "RE-GATED" blockquote with "the native C work (DEV-1164–1170) is
  **buildable & testable here now**, not GX10-gated" — followed by a table marking those issues
  🔴. `README.md` (~lines 55-60) — "must run on the GX10 and cannot be compiled on a non-target
  workstation."
- `docs/graph_store_layout_v0.1.0.md` — opens as a normative layout spec; no banner saying it
  describes the v1 custom AM, not v0. `docs/graph_store_v0_limitations.md` §3 already states v0
  is heap-backed and the custom-page layout is future — but the layout doc itself doesn't point
  back.
- `src/graph_store/README.md` exists and explains the skeleton; `src/graph_store/graphstore.h`
  is the interface contract. `src/graph_store_ext/` holds the working extension.
- Note: the relationship "fork builds on x86 (proven) → C extensions ARE buildable/testable on
  the dev box (graph_store v0 was)" is TRUE; the table's 🔴 conflates "buildable on x86" with
  "ARM sign-off + 128 GB benchmark are GX10-only." The fix is to make that distinction explicit,
  not to flip the table.

## Commands you will need
| Purpose | Command | Expected |
|---|---|---|
| Find the contradiction | `grep -n 'not GX10-gated\|GX10-gated\|must run on the GX10' docs/STATUS.md README.md` | the conflicting lines |
| Markdown sanity | `git -C /home/bob/code/tridb diff -- docs/ README.md` | only intended edits |

## Scope
**In scope**: `docs/STATUS.md`, `docs/graph_store_layout_v0.1.0.md`, `README.md`,
`src/graph_store/README.md` (and optionally a one-line pointer in `src/graph_store_ext/` — add a
short `README.md` there if none exists). **Out of scope**: any code; the ADRs in
`docs/decisions/` (settled tradeoffs — do not rewrite); renaming directories (too invasive for
this plan — clarify in prose instead).

## Git workflow
- Branch `advisor/006-docs-consistency`; commit `docs: reconcile built-vs-designed-vs-gated`.

## Steps

### Step 1: Reconcile the STATUS.md gating contradiction
Edit the `docs/STATUS.md:6-11` blockquote so it distinguishes: (a) the MSVBASE fork + C
extensions ARE buildable/testable on x86 (proven: graph_store v0), from (b) what remains
GX10-only — ARM64 build sign-off (DEV-1160) and the 128 GB benchmark. Make the prose agree with
the table and with `README.md`. Do not claim the full custom-AM work is "done here."
**Verify**: `grep -n 'GX10' docs/STATUS.md` — the blockquote no longer asserts the native C work
is "not GX10-gated" without qualification; it matches the table + README.

### Step 2: Banner the layout spec as future (v1) design
Add a one-paragraph banner at the very top of `docs/graph_store_layout_v0.1.0.md`: this spec
describes the **v1 custom adjacency-list access method (DEV-1164)**; the current **v0**
implementation (`src/graph_store_ext/`) uses a heap relation; see
`docs/graph_store_v0_limitations.md`.
**Verify**: the doc's first screen makes the v1-vs-v0 distinction unmissable.

### Step 3: Disambiguate the two directories
In `src/graph_store/README.md`, add a one-line "Not to be confused with `src/graph_store_ext/`"
note (skeleton/contract vs. working v0 extension). If `src/graph_store_ext/` has no README, add
a 3-line one stating it is the working v0 heap-backed extension and pointing to the limitations
doc + the layout spec (the future design).
**Verify**: both directories' READMEs cross-reference each other and state which is which.

### Step 4: Cross-check the other docs for the same drift
Skim `docs/sqlpgq_logical_plan_v0.1.0.md` and `docs/join_order_heuristic_v0.1.0.md` for any
claim that assumes a working scalar `<->` distance (contradicted by `docs/fork_findings.md` §2).
If found, add a one-line footnote pointing to the fork finding. (Do not redesign — just flag.)
**Verify**: `grep -n 'fork_findings\|scalar' docs/sqlpgq_logical_plan_v0.1.0.md docs/join_order_heuristic_v0.1.0.md`
— either no such assumption exists, or it now carries a footnote.

## Test plan
Docs-only; no automated tests. The verification is the greps above plus a human read confirming
STATUS.md, README.md, and the table now tell one consistent story.

## Done criteria
- [ ] `docs/STATUS.md`'s blockquote no longer contradicts its table or `README.md`.
- [ ] `docs/graph_store_layout_v0.1.0.md` opens with a v1-vs-v0 banner.
- [ ] `src/graph_store/` and `src/graph_store_ext/` READMEs disambiguate each other.
- [ ] Any doc assuming a working scalar `<->` distance carries a footnote to fork findings (or
      none exists).
- [ ] `plans/README.md` status row updated.

## STOP conditions
- Resolving the STATUS.md contradiction would require changing the *table's* 🔴 markers (i.e.
  the real gating is genuinely ambiguous, not just the prose). STOP and ask the maintainer which
  is authoritative — do not silently re-gate issues.

## Maintenance notes
- When v0 is superseded by the v1 custom AM, revisit the layout-spec banner and the two-dir note.
- Reviewer: confirm no ADR (`docs/decisions/`) was reworded — those are settled decisions.

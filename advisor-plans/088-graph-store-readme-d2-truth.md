# Plan 088: Reconcile `src/graph_store/README.md` with the D2 un-fork

> **Executor instructions**: Docs-only. Verify every claim you write against the live code you
> cite; do not import claims from other docs unverified. Skip the advisor index update.
>
> **Drift check (run first)**:
> `git diff --stat a780b46..HEAD -- src/graph_store/README.md src/graph_store/gph_page.h docs/INSTALL_stock_pg.md`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs
- **Planned at**: commit `a780b46`, 2026-07-16

## Why this matters

`src/graph_store/README.md` is the directory's front door for agents and contributors, and it is
actively wrong post-D2: it describes the store as PG 13.4-only over 32KB pages and claims a
`BLCKSZ == 32768` static assert that no longer exists. Agents "fixing" code against this README
will re-introduce fork-only assumptions the un-fork deliberately removed (plan 067 reconciled the
top-level docs but not this one).

## Current state (verified)

- `src/graph_store/README.md:3` — "adjacency-list topology store over 32KB pages".
- `src/graph_store/README.md:8-9` — "It is architecture-independent PostgreSQL 13.4 access-method
  C; only the live GX10 ARM64 build and the 128GB benchmark remain hardware-gated." (No mention of
  the stock PG16/17 build that now exists and runs in CI.)
- `src/graph_store/README.md:20` — the `gph_page.h` row claims "static-asserts `BLCKSZ == 32768`".
- Live `src/graph_store/gph_page.h:31-33`:
  ```c
  /* Any stock page size works (layout is BLCKSZ-derived); 8KB is the smallest PG supports that
   * keeps the geometry sane (>= 250 slots/page). 32KB remains the fork's performance target. */
  StaticAssertDecl(BLCKSZ >= 8192, "graph store requires BLCKSZ >= 8192");
  ```
- The stock build path exists: `scripts/pg17_graph_test.sh`, CI job `stock-pg` (PG16+17),
  `make stock-graph-test`, `docs/INSTALL_stock_pg.md`; root `CLAUDE.md` and top-level `README.md`
  already carry the corrected framing (plan 067) — match their wording, don't invent new claims.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Truth greps | `grep -n '32768\|13\.4' src/graph_store/README.md` | no stale claims remain (13.4 may remain only where explicitly labeled as the fork target) |
| Host | `make test && make lint` | exit 0 (unchanged) |

## Scope

**In scope**: `src/graph_store/README.md` only.

**Out of scope**: any code, any other doc (if another doc contradicts the code, report it, don't
edit it here).

## Git workflow

Use assigned `dustin/dev-NNNN`. Suggested commit: `docs(graph): reconcile README with D2 un-fork`.

## Steps

### Step 1: Correct the three stale claims

- Page size: BLCKSZ-derived layout, `BLCKSZ >= 8192` static assert; 8KB works on stock PG, 32KB is
  the fork's high-degree performance target (mirror `gph_page.h`'s own comment and root
  `CLAUDE.md`'s phrasing).
- Build targets: PG 13.4 fork AND stock PG 16/17 (PGXS via `scripts/pg17_graph_test.sh` /
  `make stock-graph-test`, CI job `stock-pg`); GX10 gating applies to the fork/ARM sign-off only.
- The `gph_page.h` table row: describe the real assert.

Preserve everything that is still true (the v0/v1 disambiguation, TR-1 invariants, file table
structure).

**Verify**: every changed sentence traces to a live code line or an existing post-D2 doc; the truth
grep shows no unlabeled `32768`/`13.4` claims.

### Step 2: Read-back check

Re-read the full file once; confirm no other pre-D2 claims survive (e.g. "only buildable in the
fork image" phrasing).

**Verify**: `make test && make lint && git diff --check` exit 0; `git status --short` shows only
the one file.

## Test plan

Docs-only: truth greps + read-back. No test suite changes.

## Done criteria

- [ ] README states the BLCKSZ >= 8192 capability model and the dual fork/stock build targets.
- [ ] No claim in the file contradicts `gph_page.h` or the stock CI reality.
- [ ] Only `src/graph_store/README.md` changed; host checks pass.

## STOP conditions

- The live `gph_page.h` assert differs from the excerpt above (re-verify, then report).
- Correcting the README would require asserting something you cannot verify in code.

## Maintenance notes

Directory READMEs are what code agents read first; when a capability changes (as in D2), grep for
its old constants (`32768`, `13.4`) across `src/**/README.md`, not just top-level docs.
